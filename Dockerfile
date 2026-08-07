FROM python:3.10-slim

# Install core Linux tools natively
RUN apt-get update && apt-get install -y \
    aria2 \
    ffmpeg \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your bot code
COPY . .

# Expose Port for Koyeb Health Checks
ENV PORT=8000
EXPOSE 8000

# Start the Bot
CMD ["python", "main.py"]
