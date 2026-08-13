#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
SOURCE_PATH=${1:-runtime/primary_real_market_observer.db}
if [ -n "${APA_PYTHON_BIN:-}" ]; then
    PYTHON_BIN=${APA_PYTHON_BIN}
elif [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN=${PROJECT_DIR}/.venv/bin/python
else
    PYTHON_BIN=python3
fi

cd "${PROJECT_DIR}"
if [ "$#" -ge 2 ]; then
    exec "${PYTHON_BIN}" scripts/backup_database.py "${SOURCE_PATH}" "$2"
fi
exec "${PYTHON_BIN}" scripts/backup_database.py "${SOURCE_PATH}"
