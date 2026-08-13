FROM python:3.11-slim

ARG APA_UID=10001
ARG APA_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

RUN groupadd --gid "${APA_GID}" apa \
    && useradd --uid "${APA_UID}" --gid "${APA_GID}" --no-create-home --shell /usr/sbin/nologin apa

COPY --chown=apa:apa pyproject.toml README.md ./
COPY --chown=apa:apa src ./src
RUN python -m pip install ".[dashboard]"

COPY --chown=apa:apa app.py ./app.py
COPY --chown=apa:apa configs ./configs
COPY --chown=apa:apa docs ./docs
COPY --chown=apa:apa scripts ./scripts
RUN install -d -o apa -g apa /app/data/cache /app/runtime /app/outputs

USER apa

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python", "-m", "adaptive_trader.cli", "status", "--config", "configs/observer.yaml"]

CMD ["python", "-m", "adaptive_trader.cli", "observe", "--config", "configs/observer.yaml"]
