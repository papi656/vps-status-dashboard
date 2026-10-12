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

USER dash

VOLUME ["/data"]

# 8099 = public surface (UI + read-only API) — this is the one to route.
# 8098 = ingest surface — publish to host loopback only, never route it.
EXPOSE 8099 8098

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "app.serve"]
