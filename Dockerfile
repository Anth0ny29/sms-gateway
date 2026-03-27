FROM python:3.11-slim-bookworm

LABEL maintainer="gammu-sms-gateway"
LABEL description="SMS Gateway with python-gammu direct mode"

# Install gammu + build deps for python-gammu
RUN apt-get update && apt-get install -y --no-install-recommends \
    gammu \
    libgammu-dev \
    pkg-config \
    gcc \
    libc6-dev \
    curl \
    usb-modeswitch \
    usb-modeswitch-data \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Create directories
RUN mkdir -p /var/spool/gammu/received \
    && mkdir -p /var/log/gammu

# Copy application
COPY app/ /app/
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -sf http://localhost:5000/api/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
