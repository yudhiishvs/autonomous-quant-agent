#!/usr/bin/env python3
"""Run a Python module while denying outbound network operations."""

from __future__ import annotations

import argparse
import runpy
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class NetworkAccessDenied(RuntimeError):
    """Raised when a guarded process attempts network access."""


def _deny(*_args: object, **_kwargs: object) -> None:
    raise NetworkAccessDenied("outbound network access is disabled")


@contextmanager
def deny_outbound_network() -> Iterator[None]:
    """Temporarily block Python DNS resolution and outbound socket operations."""

    replacements: tuple[tuple[object, str, Callable[..., Any]], ...] = (
        (socket, "create_connection", _deny),
        (socket, "getaddrinfo", _deny),
        (socket, "gethostbyaddr", _deny),
        (socket, "gethostbyname", _deny),
        (socket, "gethostbyname_ex", _deny),
        (socket.socket, "connect", _deny),
        (socket.socket, "connect_ex", _deny),
        (socket.socket, "sendto", _deny),
    )
    originals = tuple((owner, name, getattr(owner, name)) for owner, name, _ in replacements)
    try:
        for owner, name, replacement in replacements:
            setattr(owner, name, replacement)
        yield
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a Python module with DNS and outbound sockets disabled.",
    )
    parser.add_argument("module", help="Importable Python module to execute as __main__.")
    parser.add_argument(
        "arguments", nargs=argparse.REMAINDER, help="Arguments passed to the module."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    module_arguments = list(arguments.arguments)
    if module_arguments[:1] == ["--"]:
        module_arguments.pop(0)
    sys.argv = [arguments.module, *module_arguments]
    with deny_outbound_network():
        runpy.run_module(arguments.module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
