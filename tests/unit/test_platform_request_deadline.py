"""Request framing is bounded before authentication, including slow senders."""

import asyncio

import pytest

import adaptive_trader.platform.control.api as api


@pytest.mark.asyncio
async def test_incomplete_body_times_out_without_reaching_application(monkeypatch):
    monkeypatch.setattr(api, "REQUEST_BODY_TIMEOUT_SECONDS", 0.01)
    sent = []

    async def app(*args):
        pytest.fail("incomplete body reached application")

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    middleware = api.RequestSecurityMiddleware(
        app, correlation_id_factory=lambda: "00000000-0000-0000-0000-000000000000"
    )
    await asyncio.wait_for(middleware({"type": "http", "headers": []}, receive, send), 1)
    assert sent[0]["status"] == 408
    assert b"request_timeout" in sent[1]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size, expected", [(65_536, 200), (65_537, 413)])
async def test_chunked_body_preserves_bytes_and_enforces_total_limit(size, expected):
    sent = []
    chunks = iter([b"a" * 32_768, b"b" * (size - 32_768)])
    count = 0

    async def receive():
        nonlocal count
        count += 1
        return {"type": "http.request", "body": next(chunks), "more_body": count == 1}

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        message = await receive()
        assert message["body"] == b"a" * 32_768 + b"b" * (size - 32_768)
        assert message["more_body"] is False
        await send({"type": "http.response.start", "status": 200, "headers": []})

    middleware = api.RequestSecurityMiddleware(
        app, correlation_id_factory=lambda: "00000000-0000-0000-0000-000000000000"
    )
    await middleware({"type": "http", "headers": []}, receive, send)
    assert sent[0]["status"] == expected


@pytest.mark.asyncio
async def test_disconnect_does_not_dispatch_partial_body():
    async def app(*args):
        pytest.fail("disconnected request reached application")

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pytest.fail("responded to disconnected client")

    middleware = api.RequestSecurityMiddleware(
        app, correlation_id_factory=lambda: "00000000-0000-0000-0000-000000000000"
    )
    await middleware({"type": "http", "headers": []}, receive, send)
