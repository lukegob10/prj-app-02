# syntax=docker/dockerfile:1.7

# Both image references are immutable multi-platform digests. Keep the human-readable tags so
# routine updates are reviewable, then update the digest and uv.lock in the same change.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.6-python3.14-trixie-slim@sha256:d61b872404ed1a0774e2098b5af64c178b59c99be171db6631455262bb0750b4
ARG PYTHON_IMAGE=python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

FROM ${UV_IMAGE} AS builder

ARG TREASURY_ANALYTICS_WHEEL_SHA256

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/app/src
WORKDIR /app

# Pass a directory containing exactly one managed wheel as the named `treasury_analytics`
# build context. Validate and stage its exact bytes before doing any dependency installation.
RUN --mount=type=bind,from=treasury_analytics,source=/,target=/managed \
    set -eu; \
    set -- /managed/treasury_analytics-*.whl; \
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then \
        echo "treasury_analytics build context must contain exactly one treasury_analytics wheel" >&2; \
        exit 64; \
    fi; \
    if [ -z "${TREASURY_ANALYTICS_WHEEL_SHA256}" ]; then \
        echo "TREASURY_ANALYTICS_WHEEL_SHA256 is required" >&2; \
        exit 64; \
    fi; \
    printf '%s  %s\n' "${TREASURY_ANALYTICS_WHEEL_SHA256}" "$1" \
        | sha256sum --check --status; \
    install -d /tmp/managed-wheel; \
    cp "$1" /tmp/managed-wheel/

# Install only locked public runtime dependencies first. treasury-analytics is a separately
# managed artifact and is installed from the staged, verified wheel afterward.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY manage.py ./manage.py

# A development stand-in or incomplete managed API is rejected even if its checksum was supplied.
RUN set -eu; \
    uv pip install --python /opt/venv/bin/python --no-deps \
        /tmp/managed-wheel/treasury_analytics-*.whl; \
    /opt/venv/bin/python -c "import importlib, importlib.metadata as m, sys; module = importlib.import_module('treasury_analytics'); metadata = m.metadata('treasury-analytics'); summary = (metadata.get('Summary') or '').casefold(); invalid = m.version('treasury-analytics') != '0.1.1' or 'stand-in' in summary or 'development-only' in summary or getattr(module, 'AGORA_DEVELOPMENT_STAND_IN', False) or not hasattr(module, 'TAConnection'); sys.exit('managed treasury-analytics wheel must be version 0.1.1 and expose TAConnection') if invalid else None"; \
    uv pip check --python /opt/venv/bin/python; \
    rm -r /tmp/managed-wheel

# ServeStatic serves these immutable files from the portal process. Build-only values satisfy
# settings validation without embedding runtime credentials or contacting Oracle.
RUN AGORA_ENVIRONMENT=production \
    AGORA_DEBUG=false \
    AGORA_PORTAL_SECRET_KEY=build-only-portal-key-00000000000000000000000000000000 \
    AGORA_PORTAL_ORIGIN=https://portal.agora.invalid \
    AGORA_CONTENT_ORIGIN=https://content.agora.invalid \
    AGORA_ARTIFACT_ROOT=/tmp/agora-artifacts \
    AGORA_STATIC_ROOT=/opt/agora-static \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    ENV=PROD \
    TA_PROD_PASSWORD=build-only \
    /opt/venv/bin/python manage.py collectstatic --noinput --clear

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGORA_ARTIFACT_ROOT=/var/lib/agora/artifacts \
    AGORA_STATIC_ROOT=/var/lib/agora/static \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    UVICORN_LIMIT_CONCURRENCY=32

WORKDIR /app
RUN groupadd --system --gid "${APP_GID}" agora \
    && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir /nonexistent --shell /usr/sbin/nologin agora \
    && install -d -m 0700 -o "${APP_UID}" -g "${APP_GID}" /var/lib/agora/artifacts \
    && install -d -m 0755 -o root -g root /var/lib/agora/static

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/manage.py ./manage.py
COPY --from=builder /app/src ./src
COPY --from=builder /opt/agora-static /var/lib/agora/static

USER ${APP_UID}:${APP_GID}
EXPOSE 8000
STOPSIGNAL SIGTERM

CMD ["python", "-m", "uvicorn", "agora.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "30"]
