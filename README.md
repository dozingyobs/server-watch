# serverwatch

A small Discord bot that watches a headless Debian server and messages you when something is wrong.
this readme is a guide for me, made by claude.

**Alerts on:** high temperature, CPU, RAM, disk usage and network bandwidth (download or upload). Each alert lists the processes responsible. You can view and change every setting from Discord with `/config`, and `/status` shows a live snapshot including an estimated power draw.

## How it's deployed

GitHub builds the Docker image whenever you push. The server never builds anything: it just pulls the image and runs it. The server only needs two files, `docker-compose.yml` and `.env`.

## 1. Create the Discord bot

1. [Developer Portal](https://discord.com/developers/applications) → **New Application** → **Bot** tab → **Reset Token**. Copy the token.
2. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`, permissions **Send Messages** + **Embed Links**. Open the URL to add the bot to your server.
3. In Discord: Settings → Advanced → enable **Developer Mode**. Right-click your alerts channel → **Copy Channel ID**.

## 2. Publish the image (once)

1. Push this project to a GitHub repo. Keep `.env` and `data/` out of it (the included `.gitignore` does this).
2. In `docker-compose.yml`, replace `YOUR_GITHUB_USERNAME` with your GitHub username, in lowercase.
3. Push to `main`. The workflow in `.github/workflows/docker.yml` builds the image and publishes it to `ghcr.io/<you>/serverwatch:latest`. Watch it finish in the repo's **Actions** tab.
4. **Private repo?** The image is private too. Create a GitHub token with only the `read:packages` permission (Settings → Developer settings → Personal access tokens), then run this on the server:
   ```bash
   echo <token> | sudo docker login ghcr.io -u <your-github-username> --password-stdin
   ```
   Or make the package public in GitHub and skip this step.

## 3. Set up the server

```bash
curl -fsSL https://get.docker.com | sudo sh        # install Docker (once)

sudo mkdir -p /opt/serverwatch && cd /opt/serverwatch
# copy docker-compose.yml and .env.example here, then:
sudo cp .env.example .env
sudo nano .env            # set DISCORD_TOKEN and ALERT_CHANNEL_ID
sudo chmod 600 .env

sudo docker compose pull
sudo docker compose up -d
sudo docker compose logs -f     # Ctrl+C only stops the log view
```

Then type `/status` in Discord. A `data/` folder appears next to `docker-compose.yml`: that's where settings changed from Discord are saved. Don't delete it unless you want to reset them.

## 4. Updating

After you change the code, push to GitHub and wait for the Actions build to finish. Then on the server:

```bash
cd /opt/serverwatch
sudo docker compose pull && sudo docker compose up -d
```

Both commands are needed: `pull` downloads the new image, and `up -d` swaps the running container for it. Your `.env` and your saved settings are kept.

To build locally instead of pulling, uncomment `build: .` in `docker-compose.yml` and run `sudo docker compose up -d --build` from the full project folder.

## 5. Settings: `/config`

You can change settings from Discord, and they apply immediately with no restart:

| Command | What it does |
|---|---|
| `/config show` | Lists every setting and where its value comes from (default, `.env`, or Discord) |
| `/config set <key> <value>` | Changes a setting, e.g. `/config set CPU_LIMIT 85` |
| `/config enable <alert>` / `/config disable <alert>` | Turns an alert type on or off (temperature, CPU, RAM, disk, network) |
| `/config reset [key]` | Undoes changes made from Discord; with no key, resets all of them |

**Which value wins:** changed in Discord > `.env` > built-in default. Changes made in Discord are saved in `data/config.json`, so they survive restarts and updates.

**Who can change settings:** only the bot's owner (the Discord account that owns the application). Anyone in the channel can use `/config show` and `/status`.

**What stays in `.env`:** the token, `ALERT_CHANNEL_ID` and `ALERT_USER_ID`. Those need a container recreate to change.

**When a new version adds settings:** your `.env` doesn't need to change. Every setting has a default, and `/config show` lists everything, including new settings.

If you do edit `.env`, apply it with:

```bash
sudo docker compose up -d --force-recreate
```

(`docker compose restart` does **not** re-read `.env`.) Anything you've changed in Discord keeps winning over `.env` until you run `/config reset`.

| Setting | Default | Meaning |
|---|---|---|
| `ENABLE_TEMP`, `ENABLE_CPU`, `ENABLE_RAM`, `ENABLE_DISK`, `ENABLE_NET` | on | Turn each alert type on or off |
| `CPU_LIMIT`, `RAM_LIMIT`, `DISK_LIMIT` | 90 | Percent that triggers an alert |
| `TEMP_LIMIT_C` | 80 | Degrees C |
| `NET_LIMIT_MBIT` | 50 | Mbit/s, checked separately for download and upload |
| `SUSTAIN_CHECKS` | 3 | Consecutive checks over the limit before alerting |
| `CHECK_INTERVAL` | 30 | Seconds between checks |
| `ALERT_COOLDOWN` | 900 | Seconds before the same alert repeats |
| `POWER_IDLE_W`, `POWER_MAX_W`, `POWER_OTHER_W` | 15 / 60 / 0 | Power estimate, see below |
| `PRICE_PER_KWH`, `CURRENCY` | 0, `$` | Show an estimated electricity cost |

## 6. Power estimate

The bot shows estimated watts, kWh used since it started, and a monthly projection.

- If your CPU exposes real readings (Intel/AMD RAPL), it uses those. That only covers the CPU, so set `POWER_OTHER_W` to add a rough figure for the rest of the machine.
- Otherwise it estimates from CPU load: `POWER_IDLE_W` at idle up to `POWER_MAX_W` at 100% CPU. The defaults are generic guesses, so set them for your hardware.
- Set `PRICE_PER_KWH` (and `CURRENCY`) to see an estimated cost.

This is an estimate, not a meter. For exact numbers, use a plug-in power meter or smart plug.

## 7. Test an alert

From Discord, set low limits and wait about 30 seconds:

```
/config set CPU_LIMIT 5
/config set SUSTAIN_CHECKS 1
/config set ALERT_COOLDOWN 10
```

Generate load on the server with `timeout 120 yes > /dev/null`, or a big download for the bandwidth alert. When you're done, run `/config reset` to put everything back.

## Useful commands

```bash
sudo docker compose ps                 # is it running?
sudo docker compose logs --tail 30     # recent logs
sudo docker compose pull && sudo docker compose up -d   # update
sudo docker compose down               # stop it
```

## Troubleshooting

- **Restarts in a loop with "Set ALERT_CHANNEL_ID or ALERT_USER_ID":** the ID in `.env` is still `0`, or you didn't recreate the container. Check with `sudo grep -n ALERT .env`, then run the recreate command from section 5.
- **`pull` says "unauthorized" or "denied":** the image is private. Run the `docker login ghcr.io` step from section 2, or make the package public.
- **`pull` says "manifest unknown" or "not found":** the Actions build hasn't finished or failed, or the username in `docker-compose.yml` is wrong (it must be lowercase). Check the Actions tab.
- **`/status` or `/config` doesn't show up:** wait a couple of minutes, restart Discord, and check the invite had the `applications.commands` scope.
- **"Only the bot's owner can change settings":** you're not the account that owns the application in the Developer Portal. If you use `ALERT_USER_ID`, that account can change settings too.
- **A setting from Discord is lost after a restart:** the `data/` volume is missing. Make sure `docker-compose.yml` has the `./data:/data` line. The bot says so if it can't save.
- **A change in `.env` has no effect:** you probably changed that setting in Discord earlier. Run `/config show` (it shows the source) and `/config reset <key>`.
- **No alerts arrive:** look for "failed to send message" in the logs. Usually a wrong ID or missing channel permissions.
- **Temperature says "no sensor":** the hardware doesn't expose one (common on VMs). Everything else still works.
- **Voice warnings (PyNaCl, davey):** harmless, the bot doesn't use voice.

## Security note

The container shares the host's process list and network and mounts the host filesystem read-only, so it can monitor the real machine. It's a privileged tool: keep the token secret and the repo private.
