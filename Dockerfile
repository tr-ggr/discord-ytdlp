FROM python:3.12-slim

# ffmpeg: MP3 conversion and MP4 merge/remux. deno: JS runtime yt-dlp needs for YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py storage_to.py ./

# /app is root-owned, so the bot user can't create its logs/ dir at runtime; make it here.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/logs \
    && chown bot:bot /app/logs
USER bot

CMD ["python", "bot.py"]
