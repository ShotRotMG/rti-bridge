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

VOLUME ["/config"]
CMD ["python", "rti_ad8x_bridge.py"]
