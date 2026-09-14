FROM ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv

# Wolfi provides maintained glibc packages compatible with manylinux wheels.
# Keep Python 3.11 and SQLite's patched release explicit; signed APK dependencies
# are inventoried and scanned in every final image.
FROM cgr.dev/chainguard/wolfi-base@sha256:918a593b8268c222afd4e2c4f06860ac984e60719b4697e4c71d796bc8fcd042 AS python-base
RUN apk add --no-cache python-3.11=3.11.16-r5 sqlite-libs=3.53.4-r2

FROM python-base AS platform-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev --extra dashboard --no-editable

FROM python-base AS runtime-base

# Installers belong only to builders. Remove the base image's unused global
# tooling (including its vendored libraries), leaving the locked venv untouched.
RUN rm -rf /usr/lib/python3.11/ensurepip /usr/share/python-wheels

ARG AQA_UID=10001
ARG AQA_GID=10001

ENV HOME=/tmp \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup -g "${AQA_GID}" aqa \
    && adduser -D -H -u "${AQA_UID}" -G aqa -s /sbin/nologin aqa \
    && chown "${AQA_UID}:${AQA_GID}" /app \
    && chmod 0555 /app \
    && install -d -o "${AQA_UID}" -g "${AQA_GID}" -m 0750 \
        /app/outputs /app/outputs/artifacts /app/runtime \
    && install -d -o "${AQA_UID}" -g "${AQA_GID}" -m 0700 \
        /app/runtime/dashboard-auth /app/secrets

FROM runtime-base AS platform

ARG AQA_SOURCE_URL=https://github.com/yudhiishvs/autonomous-quant-agent
ARG AQA_VCS_REF=unknown
ARG AQA_VERSION=0.1.0

LABEL org.opencontainers.image.source="${AQA_SOURCE_URL}" \
      org.opencontainers.image.revision="${AQA_VCS_REF}" \
      org.opencontainers.image.version="${AQA_VERSION}" \
      org.opencontainers.image.title="Autonomous Quant Agent" \
      org.opencontainers.image.description="Self-hosted paper-only quantitative platform"

COPY --from=platform-builder /app/.venv /app/.venv
COPY alembic.ini ./alembic.ini
COPY app.py ./app.py
COPY configs ./configs
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
COPY docker ./docker
COPY pyproject.toml ./pyproject.toml
COPY uv.lock ./uv.lock
RUN python /app/docker/write_build_provenance.py --root /app --revision "${AQA_VCS_REF}"

USER 10001:10001

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["aqa", "doctor"]

FROM platform AS application

FROM python-base AS market-data-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --only-group market-data-runtime --no-install-project

FROM runtime-base AS market-data

ENV PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt

ARG AQA_SOURCE_URL=https://github.com/yudhiishvs/autonomous-quant-agent
ARG AQA_VCS_REF=unknown
ARG AQA_VERSION=0.1.0

LABEL org.opencontainers.image.source="${AQA_SOURCE_URL}" \
      org.opencontainers.image.revision="${AQA_VCS_REF}" \
      org.opencontainers.image.version="${AQA_VERSION}" \
      org.opencontainers.image.title="Autonomous Quant Agent market-data collector" \
      org.opencontainers.image.description="Data-only Alpaca IEX collector without a trading SDK"

COPY --from=market-data-builder /app/.venv /app/.venv
COPY src/adaptive_trader/__init__.py ./src/adaptive_trader/__init__.py
COPY src/adaptive_trader/collection ./src/adaptive_trader/collection
COPY src/adaptive_trader/platform/__init__.py \
     src/adaptive_trader/platform/canonical.py \
     src/adaptive_trader/platform/config.py \
     src/adaptive_trader/platform/constants.py \
     src/adaptive_trader/platform/domain.py \
     src/adaptive_trader/platform/errors.py \
     src/adaptive_trader/platform/hashing.py \
     src/adaptive_trader/platform/package_resources.py \
     src/adaptive_trader/platform/security.py \
     src/adaptive_trader/platform/universe.py \
     ./src/adaptive_trader/platform/
COPY src/adaptive_trader/platform/_resources/__init__.py ./src/adaptive_trader/platform/_resources/__init__.py
COPY src/adaptive_trader/platform/data ./src/adaptive_trader/platform/data
COPY src/adaptive_trader/platform/observability/__init__.py \
     src/adaptive_trader/platform/observability/logging.py \
     ./src/adaptive_trader/platform/observability/
COPY src/adaptive_trader/platform/storage/__init__.py \
     src/adaptive_trader/platform/storage/datasets.py \
     src/adaptive_trader/platform/storage/engine.py \
     src/adaptive_trader/platform/storage/experiments.py \
     src/adaptive_trader/platform/storage/market_data.py \
     src/adaptive_trader/platform/storage/migration_roles.py \
     src/adaptive_trader/platform/storage/migration_runner.py \
     src/adaptive_trader/platform/storage/repositories.py \
     src/adaptive_trader/platform/storage/role_bootstrap.py \
     src/adaptive_trader/platform/storage/tables.py \
     src/adaptive_trader/platform/storage/transactions.py \
     ./src/adaptive_trader/platform/storage/
COPY configs ./configs
COPY alembic.ini ./alembic.ini
COPY pyproject.toml ./pyproject.toml
COPY migrations ./migrations
COPY docker/entrypoint.py ./docker/entrypoint.py

USER 10001:10001

RUN test -r /etc/ssl/certs/ca-certificates.crt

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["python", "-m", "adaptive_trader.collection.cli", "run"]

FROM python-base AS execution-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --only-group execution-runtime --only-group market-data-runtime --no-install-project

FROM runtime-base AS execution

ARG AQA_SOURCE_URL=https://github.com/yudhiishvs/autonomous-quant-agent
ARG AQA_VCS_REF=unknown
ARG AQA_VERSION=0.1.0

LABEL org.opencontainers.image.source="${AQA_SOURCE_URL}" \
      org.opencontainers.image.revision="${AQA_VCS_REF}" \
      org.opencontainers.image.version="${AQA_VERSION}" \
      org.opencontainers.image.title="Autonomous Quant Agent paper execution gate" \
      org.opencontainers.image.description="Default-deny paper boundary without strategy code"

COPY --from=execution-builder /app/.venv /app/.venv
COPY src/adaptive_trader/__init__.py ./src/adaptive_trader/__init__.py
COPY src/adaptive_trader/platform/__init__.py \
     src/adaptive_trader/platform/canonical.py \
     src/adaptive_trader/platform/config.py \
     src/adaptive_trader/platform/constants.py \
     src/adaptive_trader/platform/domain.py \
     src/adaptive_trader/platform/errors.py \
     src/adaptive_trader/platform/hashing.py \
     src/adaptive_trader/platform/paper_gate_runtime.py \
     src/adaptive_trader/platform/paper_cycle.py \
     src/adaptive_trader/platform/security.py \
     src/adaptive_trader/platform/universe.py \
     src/adaptive_trader/platform/worker_cli.py \
     ./src/adaptive_trader/platform/
# Namespace packages deliberately omit broad __init__ exports and provider discovery.
COPY src/adaptive_trader/platform/signals/models.py ./src/adaptive_trader/platform/signals/models.py
COPY src/adaptive_trader/platform/scheduling/models.py ./src/adaptive_trader/platform/scheduling/models.py
COPY src/adaptive_trader/platform/data/calendar.py ./src/adaptive_trader/platform/data/calendar.py
COPY src/adaptive_trader/platform/risk/policy.py \
     src/adaptive_trader/platform/risk/statistics.py \
     ./src/adaptive_trader/platform/risk/
COPY src/adaptive_trader/platform/storage/engine.py \
     src/adaptive_trader/platform/storage/repositories.py \
     src/adaptive_trader/platform/storage/tables.py \
     src/adaptive_trader/platform/storage/transactions.py \
     ./src/adaptive_trader/platform/storage/
COPY configs ./configs
COPY docker ./docker

USER 10001:10001

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["python", "-m", "adaptive_trader.platform.worker_cli", "run", "paper-execution-worker"]
