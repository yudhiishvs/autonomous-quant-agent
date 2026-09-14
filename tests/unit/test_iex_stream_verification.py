"""An authenticated silent or stale feed must not receive a readiness sign-off."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scripts.verify_iex_stream import verify


@pytest.mark.parametrize(
    "lags, subscribed, fails, expected",
    [
        ([60, 90], True, False, True),
        ([], True, False, False),
        ([181], True, False, False),
        ([-1], True, False, False),
        ([60], False, False, False),
        ([60], True, True, False),
    ],
)
def test_receipt_and_reconnect_evidence(lags, subscribed, fails, expected):
    closed = []
    constructed = []

    class Source:
        def __init__(self, *, state_handler):
            self.state_handler = state_handler
            constructed.append(self)

        def run(self, symbols, handler, *, stop_requested):
            assert symbols == ("AMD",)
            self.state_handler("authenticated")
            if subscribed:
                self.state_handler("subscribed")
            for lag in lags:
                start = datetime(2026, 9, 14, 14, tzinfo=UTC)
                handler(
                    SimpleNamespace(
                        bar=SimpleNamespace(
                            receipt_timestamp_utc=start + timedelta(seconds=lag),
                            bar_timestamp_utc=start,
                        )
                    )
                )
            if fails:
                raise RuntimeError("SECRET_SENTINEL")

        def stop(self):
            closed.append(self)

    report = verify(Source, seconds=1)
    assert report["passed"] is expected
    assert len(constructed) == 2
    assert closed == constructed
    assert "SECRET_SENTINEL" not in str(report)
    assert report["database_persistence_verified"] is False
    assert report["unattended_operation_verified"] is False


@pytest.mark.parametrize("seconds", [0, 301, True, 1.5])
def test_rejects_unbounded_duration(seconds):
    with pytest.raises(ValueError):
        verify(lambda **kwargs: pytest.fail("unexpected connection"), seconds=seconds)


def test_cli_requires_explicit_provider_acknowledgement(monkeypatch, capsys):
    from scripts import verify_iex_stream

    monkeypatch.setattr("sys.argv", ["verify_iex_stream.py"])
    monkeypatch.setattr(
        verify_iex_stream.AlpacaDataCredentials,
        "from_environment",
        lambda: pytest.fail("credentials loaded without acknowledgement"),
    )
    with pytest.raises(SystemExit) as result:
        verify_iex_stream.main()
    assert result.value.code == 2
    assert "--allow-provider" in capsys.readouterr().err
