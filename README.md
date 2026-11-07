# serverwatch

A small Discord bot that watches a headless Debian server and messages you when something is wrong. (made by claude)

**Alerts on:** high temperature, CPU, RAM, disk usage and network bandwidth (download or upload). Each alert lists the processes responsible. `/status` shows a live snapshot, including an estimated power draw.

## How it's deployed

GitHub builds the Docker image whenever you push. The server never builds anything: it just pulls the image and runs it. The server only needs two files, `docker-compose.yml` and `.env`.

## 1. Create the Discord bot

1. [Developer Portal](https://discord.com/developers/applications) → **New Application** → **Bot** tab → **Reset Token**. Copy the token.
2. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`, permissions **Send Messages** + **Embed Links**. Open the URL to add the bot to your server.
3. In Discord: Settings → Advanced → enable **Developer Mode**. Right-click your alerts channel → **Copy Channel ID**.

## 2. Publish the image (once)

1. Push this project to a GitHub repo. Keep `.env` out of it (it holds your token).
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

Then type `/status` in Discord.

## 4. Updating

After you change the code, push to GitHub and wait for the Actions build to finish. Then on the server:

```bash
cd /opt/serverwatch
sudo docker compose pull && sudo docker compose up -d
```

Both commands are needed: `pull` downloads the new image, and `up -d` swaps the running container for it.

To build locally instead of pulling, uncomment `build: .` in `docker-compose.yml` and run `sudo docker compose up -d --build` from the full project folder.

## 5. Settings

Everything is in `.env`. After **any** change, apply it with:

```bash
sudo docker compose up -d --force-recreate
```

(`docker compose restart` does **not** re-read `.env`.)

| Setting | Default | Meaning |
|---|---|---|
| `ALERT_CHANNEL_ID` / `ALERT_USER_ID` | 0 | Channel for alerts, or your user ID for DMs (channel wins) |
| `CPU_LIMIT`, `RAM_LIMIT`, `DISK_LIMIT` | 90 | Percent that triggers an alert |
| `TEMP_LIMIT_C` | 80 | Degrees C |
| `NET_LIMIT_MBIT` | 50 | Mbit/s, checked separately for download and upload |
| `SUSTAIN_CHECKS` | 3 | Consecutive checks over the limit before alerting |
| `CHECK_INTERVAL` | 30 | Seconds between checks |
| `ALERT_COOLDOWN` | 900 | Seconds before the same alert repeats |

## 6. Power estimate

The bot shows estimated watts, kWh used since it started, and a monthly projection.

- If your CPU exposes real readings (Intel/AMD RAPL), it uses those. That only covers the CPU, so set `POWER_OTHER_W` to add a rough figure for the rest of the machine.
- Otherwise it estimates from CPU load: `POWER_IDLE_W` at idle up to `POWER_MAX_W` at 100% CPU. The defaults are generic guesses, so set them for your hardware.
- Set `PRICE_PER_KWH` (and `CURRENCY`) to see an estimated cost.

This is an estimate, not a meter. For exact numbers, use a plug-in power meter or smart plug.

## 7. Test an alert

Temporarily set low limits in `.env`, apply them, and wait about 30 seconds:

```
CPU_LIMIT=5
RAM_LIMIT=1
SUSTAIN_CHECKS=1
ALERT_COOLDOWN=60
```

Generate load with `timeout 120 yes > /dev/null`, or a big download for the bandwidth alert. Put the real values back afterwards.

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
- **`/status` doesn't show up:** wait a couple of minutes, restart Discord, and check the invite had the `applications.commands` scope.
- **No alerts arrive:** look for "failed to send message" in the logs. Usually a wrong ID or missing channel permissions.
- **Temperature says "no sensor":** the hardware doesn't expose one (common on VMs). Everything else still works.
- **Voice warnings (PyNaCl, davey):** harmless, the bot doesn't use voice.

## Security note

The container shares the host's process list and network and mounts the host filesystem read-only, so it can monitor the real machine. It's a privileged tool: keep the token secret and the repo private.
