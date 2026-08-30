FROM python:3.12-slim

# Install system dependencies (aria2, ffmpeg, curl for health checks)
RUN apt-get update && apt-get install -y \
    aria2 \
    ffmpeg \
    wget \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose Koyeb's expected port
ENV PORT=8000
EXPOSE 8000

# Docker Healthcheck (Pings the /health route in main.py)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Start the bot
CMD ["python", "main.py"]
