"""Bounded localhost-published TCP relay to the private Compose PostgreSQL service."""

from __future__ import annotations

import asyncio
from contextlib import suppress

_BUFFER_BYTES = 64 * 1024
_CONNECTION_LIMIT = 16
_CONNECT_TIMEOUT_SECONDS = 5
_IDLE_TIMEOUT_SECONDS = 300


async def _copy(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while data := await asyncio.wait_for(reader.read(_BUFFER_BYTES), _IDLE_TIMEOUT_SECONDS):
            writer.write(data)
            await writer.drain()
    except (TimeoutError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    semaphore: asyncio.Semaphore,
) -> None:
    async with semaphore:
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection("postgres", 5432),
                _CONNECT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, ConnectionError, OSError):
            client_writer.close()
            with suppress(ConnectionError):
                await client_writer.wait_closed()
            return
        await asyncio.gather(
            _copy(client_reader, upstream_writer),
            _copy(upstream_reader, client_writer),
        )


async def _serve() -> None:
    semaphore = asyncio.Semaphore(_CONNECTION_LIMIT)

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _relay(reader, writer, semaphore)

    # The container listener is reachable only on the internal database network; Compose publishes
    # this profile's host port exclusively on 127.0.0.1.
    server = await asyncio.start_server(  # nosec B104
        relay,
        "0.0.0.0",
        5432,
        backlog=_CONNECTION_LIMIT,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_serve())
