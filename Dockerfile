FROM python:3.10-slim

# Install system utilities, native aria2c, and ffmpeg
RUN apt-get update && apt-get install -y \
    aria2 \
    ffmpeg \
    procps \
    psmisc \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Expose default HTTP port
ENV PORT=8000
EXPOSE 8000

# Start application
CMD ["python", "main.py"]
