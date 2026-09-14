# syntax=docker/dockerfile:1
#
# Multi-stage build. The builder compiles wheels; the runtime image carries
# only the installed packages, so build toolchains never reach production.

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential and libpq-dev are needed to build psycopg; both stay here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements/ requirements/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements/base.txt

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# libpq5 is the runtime library only — no compiler in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --chown=appuser:appuser . /app

RUN chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /app/staticfiles \
    && chown -R appuser:appuser /app/staticfiles

USER appuser

EXPOSE 8000

# Liveness only — deliberately does not check the database, so a brief DB
# blip does not get the container killed and restarted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]

# Two workers, four threads each. Threads matter here: tool execution is
# I/O-bound (Postgres and the model API), so threads buy real concurrency
# without the memory cost of more processes. The timeout is generous because
# a multi-tool model turn legitimately takes tens of seconds.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "180", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
