# syntax=docker/dockerfile:1

# ── Builder ───────────────────────────────────────────────────────────────────
# Installs the runtime dependencies into an isolated virtualenv so the final
# image carries only what is needed to run (no build toolchain, no pip cache).
# Pinned to Python 3.12 to match the CI/lint target (ruff target-version py312),
# so the image runs on the same interpreter version tests run against.
FROM python:3.14-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Runtime ───────────────────────────────────────────────────────────────────
# Slim image running as a non-root user. Only the virtualenv and the application
# source are copied in. Python 3.12 matches the CI/lint target.
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root user pinned to UID/GID 1000 (the typical default on single-user Linux
# hosts) so a bind-mounted ./data owned by the host user is writable out of the
# box. It owns /app and /data.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home-dir /app --no-create-home app \
    && mkdir -p /app /data \
    && chown -R app:app /app /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app tr_bridge ./tr_bridge
COPY --chown=app:app VERSION ./VERSION

USER app

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["uvicorn", "tr_bridge.main:app", "--host", "0.0.0.0", "--port", "8000"]
