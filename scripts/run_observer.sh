#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
CONFIG_PATH=${1:-configs/observer.yaml}
if [ -n "${APA_PYTHON_BIN:-}" ]; then
    PYTHON_BIN=${APA_PYTHON_BIN}
elif [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN=${PROJECT_DIR}/.venv/bin/python
else
    PYTHON_BIN=python3
fi

cd "${PROJECT_DIR}"
echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
echo "Starting observer mode. This command cannot submit orders."
exec "${PYTHON_BIN}" -m adaptive_trader.cli observe --config "${CONFIG_PATH}"
