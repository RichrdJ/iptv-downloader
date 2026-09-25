FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    DOWNLOAD_DIR=/downloads \
    PORT=2233

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gunicorn.conf.py app.py ./
COPY iptv/ iptv/
COPY templates/ templates/
COPY static/ static/

VOLUME ["/config", "/downloads"]
EXPOSE 2233

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz',timeout=4)" || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
