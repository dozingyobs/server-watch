#!/usr/bin/env python3
"""
Discord server-watch bot for a headless Debian box.

Alerts (with the offending processes) when:
  - temperature is too high
  - CPU, RAM or disk usage is too high
  - network bandwidth is too high

Also provides a /status slash command for an on-demand snapshot.
Configuration is read from environment variables / a .env file.
"""
import asyncio
import logging
import os
import shutil
import subprocess
import time
from collections import defaultdict

import discord
import psutil
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------- config ---
TOKEN = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))  # post in a channel...
ALERT_USER_ID = int(os.getenv("ALERT_USER_ID", "0"))        # ...or DM this user

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))      # seconds between checks
SUSTAIN_CHECKS = int(os.getenv("SUSTAIN_CHECKS", "3"))       # consecutive breaches before alerting
COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "900"))           # seconds between repeat alerts

TEMP_LIMIT = float(os.getenv("TEMP_LIMIT_C", "80"))          # degrees C
CPU_LIMIT = float(os.getenv("CPU_LIMIT", "90"))              # %
RAM_LIMIT = float(os.getenv("RAM_LIMIT", "90"))              # %
DISK_LIMIT = float(os.getenv("DISK_LIMIT", "90"))            # % used
NET_LIMIT_MBIT = float(os.getenv("NET_LIMIT_MBIT", "50"))    # Mbit/s, up or down

# Power estimate (used when the CPU doesn't expose real power readings)
POWER_IDLE_W = float(os.getenv("POWER_IDLE_W", "15"))        # whole-server draw at idle
POWER_MAX_W = float(os.getenv("POWER_MAX_W", "60"))          # whole-server draw at 100% CPU
POWER_OTHER_W = float(os.getenv("POWER_OTHER_W", "0"))       # extra watts to add to a CPU-only reading
PRICE_PER_KWH = float(os.getenv("PRICE_PER_KWH", "0"))       # 0 = don't show cost
CURRENCY = os.getenv("CURRENCY", "$")

# When running in Docker, the host's root filesystem is bind-mounted here (e.g. /host)
HOST_ROOT = os.getenv("HOST_ROOT", "").rstrip("/")

IGNORED_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "iso9660", "fuse.lxcfs"}
NCPU = psutil.cpu_count() or 1


def shown(path: str) -> str:
    """Path as the host sees it (strips the container's HOST_ROOT prefix)."""
    if HOST_ROOT and path.startswith(HOST_ROOT):
        return path[len(HOST_ROOT):] or "/"
    return path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("serverwatch")


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
        watts = uj / 1e6 / window + POWER_OTHER_W
        return watts, "CPU (RAPL) + other" if POWER_OTHER_W else "CPU only (RAPL)"
    load = min(max(cpu_pct, 0.0), 100.0) / 100
    return POWER_IDLE_W + (POWER_MAX_W - POWER_IDLE_W) * load, "estimated from CPU load"


def money(kwh: float) -> str:
    return f" ≈ {CURRENCY}{kwh * PRICE_PER_KWH:,.2f}" if PRICE_PER_KWH > 0 else ""


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

    async def setup_hook(self):
        await self.tree.sync()
        self.monitor.change_interval(seconds=CHECK_INTERVAL)
        self.monitor.start()

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
    def build_checks(self, m, net):
        checks = {}
        if m["temp"] is not None:
            checks["temp"] = (
                m["temp"] >= TEMP_LIMIT, "🔥 Server is overheating",
                f"Hottest sensor: **{m['temp']:.0f}°C** (limit {TEMP_LIMIT:.0f}°C)",
                lambda: {
                    "Sensors": "\n".join(f"{c}/{l}: {t:.0f}°C" for c, l, t in
                                          sorted(m["temps"], key=lambda r: -r[2])[:6]),
                    "Top CPU processes": top(m["procs"], lambda p: p["cpu"], "cpu", min_value=1),
                })
        checks["cpu"] = (
            m["cpu"] >= CPU_LIMIT, "⚙️ High CPU usage",
            f"CPU at **{m['cpu']:.0f}%** (limit {CPU_LIMIT:.0f}%) · load {m['load'][0]:.2f}/{m['load'][1]:.2f}/{m['load'][2]:.2f}",
            lambda: {"Top CPU processes": top(m["procs"], lambda p: p["cpu"], "cpu", min_value=0.5)})
        checks["ram"] = (
            m["ram"].percent >= RAM_LIMIT, "🧠 High RAM usage",
            f"RAM at **{m['ram'].percent:.0f}%** ({human(m['ram'].used)} / {human(m['ram'].total)}), "
            f"swap {m['swap'].percent:.0f}%",
            lambda: {"Top memory processes": top(m["procs"], lambda p: p["rss"], "ram")})
        if m["disk"]:
            mount, pct, used, total = m["disk"]
            checks["disk"] = (
                pct >= DISK_LIMIT, "💾 Disk almost full",
                f"`{shown(mount)}` at **{pct:.0f}%** ({human(used)} / {human(total)})",
                lambda: {
                    "Biggest directories": biggest_dirs(mount),
                    "Heaviest disk I/O right now": top(
                        m["procs"], lambda p: p["read_bps"] + p["write_bps"], "io", min_value=1024),
                })
        if net:
            down, up = net
            checks["net"] = (
                max(down, up) >= NET_LIMIT_MBIT, "🌐 High network bandwidth",
                f"↓ **{down:.1f} Mbit/s**  ↑ **{up:.1f} Mbit/s** (limit {NET_LIMIT_MBIT:.0f} Mbit/s)",
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
            if self.streak[key] < SUSTAIN_CHECKS:
                continue
            if now - self.last_alert.get(key, 0) < COOLDOWN:
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


if __name__ == "__main__":
    if not (ALERT_CHANNEL_ID or ALERT_USER_ID):
        raise SystemExit("Set ALERT_CHANNEL_ID or ALERT_USER_ID in .env")
    bot.run(TOKEN)
