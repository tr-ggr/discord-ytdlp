# discord-ytdlp

A Discord bot that downloads YouTube videos with [yt-dlp](https://github.com/yt-dlp/yt-dlp) as a **320 kbps MP3** or an **MP4** (up to 4K). If the file fits under Discord's upload limit, the bot attaches it to its reply. Bigger files are uploaded to [storage.to](https://storage.to) and the bot replies with a download link instead, so size is never a problem. The result can be posted in the channel or sent to your DMs.

Discord's upload limit is 10 MB in DMs and in servers with no boosts or boost level 1, 50 MB at level 2, and 100 MB at level 3. Most MP3s fit; longer or high-resolution videos usually go to storage.to.

## Commands

The prefix is `$yt-`.

| Command | What it does |
| --- | --- |
| `$yt-mp3 <url>` | 320 kbps MP3, posted in the channel |
| `$yt-dm <url>` | Same as above, but sent to your DMs |
| `$yt-mp4 <url> [quality]` | MP4, posted in the channel |
| `$yt-dmmp4 <url> [quality]` | Same as above, but sent to your DMs |
| `$yt-help` | Shows the command list |

**Quality** can be `360`, `480`, `720`, `1080`, `1440` (`2k`), or `2160` (`4k`). A trailing `p` is fine (`720p`). The default is `DEFAULT_VIDEO_QUALITY` (1080). If a video doesn't have the quality you asked for, you get the best one below it.

Example: `$yt-mp4 https://youtu.be/dQw4w9WgXcQ 720`

Limits: single videos only (no playlists, live streams, or premieres). Videos longer than `MAX_DURATION_MINUTES` are refused. At most 2 downloads run at once, and the rest wait in a queue.

## Setup

### 1. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Reset Token** → copy the token.
3. **Bot** tab → under *Privileged Gateway Intents*, turn on **Message Content Intent**. The bot can't see commands without it.
4. **OAuth2 → URL Generator**: tick the `bot` scope and the permissions **Send Messages**, **Embed Links**, **Attach Files**, **Add Reactions**, and **Read Message History**. Without **Attach Files**, every download is sent as a storage.to link. Open the generated URL to invite the bot to your server.

### 2. Get a storage.to API key

Sign in at [storage.to](https://storage.to) and create a personal API token.

### 3. Configure

```sh
cp .env.example .env
```

Then fill in `.env`:

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | yes | | Bot token from the Developer Portal |
| `STORAGE_TO_API_KEY` | yes | | storage.to personal API token, used for files too big to attach |
| `STORAGE_TO_EXPIRY_DAYS` | no | `7` | How long storage.to download links stay alive (1–7 days on a free account) |
| `MAX_DURATION_MINUTES` | no | `90` | Videos longer than this are refused |
| `DEFAULT_VIDEO_QUALITY` | no | `1080` | Default resolution for `mp4` / `dmmp4` |

## Run with Docker Compose (recommended)

The image includes everything the bot needs: Python, ffmpeg, and Deno (yt-dlp needs a JavaScript runtime to download from YouTube). Run all commands from the repo folder.

### Option A: run on every boot

```sh
docker compose up -d --build
```

This starts the bot in the background. The compose file sets `restart: unless-stopped`, so the bot restarts after a crash and after the machine reboots. It stays down only if you stop it yourself.

> **Docker itself has to start on boot**, or the container can't start either:
> - **Windows / macOS (Docker Desktop):** Settings → General → turn on **Start Docker Desktop when you sign in**. Containers start once you log in.
> - **Linux:** `sudo systemctl enable --now docker`

### Option B: run once

```sh
docker compose run --rm --build bot
```

This runs the bot in the foreground with logs in your terminal. **Ctrl+C** stops it, and the container is deleted afterwards. `--rm` overrides the restart policy, so this container never comes back after a reboot.

### Managing the bot

```sh
docker compose logs -f     # follow the logs (Option A)
docker compose ps          # is it running?
docker compose restart     # restart (e.g. after editing .env)
docker compose stop        # stop it; it stays stopped after a reboot
docker compose start       # start it again
docker compose down        # stop and remove the container (turns off run-on-boot)
```

To switch from Option A to Option B, run `docker compose down` first.

### Updating yt-dlp

YouTube changes often, and old yt-dlp versions stop working. If downloads start failing, rebuild the image to get the latest yt-dlp:

```sh
docker compose build --pull --no-cache
docker compose up -d
```

## Run locally (without Docker)

You need Python 3.11+, plus [ffmpeg](https://ffmpeg.org/download.html) and [Deno](https://docs.deno.com/runtime/getting_started/installation/) on your `PATH`.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

When it's running, the console prints `Logged in as <bot name> (prefix: $yt-)`.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Bot is online but ignores commands | Turn on **Message Content Intent** in the Developer Portal (Bot tab) |
| "I can't DM you" | Turn on *Direct Messages* from server members in your privacy settings, or use `$yt-mp3` / `$yt-mp4` |
| "ffmpeg isn't installed" | Local runs only: install ffmpeg and add it to `PATH` (the Docker image already has it) |
| YouTube downloads fail / "Sign in to confirm" | Update yt-dlp (see above). When running locally, make sure Deno is installed |
| "storage.to is rate limiting uploads" | Wait a minute and try again |
| Container exits right away | Check `docker compose logs`. Most likely `DISCORD_TOKEN` or `STORAGE_TO_API_KEY` is missing from `.env` |
