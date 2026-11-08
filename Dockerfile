# Two targets from one build:
#   standalone (default): Docker Compose, runs as an unprivileged user.
#   app: Home Assistant App image; runs as root because the Supervisor's /data and the HA config folder are root-owned.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /data
COPY app ./app
EXPOSE 7380
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7380/healthz', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7380", "--proxy-headers"]

FROM base AS app
ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64
LABEL io.hass.type="addon" io.hass.version="${BUILD_VERSION}" io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="Assist Request Tuner (Home Assistant App)"

FROM base AS standalone
RUN groupadd -g 10001 tuner && useradd -u 10001 -g 10001 -M tuner && chown 10001:10001 /data
USER 10001:10001
LABEL org.opencontainers.image.title="Assist Request Tuner"
