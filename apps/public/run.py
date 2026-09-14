"""Run one local application command using explicit disposable secret-file paths."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    repo = root.parent.parent
    local = root / ".local"
    commands = {
        "migrate": [
            str(root / "api/.venv/bin/alembic"),
            "-c",
            str(root / "api/alembic.ini"),
            "upgrade",
            "head",
        ],
        "api": [
            str(root / "api/.venv/bin/uvicorn"),
            "aqa_public.app:configured_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8018",
            "--no-access-log",
            "--no-proxy-headers",
        ],
    }
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit("Usage: python apps/public/run.py migrate|api")
    mode = sys.argv[1]
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(repo / "src"),
            "AQA_PUBLIC_DEVELOPMENT": "YES",
            "AQA_PUBLIC_ORIGIN": "http://127.0.0.1:5178",
            "AQA_PUBLIC_OIDC_ISSUER": "http://127.0.0.1:8188/realms/paper",
            "AQA_PUBLIC_OIDC_CLIENT_ID": "paper-web",
            "AQA_DATABASE_URL_FILE": str(
                local / ("migration_url" if mode == "migrate" else "database_url")
            ),
            "AQA_PUBLIC_OIDC_CLIENT_SECRET_FILE": str(local / "oidc_secret"),
            "AQA_PUBLIC_ENCRYPTION_KEY_FILE": str(local / "encryption_key"),
            "AQA_PUBLIC_APPROVAL_SIGNING_KEY_FILE": str(local / "approval_signing_key"),
        }
    )
    if mode == "api":
        os.chdir(repo)
        os.execve(commands[mode][0], commands[mode], environment)
    return subprocess.call(commands[mode], env=environment, cwd=repo)


if __name__ == "__main__":
    raise SystemExit(main())
