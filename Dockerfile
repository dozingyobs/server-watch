FROM python:3.12-slim

# nethogs gives per-process bandwidth in alerts; du/coreutils is already in the image
RUN apt-get update \
    && apt-get install -y --no-install-recommends nethogs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor_bot.py .

# Settings changed from Discord (/config) are saved to /data (a mounted volume)
RUN mkdir -p /data
ENV DATA_DIR=/data

CMD ["python", "-u", "monitor_bot.py"]
