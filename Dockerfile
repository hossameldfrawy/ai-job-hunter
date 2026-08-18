# =============================================================================
#  AI Job Hunter -- container image
# =============================================================================
#  Runs the bot as a long-lived daemon that hunts on an interval. Use this for
#  Railway / Render / Fly / Oracle Cloud / any VPS or NAS.
#
#  For GitHub Actions you do NOT need this image -- the workflow installs
#  dependencies directly and runs `main.py --once`.
# =============================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Africa/Cairo

WORKDIR /app

# curl is used by the container HEALTHCHECK; ca-certificates for TLS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first: this layer is cached unless requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user, and give it ownership of the state directory so the
# SQLite dedup database is writable.
RUN useradd --create-home --shell /bin/bash hunter \
 && mkdir -p /app/state \
 && chown -R hunter:hunter /app
USER hunter

# Overridden by the platform ($PORT on Render/Railway). When set, main.py
# exposes a JSON health endpoint so the platform can see the worker is alive.
ENV PORT=8080 \
    RUN_INTERVAL_MINUTES=30
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/" > /dev/null || exit 1

# Daemon mode: hunt, sleep, repeat. Handles SIGTERM cleanly so the platform
# can restart it without corrupting the database.
CMD ["python", "main.py", "--daemon"]
