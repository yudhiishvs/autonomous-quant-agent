#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
APA_DASHBOARD_PORT=${APA_DASHBOARD_PORT:-8501}
APA_DASHBOARD_CONFIG=${APA_DASHBOARD_CONFIG:-configs/observer.yaml}
if [ -n "${APA_PYTHON_BIN:-}" ]; then
    PYTHON_BIN=${APA_PYTHON_BIN}
elif [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN=${PROJECT_DIR}/.venv/bin/python
else
    PYTHON_BIN=python3
fi

cd "${PROJECT_DIR}"

# The dashboard is a read-side process and never needs broker credentials.
unset APA_ALPACA_PAPER_API_KEY APA_ALPACA_PAPER_SECRET_KEY APA_ENABLE_PAPER_ORDERS
export APA_DASHBOARD_CONFIG

echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
echo "Opening the read-only dashboard at http://127.0.0.1:${APA_DASHBOARD_PORT}"
exec "${PYTHON_BIN}" -m streamlit run app.py \
    --server.address=127.0.0.1 \
    --server.port="${APA_DASHBOARD_PORT}" \
    --server.headless=true
