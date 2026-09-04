FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv

FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime-base

ARG APA_UID=10001
ARG APA_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN groupadd --gid "${APA_GID}" apa \
    && useradd --uid "${APA_UID}" --gid "${APA_GID}" --no-create-home --shell /usr/sbin/nologin apa

COPY --from=uv /uv /usr/local/bin/uv

FROM runtime-base AS market-data

COPY --chown=apa:apa pyproject.toml uv.lock README.md ./
RUN uv sync --locked --only-group market-data-runtime --no-install-project

COPY --chown=apa:apa src/adaptive_trader/__init__.py ./src/adaptive_trader/__init__.py
COPY --chown=apa:apa src/adaptive_trader/collection ./src/adaptive_trader/collection
COPY --chown=apa:apa alembic.ini ./alembic.ini
COPY --chown=apa:apa migrations ./migrations

USER apa

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "adaptive_trader.collection", "ready"]

CMD ["python", "-m", "adaptive_trader.collection", "run"]

FROM runtime-base AS application

COPY --chown=apa:apa pyproject.toml uv.lock README.md ./
COPY --chown=apa:apa src ./src
RUN uv sync --locked --no-dev --extra dashboard

COPY --chown=apa:apa alembic.ini ./alembic.ini
COPY --chown=apa:apa migrations ./migrations
COPY --chown=apa:apa app.py ./app.py
COPY --chown=apa:apa configs ./configs
COPY --chown=apa:apa docs ./docs
COPY --chown=apa:apa scripts ./scripts
RUN install -d -o apa -g apa /app/data/cache /app/runtime /app/outputs

USER apa

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python", "-m", "adaptive_trader.cli", "status", "--config", "configs/observer.yaml"]

CMD ["python", "-m", "adaptive_trader.cli", "observe", "--config", "configs/observer.yaml"]
