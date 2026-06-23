FROM python:3.12-slim

# Install ffmpeg (handles HLS, TS, MP4, MKV streams)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY downloader.py .

# Config: saved accounts  |  Downloads: output files
VOLUME ["/config", "/downloads"]

ENV CONFIG_DIR=/config
ENV DOWNLOAD_DIR=/downloads

# Run interactively by default
ENTRYPOINT ["python", "downloader.py"]
