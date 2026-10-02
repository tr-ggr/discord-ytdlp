import asyncio
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

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

job_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


class UserFacingError(Exception):
    """An error whose message is safe and useful to show in Discord."""


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


def download_media(url: str, out_dir: str, on_phase, max_height: int | None = None) -> dict:
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
        "quiet": True,
        "no_warnings": True,
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

        info = ydl.process_ie_result(info, download=True)

    path = info["requested_downloads"][0]["filepath"]
    if not path.endswith(f".{ext}") or not os.path.exists(path):
        raise UserFacingError(f"Conversion to {ext.upper()} failed. Is ffmpeg installed and on PATH?")
    info["output_path"] = path
    info["output_ext"] = ext
    return info


async def process(url: str, status: discord.Message, max_height: int | None) -> tuple[dict, dict]:
    """Download `url`, upload it to storage.to and return (video info, storage.to file)."""
    loop = asyncio.get_running_loop()

    def on_phase(phase: str) -> None:
        asyncio.run_coroutine_threadsafe(status.edit(content=f"⏳ {phase}…"), loop)

    if job_slots.locked():
        await status.edit(content="🕒 Queued, waiting for a free slot…")

    async with job_slots:
        await status.edit(content="🔎 Looking up video…")
        with tempfile.TemporaryDirectory(prefix="ytbot-") as tmp:
            info = await asyncio.to_thread(download_media, url, tmp, on_phase, max_height)
            await status.edit(content="☁️ Uploading to storage.to…")
            ext = info["output_ext"]
            file = await bot.storage.upload_file(
                info["output_path"],
                safe_filename(info.get("title") or info["id"], ext),
                content_type="video/mp4" if ext == "mp4" else "audio/mpeg",
                expiry_days=STORAGE_TO_EXPIRY_DAYS,
            )
    return info, file


def quality_label(info: dict) -> str:
    if info["output_ext"] == "mp3":
        return "MP3 · 320 kbps"
    codec = (info.get("vcodec") or "").split(".")[0]
    label = f"MP4 · {info.get('height') or '?'}p"
    if info.get("fps"):
        label += f"{round(info['fps'])}"
    return f"{label} · {CODEC_NAMES.get(codec, codec or '?')}"


def result_embed(info: dict, file: dict) -> discord.Embed:
    embed = discord.Embed(
        title=info.get("title", "Download"),
        url=info.get("webpage_url"),
        description=f"**[⬇️ Download {info['output_ext'].upper()}]({file['url']})**",
        color=EMBED_COLOR,
    )
    embed.add_field(name="Duration", value=format_duration(info.get("duration")))
    embed.add_field(name="Size", value=file.get("human_size", "?"))
    embed.add_field(name="Quality", value=quality_label(info))
    if file.get("expiry_days"):
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
    return "Something went wrong. Check the bot's console for details."


async def run_job(ctx: commands.Context, url: str, status: discord.Message, max_height: int | None) -> None:
    try:
        info, file = await process(url.strip("<>"), status, max_height)
    except Exception as exc:
        if not isinstance(exc, (UserFacingError, yt_dlp.utils.YoutubeDLError, StorageToError)):
            bot.dispatch("command_error", ctx, commands.CommandInvokeError(exc))
        await status.edit(content=f"❌ {describe_error(exc)}")
        return
    await status.edit(content=None, embed=result_embed(info, file))


async def run_in_channel(ctx: commands.Context, url: str, max_height: int | None) -> None:
    status = await ctx.reply("🔎 Starting…", mention_author=False)
    await run_job(ctx, url, status, max_height)


async def run_in_dm(ctx: commands.Context, url: str, max_height: int | None, channel_command: str) -> None:
    try:
        status = await ctx.author.send(f"🔎 Starting on <{url.strip('<>')}>…")
    except discord.Forbidden:
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
    print(f"Logged in as {bot.user} (prefix: {PREFIX})")
    await bot.change_presence(activity=discord.Game(name=f"{PREFIX}help"))


@bot.command(name="mp3")
async def mp3(ctx: commands.Context, url: str) -> None:
    """Download a YouTube video as MP3 and post the link here."""
    await run_in_channel(ctx, url, None)


@bot.command(name="dm")
async def dm(ctx: commands.Context, url: str) -> None:
    """Download a YouTube video as MP3 and send the link to your DMs."""
    await run_in_dm(ctx, url, None, "mp3")


@bot.command(name="mp4")
async def mp4(ctx: commands.Context, url: str, quality: parse_quality = DEFAULT_VIDEO_QUALITY) -> None:
    """Download a YouTube video as MP4 and post the link here."""
    await run_in_channel(ctx, url, quality)


@bot.command(name="dmmp4")
async def dmmp4(ctx: commands.Context, url: str, quality: parse_quality = DEFAULT_VIDEO_QUALITY) -> None:
    """Download a YouTube video as MP4 and send the link to your DMs."""
    await run_in_dm(ctx, url, quality, "mp4")


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(title="YouTube Downloader", color=EMBED_COLOR)
    embed.description = (
        "**Audio**\n"
        f"`{PREFIX}mp3 <url>` - 320 kbps MP3, link posted here\n"
        f"`{PREFIX}dm <url>` - same, but the link is sent to your DMs\n\n"
        "**Video**\n"
        f"`{PREFIX}mp4 <url> [quality]` - MP4, link posted here\n"
        f"`{PREFIX}dmmp4 <url> [quality]` - same, but the link is sent to your DMs\n"
        f"Quality: 360, 480, 720, 1080, 1440 (2k), 2160 (4k). Default {DEFAULT_VIDEO_QUALITY}p. "
        "If a video doesn't have that quality, you get the best one below it.\n\n"
        f"`{PREFIX}help` - show this message\n\n"
        f"Files are hosted on storage.to. Max video length: {MAX_DURATION_MINUTES} minutes."
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
        await ctx.reply(
            f"Usage: `{PREFIX}{ctx.command.name} {USAGE.get(ctx.command.name, '')}`", mention_author=False
        )
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply(f"❌ {error}", mention_author=False)
        return
    original = getattr(error, "original", error)
    print(f"Error in command {ctx.command}: {original!r}", file=sys.stderr)
    traceback.print_exception(type(original), original, original.__traceback__)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
