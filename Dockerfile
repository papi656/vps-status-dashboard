FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY public ./public

# Run as an unprivileged user. /data must be writable by this uid — Docker-managed
# volumes inherit the ownership below; a bind mount needs it set on the host.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin dash \
    && mkdir -p /data \
    && chown -R dash:dash /app /data

# Point the DB at the volume. The app's fallback is ROOT-relative (ROOT = /app), so without
# this the database lands in /app/data inside the container's writable layer and is wiped on
# every redeploy — declaring VOLUME ["/data"] is not enough on its own.
ENV DATA_DIR=/data

USER dash

VOLUME ["/data"]

# 8098 = public surface (UI + read-only API) — this is the one to route.
# 8099 = ingest surface — publish to host loopback only, never route it.
#        8099 matches the serverStatus agent's default ENDPOINT.
EXPOSE 8098 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; p = os.environ.get('PUBLIC_PORT', '8098'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "app.serve"]
