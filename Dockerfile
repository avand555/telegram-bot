FROM python:3.10-slim

# Install aria2c, ffmpeg, and Linux system tools natively
RUN apt-get update && apt-get install -y \
    aria2 \
    ffmpeg \
    procps \
    psmisc \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Start the bot
CMD ["python", "main.py"]
