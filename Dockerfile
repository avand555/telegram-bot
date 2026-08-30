FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        wget \
        ca-certificates \
        unzip \
        tar \
        aria2 \
        curl && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get clean

# Install aria2c if not available
RUN if ! command -v aria2c &> /dev/null; then \
        wget -q https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-linux-gnu-64bit-build1.tar.bz2 && \
        tar -xjf aria2-1.37.0-linux-gnu-64bit-build1.tar.bz2 && \
        mv aria2-1.37.0-linux-gnu-64bit-build1/aria2c /usr/local/bin/ && \
        chmod +x /usr/local/bin/aria2c && \
        rm -rf aria2-1.37.0-linux-gnu-64bit-build1* ; \
    fi

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "main.py"]
