"""Exercise projection SQL on SQLite; these are not PostgreSQL integration claims."""

from dataclasses import fields

import pytest
from sqlalchemy import create_engine, event

from adaptive_trader.platform.observability import operational as metrics
from adaptive_trader.platform.storage.tables import PLATFORM_SCHEMA


@pytest.fixture
def projected_metrics():
    engine = create_engine("sqlite://").execution_options(
        schema_translate_map={PLATFORM_SCHEMA: None}
    )
    sources = metrics._POSTGRES_SOURCES
    relations = {
        getattr(sources, field.name) for field in fields(sources) if not field.name.endswith("view")
    }
    with engine.begin() as connection:
        for relation in relations:
            names = [column.name for column in relation.columns] or ["fixture_id"]
            columns = ", ".join(f'"{name}"' for name in names)
            connection.exec_driver_sql(f'CREATE TABLE "fixture_{relation.name}" ({columns})')
            connection.exec_driver_sql(
                f'CREATE VIEW "{relation.name}" AS SELECT * FROM "fixture_{relation.name}"'
            )
    reader = metrics.SQLAlchemyOperationalMetricsReader(engine)
    # Use exactly the production safe-view projection while retaining SQLite execution.
    reader._sources = sources
    try:
        yield engine, reader
    finally:
        engine.dispose()


def _rows(engine, relation, names, values):
    columns = ",".join(f'"{name}"' for name in names)
    placeholders = ",".join("?" for _ in names)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f'INSERT INTO "fixture_{relation}" ({columns}) VALUES ({placeholders})', values
        )


def test_latest_latch_event_per_experiment_wins_and_restart_reads_fresh_state(projected_metrics):
    engine, reader = projected_metrics
    _rows(
        engine,
        "aqa_risk_latches_v",
        ["experiment_hash", "latch_type", "sequence", "action"],
        [
            ("a", "operator_halt", 1, "ENGAGED"),
            ("a", "operator_halt", 2, "CLEARED"),
            ("b", "operator_halt", 1, "ENGAGED"),
            ("a", "session_loss", 1, "ENGAGED"),
        ],
    )
    first = dict(reader.read().active_latches)
    assert first["operator_halt"] == 1 and first["session_loss"] == 1
    _rows(
        engine,
        "aqa_risk_latches_v",
        ["experiment_hash", "latch_type", "sequence", "action"],
        [("b", "operator_halt", 2, "CLEARED")],
    )
    assert dict(reader.read().active_latches)["operator_halt"] == 0


def test_joined_order_projection_preserves_intent_only_and_ambiguity(projected_metrics):
    engine, reader = projected_metrics
    _rows(
        engine,
        "aqa_orders_v",
        ["order_intent_id", "state"],
        [("i1", None), ("i2", "FILLED"), ("i3", "SUBMISSION_UNKNOWN"), ("i4", None)],
    )
    _rows(engine, "aqa_effective_bars_v", ["revision"], [(1,), (3,), (2,)])
    _rows(
        engine,
        "aqa_incidents_v",
        ["status", "severity"],
        [("open", "critical"), ("open", "critical"), ("resolved", "warning")],
    )
    result = reader.read()
    assert (result.bar_events, result.bar_corrections) == (6, 3)
    assert result.order_intents == 4
    assert dict(result.orders)["intent_only"] == 2
    assert dict(result.orders)["filled"] == 1
    assert result.ambiguous_orders == 1
    counts = {(status, severity): count for status, severity, count in result.incidents}
    assert counts["open", "critical"] == 2 and counts["resolved", "warning"] == 1
    assert sum(counts.values()) == 3


@pytest.mark.parametrize(
    "relation,names,values",
    [
        (
            "aqa_risk_latches_v",
            ["experiment_hash", "latch_type", "sequence", "action"],
            [("a", "unknown-latch", 1, "ENGAGED")],
        ),
        (
            "aqa_risk_latches_v",
            ["experiment_hash", "latch_type", "sequence", "action"],
            [("a", "operator_halt", 1, "unknown-action")],
        ),
        ("aqa_orders_v", ["order_intent_id", "state"], [("i", "unknown-order")]),
        ("aqa_effective_bars_v", ["revision"], [(0,)]),
        ("aqa_effective_bars_v", ["revision"], [(-1,)]),
        ("aqa_effective_bars_v", ["revision"], [(1.5,)]),
        ("aqa_incidents_v", ["status", "severity"], [("unknown", "critical")]),
        ("aqa_incidents_v", ["status", "severity"], [("open", "unknown")]),
        ("aqa_incidents_v", ["status", "severity"], [(None, "critical")]),
        ("aqa_data_gaps_v", ["status"], [("unknown",)]),
        ("aqa_jobs_v", ["state"], [("unknown",)]),
    ],
)
def test_malformed_projection_fails_instead_of_exporting_partial_metrics(
    projected_metrics, relation, names, values
):
    engine, reader = projected_metrics
    _rows(engine, relation, names, values)
    checked_in = []

    @event.listens_for(engine, "checkin")
    def checkin(*args):
        checked_in.append(True)

    with pytest.raises(metrics.OperationalMetricsReadError) as failure:
        reader.read()
    assert "unknown" not in str(failure.value)
    assert checked_in == [True]


def test_missing_projection_is_unavailable_and_does_not_leak_sql(projected_metrics):
    engine, reader = projected_metrics
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP VIEW aqa_orders_v")
    with pytest.raises(metrics.OperationalMetricsReadError) as failure:
        reader.read()
    assert str(failure.value) == "authoritative operational metrics are unavailable"


@pytest.mark.parametrize(
    "fault",
    [
        "status",
        "content-type",
        "oversize",
        "duplicate-key",
        "non-object",
        "invalid-json",
        "read-timeout",
    ],
)
def test_dashboard_response_failures_close_transport_before_rejection(tmp_path, fault):
    from types import SimpleNamespace

    from adaptive_trader.platform.dashboard.client import (
        DashboardApiClient,
        DashboardApiError,
        DashboardRoute,
    )
    from adaptive_trader.platform.security import SecretFileReference, SecretFileVariable

    token_path = tmp_path / "operator-token"
    token_path.write_text("T" * 32)
    token_path.chmod(0o600)
    token = SecretFileReference.from_path(
        token_path, source=SecretFileVariable.OPERATOR_TOKEN, application_root=tmp_path
    ).load()
    events = []

    class Response:
        status = 500 if fault == "status" else 200
        headers = SimpleNamespace(
            get_content_type=lambda: "text/html" if fault == "content-type" else "application/json"
        )

        def __enter__(self):
            events.append("opened")
            return self

        def __exit__(self, *args):
            events.append("closed")

        def read(self, amount):
            assert amount == 1_048_577
            if fault == "read-timeout":
                raise TimeoutError("private-response-detail")
            return {
                "oversize": b"x" * 1_048_577,
                "duplicate-key": b'{"x":1,"x":2}',
                "non-object": b"[]",
                "invalid-json": b"private-response-detail",
            }.get(fault, b"{}")

    def opener(request, *, timeout):
        assert request.method == "GET" and timeout == 5.0
        return Response()

    client = DashboardApiClient(base_url="http://control-api:8000", token=token, opener=opener)
    with pytest.raises(DashboardApiError) as error:
        client.get(DashboardRoute.HEALTH_READY)
    assert events == ["opened", "closed"]
    assert "private-response-detail" not in str(error.value)
