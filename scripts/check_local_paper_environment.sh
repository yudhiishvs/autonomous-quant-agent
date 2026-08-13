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
REQUIRED_TOKEN=I_ACKNOWLEDGE_PAPER_ONLY
UNSAFE=0

cd "${PROJECT_DIR}"
echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
echo "Local paper-environment safety check (credential values are never displayed)"

if [ -n "${APA_ALPACA_PAPER_API_KEY:-}" ]; then
    echo "APA_ALPACA_PAPER_API_KEY: PRESENT"
else
    echo "APA_ALPACA_PAPER_API_KEY: MISSING"
fi

if [ -n "${APA_ALPACA_PAPER_SECRET_KEY:-}" ]; then
    echo "APA_ALPACA_PAPER_SECRET_KEY: PRESENT"
else
    echo "APA_ALPACA_PAPER_SECRET_KEY: MISSING"
fi

if git check-ignore --quiet -- .env 2>/dev/null; then
    echo ".env ignore rule: PASS"
else
    echo ".env ignore rule: UNSAFE — .env is not ignored" >&2
    UNSAFE=1
fi

if ! CONFIG_STATE=$("${PYTHON_BIN}" -c '
import sys
from adaptive_trader.config import load_config

config = load_config(sys.argv[1])
print("ENABLED" if config.execution.paper_order_submission_enabled else "DISABLED")
' "${CONFIG_PATH}"); then
    echo "Paper-order configuration: ERROR — configuration could not be validated" >&2
    exit 2
fi

if [ "${CONFIG_STATE}" = "DISABLED" ]; then
    echo "Paper-order submission: DISABLED"
else
    echo "Paper-order submission: UNSAFE — enabled in ${CONFIG_PATH}" >&2
    UNSAFE=1
fi

if [ "${APA_ENABLE_PAPER_ORDERS:-}" = "${REQUIRED_TOKEN}" ]; then
    echo "Paper acknowledgement token: UNSAFE — active" >&2
    UNSAFE=1
else
    echo "Paper acknowledgement token: INACTIVE"
fi

if [ "${UNSAFE}" -ne 0 ]; then
    echo "Environment verdict: FAIL"
    exit 2
fi

echo "Environment verdict: SAFE FOR READ-ONLY PHASE 2 WORK"
