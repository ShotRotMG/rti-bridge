FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_PATH=/config/config.yaml

WORKDIR /app

# Dependencies first so code changes don't re-download packages
COPY bridge/requirements.txt .
RUN pip install -r requirements.txt

COPY bridge/ .
# shown by the web UI as a starting point when no config.yaml exists yet
COPY config.example.yaml .

VOLUME ["/config"]
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('WEB_PORT','8088'), timeout=4)" || exit 1
CMD ["python", "rti_ad8x_bridge.py"]
