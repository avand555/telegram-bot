FROM python:3.12-slim

# Runtime tools used by the bot:
# - aria2: magnet/torrent downloads
# - ffmpeg: yt-dlp media merging/conversion
# - curl: container healthcheck
# - ca-certificates: HTTPS/TLS verification
RUN apt-get update && apt-get install -y --no-install-recommends \
    aria2 \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so Docker can cache this layer.
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY . .

# Koyeb / container settings.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

# Koyeb health check.
# main.py exposes GET /health without authentication.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# -u makes Telegram/log output appear immediately in container logs.
CMD ["python", "-u", "main.py"]
