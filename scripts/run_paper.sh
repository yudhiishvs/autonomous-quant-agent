#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
CONFIG_PATH=${1:-configs/paper.yaml}
APA_PYTHON_BIN=${APA_PYTHON_BIN:-python3}
REQUIRED_TOKEN=I_ACKNOWLEDGE_PAPER_ONLY

cd "${PROJECT_DIR}"
echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"

if [ "${APA_ENABLE_PAPER_ORDERS:-}" != "${REQUIRED_TOKEN}" ]; then
    echo "Refusing paper-run: APA_ENABLE_PAPER_ORDERS must exactly equal ${REQUIRED_TOKEN}." >&2
    echo "Observer mode remains the safe default." >&2
    exit 2
fi

echo "The CLI will independently verify configuration, paper credentials, market status,"
echo "data freshness, risk checks, reconciliation state, and halt latches before any order."
exec "${APA_PYTHON_BIN}" -m adaptive_trader.cli paper-run --config "${CONFIG_PATH}"
