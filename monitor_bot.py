#!/usr/bin/env python3
"""
Discord server-watch bot for a headless Debian box.

Alerts (with the offending processes) when:
  - temperature is too high
  - CPU, RAM or disk usage is too high
  - network bandwidth is too high

Slash commands:
  /status                      snapshot of the server's health and power use
  /config show                 every setting and where its value comes from
  /config set <key> <value>    change a setting (saved, applies immediately)
  /config enable|disable       turn an alert type on or off
  /config reset [key]          undo changes made from Discord

Settings precedence: changed in Discord  >  .env file  >  built-in default.
Only the bot owner can change settings. Secrets and IDs (token, channel, user)
stay in .env and need a container recreate to change.
"""
import asyncio
import json
import logging
import math
import os
import shutil
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import discord
import psutil
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------- static ---
# These come only from .env (they need a restart to change).
TOKEN = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))  # post in a channel...
ALERT_USER_ID = int(os.getenv("ALERT_USER_ID", "0"))        # ...or DM this user

# When running in Docker, the host's root filesystem is bind-mounted here (e.g. /host)
HOST_ROOT = os.getenv("HOST_ROOT", "").rstrip("/")

# Where settings changed from Discord are saved (a mounted volume in Docker)
DATA_DIR = os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else ".")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

IGNORED_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "iso9660", "fuse.lxcfs"}
NCPU = psutil.cpu_count() or 1


def shown(path: str) -> str:
    """Path as the host sees it (strips the container's HOST_ROOT prefix)."""
    if HOST_ROOT and path.startswith(HOST_ROOT):
        return path[len(HOST_ROOT):] or "/"
    return path


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("serverwatch")


# ---------------------------------------------------------------- settings ---
@dataclass(frozen=True)
class Setting:
    key: str                      # same name as the .env variable
    kind: type                    # bool, int, float or str
    default: object
    group: str
    lo: Optional[float] = None    # allowed range for numbers
    hi: Optional[float] = None


SETTINGS = [
    Setting("ENABLE_TEMP", bool, True, "Alert types"),
    Setting("ENABLE_CPU", bool, True, "Alert types"),
    Setting("ENABLE_RAM", bool, True, "Alert types"),
    Setting("ENABLE_DISK", bool, True, "Alert types"),
    Setting("ENABLE_NET", bool, True, "Alert types"),
    Setting("TEMP_LIMIT_C", float, 80.0, "Limits", 1, 150),
    Setting("CPU_LIMIT", float, 90.0, "Limits", 1, 100),
    Setting("RAM_LIMIT", float, 90.0, "Limits", 1, 100),
    Setting("DISK_LIMIT", float, 90.0, "Limits", 1, 100),
    Setting("NET_LIMIT_MBIT", float, 50.0, "Limits", 0.1, 100000),
    Setting("CHECK_INTERVAL", int, 30, "Timing", 10, 3600),
    Setting("SUSTAIN_CHECKS", int, 3, "Timing", 1, 20),
    Setting("ALERT_COOLDOWN", int, 900, "Timing", 0, 86400),
    Setting("POWER_IDLE_W", float, 15.0, "Power", 0, 10000),
    Setting("POWER_MAX_W", float, 60.0, "Power", 0, 10000),
    Setting("POWER_OTHER_W", float, 0.0, "Power", 0, 10000),
    Setting("PRICE_PER_KWH", float, 0.0, "Power", 0, 10000),
    Setting("CURRENCY", str, "$", "Power"),
]
BY_KEY = {s.key: s for s in SETTINGS}
GROUPS = list(dict.fromkeys(s.group for s in SETTINGS))

TRUE_WORDS = {"1", "true", "yes", "on", "enable", "enabled"}
FALSE_WORDS = {"0", "false", "no", "off", "disable", "disabled"}


def parse_value(s: Setting, raw) -> object:
    """Convert and validate a value for a setting. Raises ValueError with a friendly message."""
    raw = str(raw).strip()
    if s.kind is bool:
        low = raw.lower()
        if low in TRUE_WORDS:
            return True
        if low in FALSE_WORDS:
            return False
        raise ValueError("use on or off")
    if s.kind is str:
        if not raw:
            raise ValueError("can't be empty")
        if len(raw) > 8:
            raise ValueError("too long (8 characters max)")
        return raw
    try:
        val = s.kind(raw)
    except ValueError:
        raise ValueError("must be a whole number" if s.kind is int else "must be a number") from None
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError("must be a number")
    if (s.lo is not None and val < s.lo) or (s.hi is not None and val > s.hi):
        raise ValueError(f"must be between {s.lo:g} and {s.hi:g}")
    return val


def display(s: Setting, v) -> str:
    if s.kind is bool:
        return "✅ on" if v else "⛔ off"
    if s.kind is float:
        return f"{v:g}"
    return str(v)


class Config:
    """Settings with precedence: changed in Discord > .env > built-in default."""

    def __init__(self, path: str):
        self.path = path
        self.env = {}
        self.overrides = {}
        for s in SETTINGS:
            raw = os.getenv(s.key)
            if raw is None or not raw.strip():
                continue
            try:
                self.env[s.key] = parse_value(s, raw)
            except ValueError as e:
                log.warning("ignoring %s=%r from .env: %s (using the default)", s.key, raw, e)
        try:
            with open(path) as f:
                saved = json.load(f)
        except FileNotFoundError:
            saved = {}
        except (OSError, ValueError):
            log.warning("couldn't read %s, ignoring it", path)
            saved = {}
        for key, val in (saved.items() if isinstance(saved, dict) else []):
            if key in BY_KEY:
                try:
                    self.overrides[key] = parse_value(BY_KEY[key], val)
                except ValueError:
                    log.warning("ignoring saved %s=%r", key, val)

    def get(self, key: str):
        if key in self.overrides:
            return self.overrides[key]
        if key in self.env:
            return self.env[key]
        return BY_KEY[key].default

    __getitem__ = get

    def source(self, key: str) -> str:
        if key in self.overrides:
            return "discord"
        if key in self.env:
            return ".env"
        return "default"

    def save(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.overrides, f, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError:
            log.exception("couldn't save settings to %s", self.path)
            return False

    def set(self, key: str, raw):
        """Validate and apply a change. Returns (new_value, saved_to_disk). Raises ValueError."""
        value = parse_value(BY_KEY[key], raw)
        self.overrides[key] = value
        return value, self.save()

    def reset(self, key: Optional[str] = None) -> bool:
        if key is None:
            self.overrides.clear()
        else:
            self.overrides.pop(key, None)
        return self.save()


cfg = Config(CONFIG_FILE)


# ---------------------------------------------------------------- sampling ---
def sample_procs(window: float = 1.0):
    """Sample per-process CPU/RAM/disk-IO over `window` seconds."""
    procs = {}
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            p.cpu_percent(None)  # prime
            io = p.io_counters() if hasattr(p, "io_counters") else None
            procs[p.pid] = (p, io)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.cpu_percent(None)
    time.sleep(window)

    rows = []
    for pid, (p, io0) in procs.items():
        try:
            row = {
                "pid": pid,
                "name": p.info["name"] or "?",
                "user": p.info["username"] or "?",
                # normalised so 100% == all cores busy, matching the system figure
                "cpu": p.cpu_percent(None) / NCPU,
                "rss": p.memory_info().rss,
                "mem_pct": p.memory_percent(),
                "read_bps": 0.0,
                "write_bps": 0.0,
            }
            if io0:
                io1 = p.io_counters()
                row["read_bps"] = (io1.read_bytes - io0.read_bytes) / window
                row["write_bps"] = (io1.write_bytes - io0.write_bytes) / window
            rows.append(row)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows, psutil.cpu_percent(None)


def read_temps():
    """Return (hottest_c, [(chip, label, c), ...]). Empty if no sensors."""
    try:
        data = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None, []
    readings = []
    for chip, entries in data.items():
        for e in entries:
            if e.current is not None:
                readings.append((chip, e.label or "temp", e.current))
    if not readings:
        return None, []
    return max(r[2] for r in readings), readings


def worst_disk():
    worst = None
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if HOST_ROOT and not (mp == HOST_ROOT or mp.startswith(HOST_ROOT + "/")):
            continue  # in Docker, only look at the host's mounts
        if part.fstype in IGNORED_FS or shown(mp).startswith(("/snap", "/boot/efi")):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if worst is None or u.percent > worst[1]:
            worst = (part.mountpoint, u.percent, u.used, u.total)
    return worst


# ------------------------------------------------------------------ power ---
# In Docker the host's /sys is visible under HOST_ROOT (the container's own /sys
# hides the RAPL counters), so read from there when HOST_ROOT is set.
RAPL_DIR = f"{HOST_ROOT}/sys/class/powercap"


def read_rapl():
    """Cumulative CPU package energy counters (µJ) from Intel/AMD RAPL, if readable."""
    vals = {}
    try:
        names = sorted(os.listdir(RAPL_DIR))
    except OSError:
        return vals
    for name in names:
        # top-level package domains only (intel-rapl:0), not sub-domains (intel-rapl:0:0)
        if not name.startswith("intel-rapl:") or name.count(":") != 1:
            continue
        base = os.path.join(RAPL_DIR, name)
        try:
            with open(base + "/energy_uj") as f:
                energy = int(f.read())
            with open(base + "/max_energy_range_uj") as f:
                wrap = int(f.read())
        except (OSError, ValueError):
            continue  # not readable (needs root) or not present
        vals[name] = (energy, wrap)
    return vals


def estimate_power(r0, r1, window, cpu_pct):
    """Return (watts, source). Prefer real RAPL readings, else a linear CPU-load model."""
    common = [k for k in r0 if k in r1]
    if common and window > 0:
        uj = 0
        for k in common:
            delta = r1[k][0] - r0[k][0]
            if delta < 0:            # counter wrapped around
                delta += r0[k][1]
            uj += delta
        other = cfg["POWER_OTHER_W"]
        return uj / 1e6 / window + other, "CPU (RAPL) + other" if other else "CPU only (RAPL)"
    idle, peak = cfg["POWER_IDLE_W"], cfg["POWER_MAX_W"]
    load = min(max(cpu_pct, 0.0), 100.0) / 100
    return idle + (peak - idle) * load, "estimated from CPU load"


def money(kwh: float) -> str:
    price = cfg["PRICE_PER_KWH"]
    return f" ≈ {cfg['CURRENCY']}{kwh * price:,.2f}" if price > 0 else ""


def power_text(watts: float, source: str) -> str:
    month_kwh = watts * 24 * 30 / 1000
    return f"~{watts:.0f} W ({source})\nAt this draw: ~{month_kwh:.1f} kWh/month{money(month_kwh)}"


def energy_text(wh: float) -> str:
    return f"{wh / 1000:.3f} kWh since bot start{money(wh / 1000)}"


def collect():
    r0, t0 = read_rapl(), time.monotonic()
    procs, cpu = sample_procs()
    r1, t1 = read_rapl(), time.monotonic()
    power_w, power_src = estimate_power(r0, r1, t1 - t0, cpu)
    temp, temps = read_temps()
    return {
        "procs": procs,
        "cpu": cpu,
        "ram": psutil.virtual_memory(),
        "swap": psutil.swap_memory(),
        "temp": temp,
        "temps": temps,
        "disk": worst_disk(),
        "load": os.getloadavg(),
        "power_w": power_w,
        "power_src": power_src,
    }


# ------------------------------------------------------- heavy "who's to blame" ---
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_proc(p, kind: str) -> str:
    head = f"`{p['name']}` (pid {p['pid']}, {p['user']})"
    if kind == "cpu":
        return f"{head} — {p['cpu']:.1f}% CPU"
    if kind == "ram":
        return f"{head} — {human(p['rss'])} ({p['mem_pct']:.1f}% RAM)"
    if kind == "io":
        return f"{head} — read {human(p['read_bps'])}/s, write {human(p['write_bps'])}/s"
    return head


def top(procs, key, kind, n=5, min_value=0.0):
    ranked = sorted(procs, key=key, reverse=True)
    ranked = [p for p in ranked if key(p) > min_value][:n]
    return "\n".join(fmt_proc(p, kind) for p in ranked) or "nothing notable"


def biggest_dirs(mount: str, n=5) -> str:
    """Largest top-level directories on a mount (stays on one filesystem)."""
    try:
        out = subprocess.run(
            ["du", "-x", "-B1", "--max-depth=1", mount],
            capture_output=True, text=True, timeout=90,
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "du timed out / unavailable"
    rows = []
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        if size.isdigit() and path != mount:
            rows.append((int(size), path))
    rows.sort(reverse=True)
    return "\n".join(f"`{shown(path)}` — {human(size)}" for size, path in rows[:n]) or "unknown"


def nethogs_top(n=5) -> str:
    """Per-process bandwidth via nethogs (optional, needs root)."""
    if not shutil.which("nethogs"):
        return "install `nethogs` (and run as root) to see which process is responsible"
    try:
        out = subprocess.run(
            ["nethogs", "-t", "-c", "4", "-d", "1"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except subprocess.TimeoutExpired:
        return "nethogs timed out"
    block, blocks = [], []
    for line in out.splitlines():
        if line.startswith("Refreshing"):
            if block:
                blocks.append(block)
            block = []
        elif "\t" in line and "/" in line.split("\t")[0]:
            block.append(line)
    if block:
        blocks.append(block)
    if not blocks:
        return "no per-process data"
    rows = []
    for line in blocks[-1]:
        name, sent, recv = (line.split("\t") + ["0", "0"])[:3]
        try:
            rows.append((float(sent) + float(recv), name, float(sent), float(recv)))
        except ValueError:
            continue
    rows.sort(reverse=True)
    lines = []
    for _, name, sent, recv in rows[:n]:
        if sent + recv < 1:
            continue
        # nethogs name looks like "program/pid/uid"
        parts = name.rsplit("/", 2)
        label = f"`{parts[0]}` (pid {parts[1]})" if len(parts) == 3 else f"`{name}`"
        lines.append(f"{label} — ↑ {sent:.0f} KB/s ↓ {recv:.0f} KB/s")
    return "\n".join(lines) or "no significant per-process traffic"


# -------------------------------------------------------------------- bot ---
class WatchBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.streak = defaultdict(int)
        self.last_alert = {}
        self.active = set()
        self.prev_net = None
        self.energy_wh = 0.0       # estimated energy used since the bot started
        self.last_power_t = None
        self._admins = None

    async def setup_hook(self):
        await self.tree.sync()
        self.monitor.change_interval(seconds=cfg["CHECK_INTERVAL"])
        self.monitor.start()

    async def is_admin(self, user) -> bool:
        """Only the bot's owner (or team members) and the DM target may change settings."""
        if self._admins is None:
            ids = {ALERT_USER_ID} if ALERT_USER_ID else set()
            try:
                app = await self.application_info()
                ids.add(app.owner.id)
                if app.team:
                    ids |= {m.id for m in app.team.members}
            except discord.DiscordException:
                log.exception("couldn't look up the bot owner")
            self._admins = ids
        return user.id in self._admins

    async def send(self, **kwargs):
        try:
            if ALERT_CHANNEL_ID:
                dest = self.get_channel(ALERT_CHANNEL_ID) or await self.fetch_channel(ALERT_CHANNEL_ID)
            else:
                dest = await self.fetch_user(ALERT_USER_ID)
            await dest.send(**kwargs)
        except discord.DiscordException:
            log.exception("failed to send message")

    def net_rates(self):
        """Mbit/s (down, up) since the previous call."""
        now, c = time.time(), psutil.net_io_counters()
        prev, self.prev_net = self.prev_net, (now, c)
        if not prev:
            return None
        dt = max(now - prev[0], 0.001)
        down = (c.bytes_recv - prev[1].bytes_recv) * 8 / dt / 1e6
        up = (c.bytes_sent - prev[1].bytes_sent) * 8 / dt / 1e6
        return down, up

    # each check: key -> (breached, title, summary, detail-builder)
    # Checks switched off with /config disable are simply left out.
    def build_checks(self, m, net):
        checks = {}
        if cfg["ENABLE_TEMP"] and m["temp"] is not None:
            limit = cfg["TEMP_LIMIT_C"]
            checks["temp"] = (
                m["temp"] >= limit, "🔥 Server is overheating",
                f"Hottest sensor: **{m['temp']:.0f}°C** (limit {limit:.0f}°C)",
                lambda: {
                    "Sensors": "\n".join(f"{c}/{l}: {t:.0f}°C" for c, l, t in
                                          sorted(m["temps"], key=lambda r: -r[2])[:6]),
                    "Top CPU processes": top(m["procs"], lambda p: p["cpu"], "cpu", min_value=1),
                })
        if cfg["ENABLE_CPU"]:
            limit = cfg["CPU_LIMIT"]
            checks["cpu"] = (
                m["cpu"] >= limit, "⚙️ High CPU usage",
                f"CPU at **{m['cpu']:.0f}%** (limit {limit:.0f}%) · load {m['load'][0]:.2f}/{m['load'][1]:.2f}/{m['load'][2]:.2f}",
                lambda: {"Top CPU processes": top(m["procs"], lambda p: p["cpu"], "cpu", min_value=0.5)})
        if cfg["ENABLE_RAM"]:
            limit = cfg["RAM_LIMIT"]
            checks["ram"] = (
                m["ram"].percent >= limit, "🧠 High RAM usage",
                f"RAM at **{m['ram'].percent:.0f}%** ({human(m['ram'].used)} / {human(m['ram'].total)}), "
                f"swap {m['swap'].percent:.0f}% (limit {limit:.0f}%)",
                lambda: {"Top memory processes": top(m["procs"], lambda p: p["rss"], "ram")})
        if cfg["ENABLE_DISK"] and m["disk"]:
            mount, pct, used, total = m["disk"]
            limit = cfg["DISK_LIMIT"]
            checks["disk"] = (
                pct >= limit, "💾 Disk almost full",
                f"`{shown(mount)}` at **{pct:.0f}%** ({human(used)} / {human(total)}, limit {limit:.0f}%)",
                lambda: {
                    "Biggest directories": biggest_dirs(mount),
                    "Heaviest disk I/O right now": top(
                        m["procs"], lambda p: p["read_bps"] + p["write_bps"], "io", min_value=1024),
                })
        if cfg["ENABLE_NET"] and net:
            down, up = net
            limit = cfg["NET_LIMIT_MBIT"]
            checks["net"] = (
                max(down, up) >= limit, "🌐 High network bandwidth",
                f"↓ **{down:.1f} Mbit/s**  ↑ **{up:.1f} Mbit/s** (limit {limit:g} Mbit/s)",
                lambda: {"Top bandwidth processes": nethogs_top()})
        return checks

    @tasks.loop(seconds=30)
    async def monitor(self):
        try:
            m = await asyncio.to_thread(collect)
        except Exception:
            log.exception("collect failed")
            return
        checks = self.build_checks(m, self.net_rates())
        now = time.time()
        if self.last_power_t is not None:
            self.energy_wh += m["power_w"] * (now - self.last_power_t) / 3600
        self.last_power_t = now

        # forget state for checks that were switched off (or have no data right now)
        for key in list(self.active | set(self.streak)):
            if key not in checks:
                self.active.discard(key)
                self.streak.pop(key, None)

        for key, (breached, title, summary, detail) in checks.items():
            if not breached:
                self.streak[key] = 0
                if key in self.active:
                    self.active.discard(key)
                    await self.send(embed=discord.Embed(
                        title=f"✅ Recovered: {title.split(' ', 1)[1]}",
                        description=summary, colour=discord.Colour.green()))
                continue

            self.streak[key] += 1
            if self.streak[key] < cfg["SUSTAIN_CHECKS"]:
                continue
            if now - self.last_alert.get(key, 0) < cfg["ALERT_COOLDOWN"]:
                continue

            self.last_alert[key] = now
            self.active.add(key)
            fields = await asyncio.to_thread(detail)
            embed = discord.Embed(title=title, description=summary, colour=discord.Colour.red())
            for name, value in fields.items():
                embed.add_field(name=name, value=value[:1024] or "—", inline=False)
            embed.set_footer(text=f"{os.uname().nodename} · ~{m['power_w']:.0f} W")
            await self.send(embed=embed)

    @monitor.before_loop
    async def _wait(self):
        await self.wait_until_ready()
        self.net_rates()  # prime the baseline


bot = WatchBot()


# ---------------------------------------------------------------- commands ---
@bot.tree.command(name="status", description="Snapshot of the server's current health")
async def status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    m = await asyncio.to_thread(collect)
    embed = discord.Embed(title=f"📊 {os.uname().nodename}", colour=discord.Colour.blurple())
    embed.add_field(name="CPU", value=f"{m['cpu']:.0f}% · load {m['load'][0]:.2f}")
    embed.add_field(name="RAM", value=f"{m['ram'].percent:.0f}% ({human(m['ram'].used)})")
    if m["disk"]:
        embed.add_field(name=f"Disk {shown(m['disk'][0])}", value=f"{m['disk'][1]:.0f}%")
    embed.add_field(name="Temp", value=f"{m['temp']:.0f}°C" if m["temp"] is not None else "no sensor")
    embed.add_field(name="Uptime", value=f"{(time.time() - psutil.boot_time()) / 3600:.1f} h")
    embed.add_field(name="⚡ Power", value=power_text(m["power_w"], m["power_src"]), inline=False)
    embed.add_field(name="Energy used", value=energy_text(bot.energy_wh), inline=False)
    embed.add_field(name="Top CPU", value=top(m["procs"], lambda p: p["cpu"], "cpu", n=3, min_value=0.1), inline=False)
    embed.add_field(name="Top RAM", value=top(m["procs"], lambda p: p["rss"], "ram", n=3), inline=False)
    await interaction.followup.send(embed=embed)


config_group = app_commands.Group(name="config", description="View and change server-watch settings")
bot.tree.add_command(config_group)

CHECK_CHOICES = [
    app_commands.Choice(name="Temperature", value="TEMP"),
    app_commands.Choice(name="CPU", value="CPU"),
    app_commands.Choice(name="RAM", value="RAM"),
    app_commands.Choice(name="Disk", value="DISK"),
    app_commands.Choice(name="Network bandwidth", value="NET"),
]
SOURCE_LABEL = {"discord": "✏️ Discord", ".env": "📄 .env", "default": "default"}


def apply_runtime_changes():
    """Push changed settings into running parts of the bot."""
    bot.monitor.change_interval(seconds=cfg["CHECK_INTERVAL"])


async def require_admin(interaction: discord.Interaction) -> bool:
    if await bot.is_admin(interaction.user):
        return True
    await interaction.response.send_message("Only the bot's owner can change settings.", ephemeral=True)
    return False


def save_note(saved: bool) -> str:
    return "" if saved else "\n⚠️ Couldn't save to disk, so this will be lost if the bot restarts."


@config_group.command(name="show", description="Show every setting and where its value comes from")
async def config_show(interaction: discord.Interaction):
    embed = discord.Embed(title="⚙️ Settings", colour=discord.Colour.blurple())
    for group in GROUPS:
        lines = [
            f"`{s.key}` = **{display(s, cfg[s.key])}** · {SOURCE_LABEL[cfg.source(s.key)]}"
            for s in SETTINGS if s.group == group
        ]
        embed.add_field(name=group, value="\n".join(lines), inline=False)
    embed.set_footer(text="Change: /config set · enable · disable · reset. "
                          "Token and IDs live in .env only.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@config_group.command(name="set", description="Change a setting (saved, applies immediately)")
@app_commands.describe(key="Setting name (pick from the list)", value="New value, e.g. 85, on or off")
async def config_set(interaction: discord.Interaction, key: str, value: str):
    if not await require_admin(interaction):
        return
    s = BY_KEY.get(key.strip().upper())
    if not s:
        await interaction.response.send_message(
            f"Unknown setting `{key}`. Pick one from the list, or see `/config show`.", ephemeral=True)
        return
    try:
        new, saved = cfg.set(s.key, value)
    except ValueError as e:
        await interaction.response.send_message(f"`{s.key}` {e}.", ephemeral=True)
        return
    apply_runtime_changes()
    await interaction.response.send_message(
        f"✏️ `{s.key}` set to **{display(s, new)}** by {interaction.user.display_name}{save_note(saved)}")


async def toggle(interaction: discord.Interaction, check: app_commands.Choice[str], on: bool):
    if not await require_admin(interaction):
        return
    _, saved = cfg.set(f"ENABLE_{check.value}", "on" if on else "off")
    state = "✅ Enabled" if on else "⛔ Disabled"
    await interaction.response.send_message(
        f"{state} **{check.name}** alerts (by {interaction.user.display_name}){save_note(saved)}")


@config_group.command(name="enable", description="Turn an alert type on")
@app_commands.describe(check="Which alert")
@app_commands.choices(check=CHECK_CHOICES)
async def config_enable(interaction: discord.Interaction, check: app_commands.Choice[str]):
    await toggle(interaction, check, True)


@config_group.command(name="disable", description="Turn an alert type off")
@app_commands.describe(check="Which alert")
@app_commands.choices(check=CHECK_CHOICES)
async def config_disable(interaction: discord.Interaction, check: app_commands.Choice[str]):
    await toggle(interaction, check, False)


@config_group.command(name="reset", description="Undo changes made from Discord (back to .env / defaults)")
@app_commands.describe(key="Setting to reset; leave empty to reset everything changed from Discord")
async def config_reset(interaction: discord.Interaction, key: Optional[str] = None):
    if not await require_admin(interaction):
        return
    s = None
    if key:
        s = BY_KEY.get(key.strip().upper())
        if not s:
            await interaction.response.send_message(
                f"Unknown setting `{key}`. Pick one from the list, or see `/config show`.", ephemeral=True)
            return
    saved = cfg.reset(s.key if s else None)
    apply_runtime_changes()
    what = f"`{s.key}` is back to **{display(s, cfg[s.key])}** ({SOURCE_LABEL[cfg.source(s.key)]})" \
        if s else "All settings changed from Discord were reset to your .env / defaults"
    await interaction.response.send_message(f"🔄 {what}{save_note(saved)}")


@config_set.autocomplete("key")
@config_reset.autocomplete("key")
async def key_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.strip().lower()
    return [app_commands.Choice(name=s.key, value=s.key)
            for s in SETTINGS if cur in s.key.lower()][:25]


if __name__ == "__main__":
    if not (ALERT_CHANNEL_ID or ALERT_USER_ID):
        raise SystemExit("Set ALERT_CHANNEL_ID or ALERT_USER_ID in .env")
    defaults = [s.key for s in SETTINGS if cfg.source(s.key) == "default"]
    log.info("Settings file: %s", CONFIG_FILE)
    log.info("Using built-in defaults for: %s", ", ".join(defaults) or "nothing (all set)")
    bot.run(TOKEN)
