"""Repository-wide structural checks for the paper-only boundary."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from pathlib import Path

import pytest
import yaml

from adaptive_trader import cli
from adaptive_trader.logging_config import configure_logging


def _python_sources(project_root: Path) -> list[Path]:
    return sorted((project_root / "src").rglob("*.py"))


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_every_alpaca_trading_client_is_literal_paper_true(project_root: Path) -> None:
    constructors: list[tuple[Path, ast.Call]] = []
    for path in _python_sources(project_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in {
                "TradingClient",
                "TradingStream",
            }:
                constructors.append((path, node))
    assert constructors, "The Alpaca paper adapter must construct explicit trading clients"
    for path, call in constructors:
        paper_values = [item.value for item in call.keywords if item.arg == "paper"]
        assert len(paper_values) == 1, f"{path} must pass paper exactly once"
        value = paper_values[0]
        assert isinstance(value, ast.Constant) and value.value is True, (
            f"{path} must pass the literal paper=True"
        )


def test_no_live_endpoint_or_generic_alpaca_credentials(project_root: Path) -> None:
    forbidden = {
        "https://api.alpaca.markets",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
    }
    files = [*_python_sources(project_root), *sorted((project_root / "configs").glob("*.yaml"))]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for value in forbidden:
        assert value not in combined
    assert "live-trade" not in combined.lower()


def test_only_paper_broker_implementation_exists(project_root: Path) -> None:
    class_names: set[str] = set()
    for path in _python_sources(project_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        class_names.update(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    assert "AlpacaPaperBroker" in class_names
    assert not any(
        name in {"AlpacaLiveBroker", "LiveBroker", "RealMoneyBroker"} for name in class_names
    )


def test_structured_logs_redact_known_secrets(tmp_path: Path) -> None:
    import logging

    api_key = "FAKE-PAPER-KEY-123"
    secret = "FAKE-PAPER-SECRET-456"
    log_path = configure_logging(tmp_path, secrets=(api_key, secret))
    logging.getLogger("safety-test").error("api_key=%s authorization=Bearer %s", api_key, secret)
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert api_key not in contents
    assert secret not in contents
    assert "[REDACTED]" in contents


def test_redaction_removes_complete_header_and_json_style_values() -> None:
    from adaptive_trader.logging_config import redact

    sensitive = "VERYSECRET-PAPER-TOKEN"
    examples = (
        f"Authorization: Bearer {sensitive}",
        f"Authorization: Basic account:{sensitive}",
        f'{{"api_key": "{sensitive}"}}',
        f"secret={sensitive}",
        f"request failed with Bearer {sensitive}",
        f"https://example.invalid/path?api_key={sensitive}&symbol=SPY",
        f"APA_ALPACA_PAPER_API_KEY={sensitive}",
        f"APA_ALPACA_PAPER_SECRET_KEY={sensitive}",
        f"APCA_API_KEY_ID={sensitive}",
        f"APCA_API_SECRET_KEY={sensitive}",
        f"first line\nAuthorization: Bearer {sensitive}\nlast line",
    )
    for example in examples:
        redacted = redact(example)
        assert sensitive not in redacted
        assert "[REDACTED]" in redacted


def test_structured_logs_redact_nested_exception_secrets(
    tmp_path: Path,
    caplog,
) -> None:
    import logging

    sensitive = "FAKE-NESTED-EXCEPTION-SECRET-789"
    caplog.set_level(logging.ERROR)
    log_path = configure_logging(tmp_path)
    try:
        try:
            raise ValueError(f"APCA_API_SECRET_KEY={sensitive}")
        except ValueError as exc:
            raise RuntimeError(f"APA_ALPACA_PAPER_SECRET_KEY={sensitive}") from exc
    except RuntimeError:
        logging.getLogger("nested-redaction-test").exception("nested provider failure")

    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert sensitive not in contents
    assert sensitive not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_doctor_is_byte_for_byte_read_only_for_an_existing_database(
    project_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "existing-observer.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('unchanged')")

    config = yaml.safe_load((project_root / "configs" / "paper.yaml").read_text(encoding="utf-8"))
    config["project"]["database_path"] = str(database_path)
    config["project"]["output_directory"] = str(tmp_path / "outputs")
    config_path = tmp_path / "doctor.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    monkeypatch.delenv("APA_ALPACA_PAPER_API_KEY", raising=False)
    monkeypatch.delenv("APA_ALPACA_PAPER_SECRET_KEY", raising=False)

    def forbid_broker(_credentials: object) -> None:
        raise AssertionError("doctor must not construct a broker without credentials")

    monkeypatch.setattr(cli, "_create_alpaca_broker", forbid_broker)
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()

    assert cli._run_doctor(config_path) == 0

    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == ("unchanged",)
