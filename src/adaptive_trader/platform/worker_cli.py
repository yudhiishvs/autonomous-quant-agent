"""Minimal worker entry point suitable for capability-restricted container images."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from adaptive_trader.platform.config import (
    RuntimeService,
    load_runtime_settings,
)

_USAGE = "usage: python -m adaptive_trader.platform.worker_cli (run|health) SERVICE [--once]"


def main() -> None:
    """Dispatch only exact worker identities; never accept arbitrary import targets."""

    arguments = tuple(sys.argv[1:])
    if len(arguments) not in {2, 3} or arguments[0] not in {"run", "health"}:
        raise SystemExit(_USAGE)
    if len(arguments) == 3 and (arguments[2] != "--once" or arguments[0] != "run"):
        raise SystemExit(_USAGE)
    try:
        service = RuntimeService(arguments[1])
    except ValueError:
        raise SystemExit("worker: service is not implemented") from None

    if service is RuntimeService.MARKET_DATA_LIVE:
        from adaptive_trader.collection.cli import ready, run

        if len(arguments) == 3:
            raise SystemExit("collector: use the explicit backfill command for a finite run")
        if arguments[0] == "health":
            ready()
        else:
            run(start_if_empty=None, verbose=False)
        return

    if service is RuntimeService.PAPER_EXECUTION_WORKER:
        from adaptive_trader.platform.paper_gate_runtime import (
            PaperGateRuntimeError,
            paper_gate_is_healthy,
            run_paper_gate,
        )

        if arguments[0] == "health":
            if not paper_gate_is_healthy():
                raise SystemExit("worker health: not ready")
            print("worker health: ready")
            return
        settings = load_runtime_settings(
            dict(os.environ),
            service=service,
            application_root=Path.cwd().resolve(strict=True),
        )
        try:
            run_paper_gate(settings, once=len(arguments) == 3)
        except PaperGateRuntimeError as error:
            raise SystemExit(str(error)) from None
        return

    from adaptive_trader.platform.worker_runtime import (
        DOMAIN_WORKER_SERVICES,
        run_service_worker,
        service_is_healthy,
    )

    if service not in DOMAIN_WORKER_SERVICES:
        raise SystemExit("worker: service is not implemented")
    if arguments[0] == "health":
        if not service_is_healthy(service=service):
            raise SystemExit("worker health: not ready")
        print("worker health: ready")
        return
    run_service_worker(
        service=service,
        application_root=Path.cwd(),
        once=len(arguments) == 3,
    )


if __name__ == "__main__":
    main()
