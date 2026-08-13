#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
CONFIG_PATH=${1:-configs/observer.yaml}
if [ -n "${APA_PYTHON_BIN:-}" ]; then
    :
elif [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
    APA_PYTHON_BIN=${PROJECT_DIR}/.venv/bin/python
else
    APA_PYTHON_BIN=python3
fi
OBSERVER_PID=""
STOP_REQUESTED=0
SHUTDOWN_DEADLINE=0

cd "${PROJECT_DIR}"
echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
echo "Full real-market observer session; broker mutation remains prohibited."

if [ -f .env ] && [ "${APA_OBSERVER_DOTENV_LOADED:-}" != "1" ]; then
    # Parse dotenv data as data, not shell code. Only the three approved names
    # are injected, and no value is printed.
    exec "${APA_PYTHON_BIN}" -c '
import os
import sys
from dotenv import dotenv_values

allowed = {
    "APA_ALPACA_PAPER_API_KEY",
    "APA_ALPACA_PAPER_SECRET_KEY",
    "APA_ENABLE_PAPER_ORDERS",
}
environment = dict(os.environ)
for name, value in dotenv_values(".env").items():
    if name in allowed and value is not None:
        environment[name] = value
environment["APA_OBSERVER_DOTENV_LOADED"] = "1"
os.execvpe("bash", ["bash", sys.argv[1], *sys.argv[2:]], environment)
' "$0" "$@"
fi

"${SCRIPT_DIR}/check_local_paper_environment.sh" "${CONFIG_PATH}"
export APA_ENABLE_PAPER_ORDERS=NO

if [ -z "${APA_ALPACA_PAPER_API_KEY:-}" ] || [ -z "${APA_ALPACA_PAPER_SECRET_KEY:-}" ]; then
    echo "Observer session blocked: dedicated paper credentials are MISSING." >&2
    exit 2
fi

"${APA_PYTHON_BIN}" -m adaptive_trader.cli doctor --config "${CONFIG_PATH}"

stop_observer() {
    STOP_REQUESTED=1
    if [ "${SHUTDOWN_DEADLINE}" -eq 0 ]; then
        SHUTDOWN_DEADLINE=$((SECONDS + 60))
    fi
    if [ -n "${OBSERVER_PID}" ] && kill -0 "${OBSERVER_PID}" 2>/dev/null; then
        kill -TERM "${OBSERVER_PID}"
    fi
}
trap stop_observer INT TERM

"${APA_PYTHON_BIN}" -m adaptive_trader.cli observe --config "${CONFIG_PATH}" &
OBSERVER_PID=$!
set +e
OBSERVER_RESULT=0
while :; do
    if [ "${STOP_REQUESTED}" -eq 0 ]; then
        wait "${OBSERVER_PID}"
        WAIT_RESULT=$?
        if [ "${STOP_REQUESTED}" -eq 0 ]; then
            OBSERVER_RESULT=${WAIT_RESULT}
            break
        fi
    fi

    # A signal can interrupt `wait`. Once shutdown is requested, poll instead
    # so the bounded grace period is still enforced if the child ignores TERM.
    if ! kill -0 "${OBSERVER_PID}" 2>/dev/null; then
        wait "${OBSERVER_PID}"
        OBSERVER_RESULT=$?
        break
    fi
    if [ "${SECONDS}" -ge "${SHUTDOWN_DEADLINE}" ]; then
        echo "Observer did not stop within 60 seconds; terminating child to avoid an orphan." >&2
        kill -KILL "${OBSERVER_PID}" 2>/dev/null || true
        wait "${OBSERVER_PID}"
        OBSERVER_RESULT=124
        break
    fi
    sleep 1
done
set -e
OBSERVER_PID=""
trap - INT TERM

if [ "${OBSERVER_RESULT}" -ne 0 ] && [ "${OBSERVER_RESULT}" -ne 130 ] && [ "${OBSERVER_RESULT}" -ne 143 ]; then
    echo "Observer exited with status ${OBSERVER_RESULT}; continuing evidence preservation." >&2
fi

set +e
"${APA_PYTHON_BIN}" -m adaptive_trader.cli status --config "${CONFIG_PATH}"
STATUS_RESULT=$?
"${APA_PYTHON_BIN}" -m adaptive_trader.cli report --config "${CONFIG_PATH}"
REPORT_RESULT=$?
set -e

DATABASE_PATH=$("${APA_PYTHON_BIN}" -c '
import sys
from pathlib import Path
from adaptive_trader.config import load_config

config = load_config(sys.argv[1])
print((Path.cwd() / config.project.database_path).resolve())
' "${CONFIG_PATH}")

SESSION_DATE=$("${APA_PYTHON_BIN}" -c '
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from adaptive_trader.config import load_config

config = load_config(sys.argv[1])
print(datetime.now(ZoneInfo(config.project.timezone)).date().isoformat())
' "${CONFIG_PATH}")

set +e
"${APA_PYTHON_BIN}" scripts/audit_observer_session.py \
    --config "${CONFIG_PATH}" \
    --database "${DATABASE_PATH}" \
    --session-date "${SESSION_DATE}"
AUDIT_RESULT=$?
"${APA_PYTHON_BIN}" scripts/backup_database.py \
    "${DATABASE_PATH}" \
    --config "${CONFIG_PATH}" \
    --evidence-directory outputs/observer_evidence \
    --manifest outputs/observer_evidence/observer_database_backup.json
BACKUP_RESULT=$?
"${APA_PYTHON_BIN}" scripts/summarize_observer_evidence.py \
    --config "${CONFIG_PATH}" \
    --output outputs/observer_evidence/summary.json
SUMMARY_RESULT=$?
set -e

echo "Observer evidence preservation complete."
echo "Observer=${OBSERVER_RESULT} status=${STATUS_RESULT} report=${REPORT_RESULT} audit=${AUDIT_RESULT} backup=${BACKUP_RESULT} summary=${SUMMARY_RESULT}"
if [ "${SUMMARY_RESULT}" -ne 0 ]; then
    echo "Evidence summary is expected to remain INCOMPLETE until 5 sessions, 3 dry runs, and a durable restart drill pass."
fi
echo "The official observer database was preserved and was never reset."

# `status` is intentionally preserved above, but a cleanly stopped observer has
# a stale/non-running heartbeat by definition. The session audit and report are
# the authoritative clean-shutdown gates, so that expected status is nonfatal.
if { [ "${OBSERVER_RESULT}" -ne 0 ] && [ "${OBSERVER_RESULT}" -ne 130 ] && [ "${OBSERVER_RESULT}" -ne 143 ]; } || \
    [ "${REPORT_RESULT}" -ne 0 ] || [ "${AUDIT_RESULT}" -ne 0 ] || \
    [ "${BACKUP_RESULT}" -ne 0 ]; then
    exit 1
fi
exit 0
