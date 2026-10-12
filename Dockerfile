# RDC Associates Hiring - application image.
#   docker build -t rdc-hiring .
# Normally built and started through docker-compose.yml (see DOCKER.md).

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes don't re-download them. gunicorn is the production server; it
# is not in requirements.txt because the Windows development setup uses Flask's built-in server.
COPY requirements.txt .
RUN pip install -r requirements.txt gunicorn==23.0.0

# Run as an unprivileged user. The folders below hold data that must survive a container rebuild;
# docker-compose.yml mounts a named volume on each. They are created and owned by `app` here so a
# fresh volume inherits that ownership.
RUN useradd --uid 10001 --create-home app \
 && mkdir -p app/static/uploads instance backups truein_dryruns \
 && chown -R app:app /app

COPY --chown=app:app . .

USER app
EXPOSE 5000

# /auth/login answers 200 without a session. Python is used instead of curl (not in the slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:5000/auth/login', timeout=4)"

# ONE worker on purpose. The app keeps per-process state - the Truein/ZingHR employee caches, the
# rate-limit counters, and the nightly-sync / backup / digest threads started in create_app() - so a
# second worker would repeat the 8-minute Truein pull, split the counters and run every scheduled job
# twice. Concurrency comes from threads instead. 300s timeout covers the large Excel exports.
CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", "--threads", "16", \
     "--timeout", "300", "--graceful-timeout", "30", \
     "--access-logfile", "-"]
