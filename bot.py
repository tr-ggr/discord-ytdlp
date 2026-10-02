import asyncio
import itertools
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import aiohttp
import discord
import yt_dlp
from discord.ext import commands
from dotenv import load_dotenv

from storage_to import StorageToClient, StorageToError

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
STORAGE_TO_API_KEY = os.getenv("STORAGE_TO_API_KEY", "").strip()
STORAGE_TO_EXPIRY_DAYS = int(os.getenv("STORAGE_TO_EXPIRY_DAYS", "7"))
MAX_DURATION_MINUTES = int(os.getenv("MAX_DURATION_MINUTES", "90"))
DEFAULT_VIDEO_QUALITY = int(os.getenv("DEFAULT_VIDEO_QUALITY", "1080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

if not DISCORD_TOKEN:
    sys.exit("DISCORD_TOKEN is not set. Add it to your .env file (see .env.example).")
if not STORAGE_TO_API_KEY:
    sys.exit("STORAGE_TO_API_KEY is not set. Add it to your .env file (see .env.example).")

PREFIX = "$yt-"
MAX_CONCURRENT_JOBS = 2
EMBED_COLOR = discord.Color.red()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
VIDEO_QUALITIES = (360, 480, 720, 1080, 1440, 2160)
QUALITY_ALIASES = {"2k": 1440, "4k": 2160}
CODEC_NAMES = {"avc1": "H.264", "vp09": "VP9", "vp9": "VP9", "av01": "AV1"}
# Headroom under Discord's attachment limit for the embed and multipart overhead.
DISCORD_UPLOAD_MARGIN = 256 * 1024
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

job_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
job_counter = itertools.count(1)
log = logging.getLogger("ytbot")


def setup_logging() -> None:
    """Log to the terminal (colored, via discord.py) and to a rotating logs/bot.log file."""
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    # Adds a stderr handler to the root logger, so every logger below shows up in the terminal.
    discord.utils.setup_logging(level=level, root=True)

    os.makedirs(LOG_DIR, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)

    # discord.py's gateway/HTTP and asyncio's debug output drown everything else out.
    for noisy in ("discord", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))


class UserFacingError(Exception):
    """An error whose message is safe and useful to show in Discord."""


class YtDlpLogger:
    """Routes yt-dlp's output into our logs, tagged with the job number."""

    def __init__(self, job_id: int):
        self._log = logging.getLogger("ytbot.yt_dlp")
        self._prefix = f"job #{job_id}: "

    def debug(self, msg: str) -> None:
        # yt-dlp sends both debug and info output here; info lines have no "[debug] " prefix.
        self._log.debug(self._prefix + msg.removeprefix("[debug] "))

    def info(self, msg: str) -> None:
        self._log.debug(self._prefix + msg)

    def warning(self, msg: str) -> None:
        self._log.warning(self._prefix + ANSI_RE.sub("", msg))

    def error(self, msg: str) -> None:
        # Errors are also raised as exceptions and logged by run_job, so keep this quiet.
        self._log.debug(self._prefix + ANSI_RE.sub("", msg))


class YtBot(commands.Bot):
    http_session: aiohttp.ClientSession
    storage: StorageToClient

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.storage = StorageToClient(self.http_session, STORAGE_TO_API_KEY)

    async def close(self) -> None:
        if hasattr(self, "http_session"):
            await self.http_session.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = YtBot(command_prefix=PREFIX, intents=intents, help_command=None)


def parse_quality(argument: str) -> int:
    """Converter for the optional video quality argument: 720, 1080p, 4k, ..."""
    value = argument.lower().removesuffix("p")
    height = QUALITY_ALIASES.get(value) or (int(value) if value.isdigit() else None)
    if height not in VIDEO_QUALITIES:
        options = ", ".join(f"{q}p" for q in VIDEO_QUALITIES)
        raise commands.BadArgument(f"Unknown quality `{argument}`. Use one of: {options} (or 2k / 4k).")
    return height


def safe_filename(title: str, ext: str) -> str:
    name = INVALID_FILENAME_CHARS.sub("", title).strip().rstrip(".") or "download"
    return f"{name[:200]}.{ext}"


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def audio_options() -> dict:
    return {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }


def video_options(max_height: int) -> dict:
    return {
        # Best video at or below the requested height; fall back to anything if nothing qualifies.
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
        # Highest resolution first, then prefer H.264 + AAC so the MP4 plays everywhere.
        "format_sort": ["res", "vcodec:h264", "fps", "acodec:aac"],
        "merge_output_format": "mp4",
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }


def download_media(job_id: int, url: str, out_dir: str, on_phase, max_height: int | None = None) -> dict:
    """Blocking: probe the URL, enforce limits, then download as MP3 (max_height=None) or MP4."""
    ext = "mp4" if max_height else "mp3"
    phases_seen = set()

    def report(phase: str) -> None:
        if phase not in phases_seen:
            phases_seen.add(phase)
            on_phase(phase)

    def progress_hook(d: dict) -> None:
        if d.get("status") == "downloading":
            report("Downloading")

    def postprocessor_hook(d: dict) -> None:
        if d.get("status") != "started":
            return
        if d.get("postprocessor") == "ExtractAudio":
            report("Converting to MP3 (320 kbps)")
        elif d.get("postprocessor") == "Merger":
            report("Merging video and audio")

    ydl_opts = {
        "noplaylist": True,
        "logger": YtDlpLogger(job_id),
        "quiet": True,
        "noprogress": True,
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        **(video_options(max_height) if max_height else audio_options()),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            raise UserFacingError("That's a playlist. Send a link to a single video.")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise UserFacingError("Live streams and premieres can't be downloaded.")
        duration = info.get("duration") or 0
        if duration > MAX_DURATION_MINUTES * 60:
            raise UserFacingError(
                f"That video is {format_duration(duration)} long; the limit is {MAX_DURATION_MINUTES} minutes."
            )
        log.info(
            "job #%d: found %r by %s (%s)",
            job_id, info.get("title"), info.get("uploader"), format_duration(duration),
        )

        started = time.perf_counter()
        info = ydl.process_ie_result(info, download=True)

    path = info["requested_downloads"][0]["filepath"]
    if not path.endswith(f".{ext}") or not os.path.exists(path):
        raise UserFacingError(f"Conversion to {ext.upper()} failed. Is ffmpeg installed and on PATH?")
    info["output_path"] = path
    info["output_ext"] = ext
    log.info(
        "job #%d: downloaded and converted in %.1fs (format %s, %.1f MB)",
        job_id, time.perf_counter() - started, info.get("format_id"), os.path.getsize(path) / 1e6,
    )
    return info


async def edit_status(status: discord.Message, **fields) -> None:
    """Edit the progress message, without failing the job if it was deleted or can't be edited."""
    try:
        await status.edit(**fields)
    except discord.HTTPException as exc:
        log.warning("Couldn't update status message %s: %s", status.id, exc)


def upload_limit(status: discord.Message) -> int | None:
    """Largest file we can attach where `status` lives, or None if we can't attach files there."""
    if status.guild is None:
        limit = discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
    elif status.channel.permissions_for(status.guild.me).attach_files:
        limit = status.guild.filesize_limit
    else:
        return None
    return limit - DISCORD_UPLOAD_MARGIN


async def attach_to_discord(job_id: int, status: discord.Message, info: dict, size: int, filename: str) -> bool:
    """Attach the file to the status message. False if Discord rejected it."""
    await edit_status(status, content="📎 Uploading to Discord…")
    try:
        await status.edit(
            content=None,
            embed=result_embed(info, size),
            attachments=[discord.File(info["output_path"], filename=filename)],
        )
    except discord.HTTPException as exc:
        log.warning(
            "job #%d: Discord rejected the %s attachment, using storage.to: %s", job_id, format_size(size), exc
        )
        return False
    return True


async def process(job_id: int, url: str, status: discord.Message, max_height: int | None) -> tuple[dict, int, str]:
    """Download `url` and deliver it: attached to `status` if it fits, otherwise as a storage.to link.

    Returns (video info, file size, where it went).
    """
    loop = asyncio.get_running_loop()

    def on_phase(phase: str) -> None:
        log.debug("job #%d: %s", job_id, phase)
        asyncio.run_coroutine_threadsafe(edit_status(status, content=f"⏳ {phase}…"), loop)

    if job_slots.locked():
        log.info("job #%d: queued, %d jobs already running", job_id, MAX_CONCURRENT_JOBS)
        await edit_status(status, content="🕒 Queued, waiting for a free slot…")

    async with job_slots:
        await edit_status(status, content="🔎 Looking up video…")
        with tempfile.TemporaryDirectory(prefix="ytbot-") as tmp:
            info = await asyncio.to_thread(download_media, job_id, url, tmp, on_phase, max_height)
            ext = info["output_ext"]
            filename = safe_filename(info.get("title") or info["id"], ext)
            size = os.path.getsize(info["output_path"])

            limit = upload_limit(status)
            if limit is None:
                log.info("job #%d: no Attach Files permission here, using storage.to", job_id)
            elif size > limit:
                log.info(
                    "job #%d: %s is over the %s attachment limit, using storage.to",
                    job_id, format_size(size), format_size(limit),
                )
            elif await attach_to_discord(job_id, status, info, size, filename):
                return info, size, "Discord attachment"

            await edit_status(status, content="☁️ Uploading to storage.to…")
            file = await bot.storage.upload_file(
                info["output_path"],
                filename,
                content_type="video/mp4" if ext == "mp4" else "audio/mpeg",
                expiry_days=STORAGE_TO_EXPIRY_DAYS,
            )
            await edit_status(status, content=None, embed=result_embed(info, size, file))
    return info, size, file["url"]


def quality_label(info: dict) -> str:
    if info["output_ext"] == "mp3":
        return "MP3 · 320 kbps"
    codec = (info.get("vcodec") or "").split(".")[0]
    label = f"MP4 · {info.get('height') or '?'}p"
    if info.get("fps"):
        label += f"{round(info['fps'])}"
    return f"{label} · {CODEC_NAMES.get(codec, codec or '?')}"


def result_embed(info: dict, size: int, file: dict | None = None) -> discord.Embed:
    """Result card; `file` is the storage.to upload, or None when the file is attached to the message."""
    ext = info["output_ext"].upper()
    embed = discord.Embed(
        title=info.get("title", "Download"),
        url=info.get("webpage_url"),
        description=f"**[⬇️ Download {ext}]({file['url']})**" if file else f"📎 {ext} attached",
        color=EMBED_COLOR,
    )
    embed.add_field(name="Duration", value=format_duration(info.get("duration")))
    embed.add_field(name="Size", value=format_size(size))
    embed.add_field(name="Quality", value=quality_label(info))
    if file is None:
        expires = None
    elif file.get("expiry_days"):
        # The confirm response predates our expiry change, so derive it ourselves.
        expires = discord.utils.utcnow() + timedelta(days=file["expiry_days"])
    elif file.get("expires_at"):
        expires = datetime.fromisoformat(file["expires_at"].replace("Z", "+00:00"))
    else:
        expires = None
    if expires:
        embed.add_field(name="Link expires", value=discord.utils.format_dt(expires, "R"), inline=False)
    if info.get("thumbnail"):
        embed.set_thumbnail(url=info["thumbnail"])
    if info.get("uploader"):
        embed.set_author(name=info["uploader"])
    return embed


def describe_error(exc: Exception) -> str:
    if isinstance(exc, UserFacingError):
        return str(exc)
    if isinstance(exc, yt_dlp.utils.YoutubeDLError):
        message = ANSI_RE.sub("", str(exc)).removeprefix("ERROR: ")
        if "ffmpeg" in message.lower() or "ffprobe" in message.lower():
            return "ffmpeg isn't installed or isn't on PATH on the bot's machine."
        return f"Couldn't download that video: {message[:300]}"
    if isinstance(exc, StorageToError):
        if exc.status == 429:
            return "storage.to is rate limiting uploads right now. Try again in a minute."
        return f"Upload to storage.to failed: {exc}"
    return "Something went wrong. Check the bot's logs for details."


def describe_origin(ctx: commands.Context) -> str:
    where = f"#{ctx.channel} in {ctx.guild} ({ctx.guild.id})" if ctx.guild else "DMs"
    return f"{ctx.author} ({ctx.author.id}) in {where}"


async def run_job(ctx: commands.Context, url: str, status: discord.Message, max_height: int | None) -> None:
    job_id = next(job_counter)
    url = url.strip("<>")
    target = f"MP4 up to {max_height}p" if max_height else "MP3"
    delivery = "DM" if isinstance(status.channel, discord.DMChannel) else "channel"
    log.info("job #%d: %s requested %s of %s (delivery: %s)", job_id, describe_origin(ctx), target, url, delivery)

    started = time.perf_counter()
    try:
        info, size, destination = await process(job_id, url, status, max_height)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        if isinstance(exc, (UserFacingError, yt_dlp.utils.YoutubeDLError, StorageToError)):
            reason = ANSI_RE.sub("", str(exc)).removeprefix("ERROR: ")
            log.warning("job #%d: failed after %.1fs: %s", job_id, elapsed, reason)
        else:
            log.exception("job #%d: crashed after %.1fs", job_id, elapsed)
        await edit_status(status, content=f"❌ {describe_error(exc)}")
        return

    log.info(
        "job #%d: done in %.1fs - %s, %s -> %s",
        job_id, time.perf_counter() - started, quality_label(info), format_size(size), destination,
    )


async def run_in_channel(ctx: commands.Context, url: str, max_height: int | None) -> None:
    status = await ctx.reply("🔎 Starting…", mention_author=False)
    await run_job(ctx, url, status, max_height)


async def run_in_dm(ctx: commands.Context, url: str, max_height: int | None, channel_command: str) -> None:
    try:
        status = await ctx.author.send(f"🔎 Starting on <{url.strip('<>')}>…")
    except discord.Forbidden:
        log.info("Can't DM %s, their DMs are closed", describe_origin(ctx))
        await ctx.reply(
            "❌ I can't DM you. Enable **Direct Messages** from server members in your privacy settings, "
            f"or use `{PREFIX}{channel_command}` instead.",
            mention_author=False,
        )
        return

    if ctx.guild is not None:
        await ctx.message.add_reaction("📬")
    await run_job(ctx, url, status, max_height)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s), in %d servers, prefix %r", bot.user, bot.user.id, len(bot.guilds), PREFIX)
    log.info(
        "Settings: links expire after %d days, max length %d min, default video %dp, %d jobs at a time",
        STORAGE_TO_EXPIRY_DAYS, MAX_DURATION_MINUTES, DEFAULT_VIDEO_QUALITY, MAX_CONCURRENT_JOBS,
    )
    await bot.change_presence(activity=discord.Game(name=f"{PREFIX}help"))


@bot.command(name="mp3")
async def mp3(ctx: commands.Context, url: str) -> None:
    """Download a YouTube video as MP3 and post it here."""
    await run_in_channel(ctx, url, None)


@bot.command(name="dm")
async def dm(ctx: commands.Context, url: str) -> None:
    """Download a YouTube video as MP3 and send it to your DMs."""
    await run_in_dm(ctx, url, None, "mp3")


@bot.command(name="mp4")
async def mp4(ctx: commands.Context, url: str, quality: parse_quality = DEFAULT_VIDEO_QUALITY) -> None:
    """Download a YouTube video as MP4 and post it here."""
    await run_in_channel(ctx, url, quality)


@bot.command(name="dmmp4")
async def dmmp4(ctx: commands.Context, url: str, quality: parse_quality = DEFAULT_VIDEO_QUALITY) -> None:
    """Download a YouTube video as MP4 and send it to your DMs."""
    await run_in_dm(ctx, url, quality, "mp4")


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(title="YouTube Downloader", color=EMBED_COLOR)
    embed.description = (
        "**Audio**\n"
        f"`{PREFIX}mp3 <url>` - 320 kbps MP3, posted here\n"
        f"`{PREFIX}dm <url>` - same, but sent to your DMs\n\n"
        "**Video**\n"
        f"`{PREFIX}mp4 <url> [quality]` - MP4, posted here\n"
        f"`{PREFIX}dmmp4 <url> [quality]` - same, but sent to your DMs\n"
        f"Quality: 360, 480, 720, 1080, 1440 (2k), 2160 (4k). Default {DEFAULT_VIDEO_QUALITY}p. "
        "If a video doesn't have that quality, you get the best one below it.\n\n"
        f"`{PREFIX}help` - show this message\n\n"
        "Files that fit under Discord's upload limit are attached directly; bigger ones are "
        f"linked from storage.to. Max video length: {MAX_DURATION_MINUTES} minutes."
    )
    await ctx.reply(embed=embed, mention_author=False)


USAGE = {
    "mp3": "<youtube url>",
    "dm": "<youtube url>",
    "mp4": "<youtube url> [quality]",
    "dmmp4": "<youtube url> [quality]",
}


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        log.info("%s used %s without a URL", describe_origin(ctx), ctx.command.name)
        await ctx.reply(
            f"Usage: `{PREFIX}{ctx.command.name} {USAGE.get(ctx.command.name, '')}`", mention_author=False
        )
        return
    if isinstance(error, commands.BadArgument):
        log.info("%s gave a bad argument to %s: %s", describe_origin(ctx), ctx.command.name, error)
        await ctx.reply(f"❌ {error}", mention_author=False)
        return
    original = getattr(error, "original", error)
    log.error("Unhandled error in %s from %s", ctx.command, describe_origin(ctx), exc_info=original)


if __name__ == "__main__":
    setup_logging()
    bot.run(DISCORD_TOKEN, log_handler=None)
