"""Deterministic and adversarial tests for append-only audit evidence."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, delete, select, update
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import OperationalError

from adaptive_trader.platform.constants import (
    AUDIT_GENESIS_HASH,
    MAX_AUDIT_CONTAINER_ITEMS,
    MAX_AUDIT_PAYLOAD_BYTES,
    MAX_SIGNED_64_BIT_INTEGER,
)
from adaptive_trader.platform.domain import (
    AuditEvent,
    AuditPayload,
    AuditStreamHead,
    AuditVerificationReport,
    AuditWriter,
    audit_event_hash,
)
from adaptive_trader.platform.errors import (
    AuditIntegrityError,
    AuditPersistenceError,
    AuditValidationError,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.repositories import AuditRepository, verify_audit_chain
from adaptive_trader.platform.storage.tables import aqa_audit_events, metadata
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
    TransactionBoundaryError,
    TransactionViolation,
)

_OCCURRED_AT = datetime(2026, 9, 5, 12, 34, 56, 123456, tzinfo=UTC)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'audit.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    ).execution_options(schema_translate_map={"aqa": None})
    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def repository(sqlite_engine: Engine) -> AuditRepository:
    return AuditRepository(sqlite_engine, writer=AuditWriter.CONTROL)


def _audit_id(label: str) -> str:
    return f"test_{sha256_hex(('audit-test-id', label))}"


def _payload(value: object, *, idempotency_key: str = "default") -> AuditPayload:
    if type(value) is not dict:
        return AuditPayload.from_mapping(value)
    prepared = dict(value)
    prepared.setdefault("idempotency_key", _audit_id(idempotency_key))
    return AuditPayload.from_mapping(prepared)


def _append(
    repository: AuditRepository,
    *,
    stream_id: str = "aqa_control:experiment:semiconductor-v1",
    offset: int = 0,
) -> AuditEvent:
    return repository.append(
        stream_id=stream_id,
        event_type="experiment.registered",
        occurred_at=_OCCURRED_AT + timedelta(microseconds=offset),
        payload=_payload(
            {"experiment_hash": "0" * 64, "version": offset + 1},
            idempotency_key=f"event-{offset}",
        ),
    )


def test_sqlite_repository_requires_the_explicit_platform_schema_map() -> None:
    bare_engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="platform schema map"):
            AuditRepository(bare_engine)
    finally:
        bare_engine.dispose()


def test_audit_event_has_known_canonical_hashes_and_id() -> None:
    event = AuditEvent.create(
        stream_id="aqa_control:experiment:semiconductor-v1",
        sequence=1,
        previous_hash=AUDIT_GENESIS_HASH,
        event_type="experiment.registered",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=_payload({"version": 1, "experiment_hash": "0" * 64}),
    )

    assert event.payload_json == (
        '{"experiment_hash":"'
        + ("0" * 64)
        + '","idempotency_key":"'
        + _audit_id("default")
        + '","version":1}'
    )
    assert event.payload_hash == "7248bab9fc3ec1db7c371b66ba563882c1a4a006f0ba5a9b96231cfca469c509"
    assert event.event_hash == "9c432e7eb8f72cff5755568f4d6a377146b5d5356e7dac7336e760fd8942ed21"
    assert event.audit_event_id == f"audit_{event.event_hash}"
    assert event.content_hash == event.event_hash


def test_audit_event_is_deeply_immutable_at_its_public_boundary() -> None:
    payload = {
        "state": "completed",
        "counts": {"accepted": 1, "rejected": 2},
        "correlation_id": "0198fa2d-7b8c-7123-8abc-0123456789ab",
    }
    event = AuditEvent.create(
        stream_id="aqa_control:job:one",
        sequence=1,
        previous_hash=AUDIT_GENESIS_HASH,
        event_type="job.completed",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=_payload(payload),
    )
    payload["state"] = "modified"
    decoded = event.payload
    decoded["state"] = "also-modified"

    assert event.payload == {
        "correlation_id": "0198fa2d-7b8c-7123-8abc-0123456789ab",
        "counts": {"accepted": 1, "rejected": 2},
        "idempotency_key": _audit_id("default"),
        "state": "completed",
    }
    with pytest.raises(FrozenInstanceError):
        event.sequence = 2
    with pytest.raises(AttributeError):
        _ = event.__dict__


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "not-even-a-real-key"},
        {"api-key": "not-even-a-real-key"},
        {"apiKey": "SHORT-UPPERCASE-TOKEN"},
        {"mysecret_id": "reference:one"},
        {"accessTokenHash": "0" * 64},
        {"context": {"databaseURL": "postgresql://localhost/example"}},
        {"reason_code": "Authorization: Bearer fake-value"},
        {"reason_code": "prefix POSTGRESQL://WORKER:fake-value@DATABASE/example suffix"},
        {"correlation_id": "HTTPS://example.invalid/resource"},
        {"reason_code": "PKAB12CD34"},
        {"reason_code": "a" * 64},
        {"reason_code": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl"},
        {"reason_code": "-----BEGIN PRIVATE KEY-----"},
    ],
)
def test_secret_like_payload_keys_and_values_are_rejected_without_echo(payload: object) -> None:
    with pytest.raises(AuditValidationError) as captured:
        AuditEvent.create(
            stream_id="aqa_execution:security:startup",
            sequence=1,
            previous_hash=AUDIT_GENESIS_HASH,
            event_type="security.startup_failed",
            actor=AuditWriter.EXECUTION,
            occurred_at=_OCCURRED_AT,
            payload=_payload(payload),
        )

    assert "fake-value" not in str(captured.value)
    assert "SHORT-UPPERCASE-TOKEN" not in str(captured.value)


def test_closed_payload_accepts_typed_metadata_and_prefixed_deterministic_ids() -> None:
    deterministic_id = f"job_{'a' * 64}"

    payload = AuditPayload.from_mapping(
        {
            "client_order_id": deterministic_id,
            "correlation_id": "0198fa2d-7b8c-7123-8abc-0123456789ab",
            "counts": {"accepted": 3, "rejected": 0},
            "event_ids": [f"event_{'a' * 64}", f"event_{'b' * 64}"],
            "experiment_hash": "c" * 64,
            "fencing_token": 2,
            "idempotency_key": f"request_{'d' * 64}",
            "occurred_at": _OCCURRED_AT,
            "reason_codes": ["accepted", "deduplicated"],
            "state": "completed",
            "submission_enabled": False,
            "symbol": "NVDA",
            "symbols": ["NVDA", "AMD"],
            "target_price": Decimal("123.4500"),
            "version": 1,
        }
    )

    assert payload.value["client_order_id"] == deterministic_id
    assert payload.value["occurred_at"] == "2026-09-05T12:34:56.123456Z"
    assert payload.value["target_price"] == "123.45"


@pytest.mark.parametrize(
    "opaque_id",
    [
        "PKAB12CD34",
        "AKIAIOSFODNN7EXAMPLE",
        "0198FA2D-7B8C-7123-8ABC-0123456789AB",
        "opaque-value-without-namespace",
    ],
)
def test_audit_payload_rejects_opaque_ids_without_entropy_guessing(opaque_id: str) -> None:
    with pytest.raises(AuditValidationError, match="ID is invalid"):
        _payload({"correlation_id": opaque_id})


@pytest.mark.parametrize(
    "payload",
    [
        {"context": {"state": "completed"}},
        {"session_id": "session:one"},
        {"credential_id": "credential:one"},
        {"arbitrary_hash": "0" * 64},
        {"unexpected_count": 1},
        {"is_unreviewed": True},
        {"closing_price": Decimal("1.0")},
    ],
)
def test_audit_payload_rejects_unknown_top_level_fields(payload: object) -> None:
    with pytest.raises(AuditValidationError):
        _payload(payload)


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "2026-02-30T12:00:00.000000Z",
        "2026-09-05T25:00:00.000000Z",
        "2026-09-05T12:00:60.000000Z",
    ],
)
def test_audit_payload_parses_timestamp_values_not_only_their_shape(
    invalid_timestamp: str,
) -> None:
    with pytest.raises(AuditValidationError, match="timestamp is invalid"):
        _payload({"occurred_at": invalid_timestamp})


def test_audit_event_requires_the_closed_typed_payload_contract() -> None:
    with pytest.raises(AuditValidationError, match="typed payload contract"):
        AuditEvent.create(
            stream_id="aqa_control:job:one",
            sequence=1,
            previous_hash=AUDIT_GENESIS_HASH,
            event_type="job.completed",
            actor=AuditWriter.CONTROL,
            occurred_at=_OCCURRED_AT,
            payload={},
        )


def test_repository_requires_a_bound_writer_for_append(sqlite_engine: Engine) -> None:
    verifier = AuditRepository(sqlite_engine)

    with pytest.raises(AuditPersistenceError, match="verify-only"):
        verifier.append(
            stream_id="aqa_control:job:one",
            event_type="job.completed",
            occurred_at=_OCCURRED_AT,
            payload=_payload({"state": "completed"}),
        )


@pytest.mark.parametrize(
    ("writer", "stream_id", "event_type"),
    [
        (AuditWriter.COLLECTOR, "aqa_collector:data:bars", "data.persisted"),
        (AuditWriter.SCHEDULER, "aqa_scheduler:slot:open", "slot.claimed"),
        (AuditWriter.STRATEGY, "aqa_strategy:signal:one", "signal.emitted"),
        (AuditWriter.EXECUTION, "aqa_execution:order:one", "order.submitted"),
        (AuditWriter.CONTROL, "aqa_control:job:one", "job.completed"),
    ],
)
def test_closed_writer_contract_accepts_only_owned_streams_and_event_families(
    sqlite_engine: Engine,
    writer: AuditWriter,
    stream_id: str,
    event_type: str,
) -> None:
    repository = AuditRepository(sqlite_engine, writer=writer)
    event = repository.append(
        stream_id=stream_id,
        event_type=event_type,
        occurred_at=_OCCURRED_AT,
        payload=_payload({"state": "completed"}, idempotency_key=stream_id),
    )

    assert event.actor is writer
    forged_stream = (
        "aqa_execution:order:forged"
        if writer is not AuditWriter.EXECUTION
        else "aqa_control:job:forged"
    )
    with pytest.raises(AuditValidationError, match="does not own"):
        repository.append(
            stream_id=forged_stream,
            event_type=event_type,
            occurred_at=_OCCURRED_AT,
            payload=_payload({}, idempotency_key=f"forged-{stream_id}"),
        )
    with pytest.raises(AuditValidationError, match="not permitted"):
        repository.append(
            stream_id=stream_id,
            event_type=(
                "order.submitted" if writer is AuditWriter.CONTROL else "experiment.registered"
            ),
            occurred_at=_OCCURRED_AT,
            payload=_payload({}, idempotency_key=f"family-{stream_id}"),
        )


def test_audit_event_rejects_free_form_actor_even_when_its_text_is_known() -> None:
    with pytest.raises(AuditValidationError, match="closed writer contract"):
        AuditEvent.create(
            stream_id="aqa_control:job:one",
            sequence=1,
            previous_hash=AUDIT_GENESIS_HASH,
            event_type="job.completed",
            actor="aqa_control",  # type: ignore[arg-type]
            occurred_at=_OCCURRED_AT,
            payload=_payload({"state": "completed"}),
        )


def test_hostile_payload_object_is_rejected_without_rendering() -> None:
    class Hostile:
        def __str__(self) -> str:
            raise AssertionError("secret rendering must not run")

        def __repr__(self) -> str:
            raise AssertionError("secret rendering must not run")

    with pytest.raises(AuditValidationError, match="payload is invalid"):
        AuditEvent.create(
            stream_id="aqa_execution:security:startup",
            sequence=1,
            previous_hash=AUDIT_GENESIS_HASH,
            event_type="security.startup_failed",
            actor=AuditWriter.EXECUTION,
            occurred_at=_OCCURRED_AT,
            payload=_payload({"reason_code": Hostile()}),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"stream_id": "invalid stream"}, "stream ID"),
        ({"stream_id": "aqa_control"}, "stream ID"),
        ({"stream_id": "aqa_unknown:job:one"}, "invalid writer"),
        ({"stream_id": "aqa_control:job:"}, "resource reference"),
        ({"event_type": "UPPERCASE"}, "event type"),
        ({"event_type": "job"}, "event type"),
        ({"actor": "actor\nforged"}, "actor"),
        ({"sequence": 0}, "sequence"),
        ({"previous_hash": "0" * 63}, "previous hash"),
        ({"occurred_at": datetime(2026, 9, 5)}, "timestamp"),
    ],
)
def test_audit_event_rejects_invalid_bounded_fields(
    changes: dict[str, object],
    message: str,
) -> None:
    arguments: dict[str, object] = {
        "stream_id": "aqa_control:job:one",
        "sequence": 1,
        "previous_hash": AUDIT_GENESIS_HASH,
        "event_type": "job.completed",
        "actor": AuditWriter.CONTROL,
        "occurred_at": _OCCURRED_AT,
        "payload": _payload({}),
    }
    arguments.update(changes)

    with pytest.raises(AuditValidationError, match=message):
        AuditEvent.create(**arguments)


def test_audit_payload_enforces_object_and_byte_limits() -> None:
    with pytest.raises(AuditValidationError, match="must be an object"):
        AuditPayload.from_mapping(["not", "an", "object"])
    with pytest.raises(AuditValidationError, match="size limit"):
        AuditPayload.from_mapping({"reason_code": "x" * MAX_AUDIT_PAYLOAD_BYTES})
    with pytest.raises(AuditValidationError, match="requires an idempotency key"):
        AuditPayload.from_mapping({"state": "completed"})
    with pytest.raises(AuditValidationError, match="container exceeds"):
        _payload(
            {f"bounded_field_{index}": "completed" for index in range(MAX_AUDIT_CONTAINER_ITEMS)}
        )


@pytest.mark.parametrize(
    ("payload_json", "message"),
    [
        (None, "encoding"),
        ("\ud800", "encoding"),
        ("x" * (MAX_AUDIT_PAYLOAD_BYTES + 1), "size limit"),
        (("[" * 1_100) + "0" + ("]" * 1_100), "encoding"),
        ("[]", "must be an object"),
        ("{}", "idempotency key"),
    ],
)
def test_stored_audit_payload_rejects_malformed_or_unbounded_json(
    payload_json: object,
    message: str,
) -> None:
    with pytest.raises(AuditValidationError, match=message):
        AuditPayload(
            payload_json=payload_json,  # type: ignore[arg-type]
            payload_hash="0" * 64,
        )


def test_stored_audit_payload_requires_canonical_encoding_and_matching_hash() -> None:
    valid = _payload({"state": "completed"})

    with pytest.raises(AuditValidationError, match="not canonical"):
        AuditPayload(
            payload_json=valid.payload_json + "\n",
            payload_hash=valid.payload_hash,
        )
    with pytest.raises(AuditValidationError, match="payload hash"):
        AuditPayload(
            payload_json=valid.payload_json,
            payload_hash="0" * 64,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"9invalid": "value"},
        {"reason-code": "invalid-key-spelling"},
        {"state": None},
        {"count": -1},
        {"count": MAX_SIGNED_64_BIT_INTEGER + 1},
        {"submission_enabled": 1},
        {"job_id": 7},
        {"symbols": "NVDA"},
        {"symbols": ["NVDA"] * (MAX_AUDIT_CONTAINER_ITEMS + 1)},
        {"symbols": [7]},
        {"event_ids": "event_" + ("a" * 64)},
        {"event_ids": ["event_" + ("a" * 64)] * (MAX_AUDIT_CONTAINER_ITEMS + 1)},
        {"event_ids": [7]},
        {"counts": []},
        {"counts": {f"state_{index}": 1 for index in range(MAX_AUDIT_CONTAINER_ITEMS + 1)}},
        {"counts": {"accepted": -1}},
    ],
)
def test_closed_payload_rejects_invalid_semantics_for_every_container_family(
    payload: object,
) -> None:
    with pytest.raises(AuditValidationError):
        _payload(payload)


def test_closed_payload_accepts_bounded_hash_lists() -> None:
    payload = _payload({"artifact_hashes": ["a" * 64, "b" * 64]})

    assert payload.value["artifact_hashes"] == ["a" * 64, "b" * 64]


def test_stored_audit_domain_objects_reject_malformed_representations() -> None:
    event = AuditEvent.create(
        stream_id="aqa_control:job:stored-contract",
        sequence=1,
        previous_hash=AUDIT_GENESIS_HASH,
        event_type="job.completed",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=_payload({"state": "completed"}),
    )

    with pytest.raises(AuditValidationError, match="event ID"):
        replace(event, audit_event_id="invalid")
    with pytest.raises(AuditValidationError, match="sequence"):
        replace(event, sequence=0)
    with pytest.raises(AuditValidationError, match="timestamp"):
        replace(event, occurred_at=_OCCURRED_AT.replace(tzinfo=None))
    with pytest.raises(AuditValidationError, match="payload hash"):
        _ = replace(event, payload_hash="f" * 64).audit_payload
    with pytest.raises(AuditValidationError, match="sequence"):
        AuditStreamHead(stream_id=event.stream_id, sequence=0, event_hash=event.event_hash)


def test_public_audit_hash_rejects_invalid_sequence_and_timestamp() -> None:
    with pytest.raises(AuditValidationError, match="sequence"):
        audit_event_hash(
            stream_id="aqa_control:job:hash-contract",
            sequence=0,
            previous_hash=AUDIT_GENESIS_HASH,
            event_type="job.completed",
            actor=AuditWriter.CONTROL,
            occurred_at=_OCCURRED_AT,
            payload_hash="a" * 64,
        )
    with pytest.raises(AuditValidationError, match="timestamp"):
        audit_event_hash(
            stream_id="aqa_control:job:hash-contract",
            sequence=1,
            previous_hash=AUDIT_GENESIS_HASH,
            event_type="job.completed",
            actor=AuditWriter.CONTROL,
            occurred_at=_OCCURRED_AT.replace(tzinfo=None),
            payload_hash="a" * 64,
        )


def test_repository_appends_independent_streams_and_verifies_all_hashes(
    repository: AuditRepository,
) -> None:
    first = _append(repository)
    second = _append(repository, offset=1)
    other = _append(repository, stream_id="aqa_control:experiment:other")

    assert first.sequence == 1
    assert first.previous_hash == AUDIT_GENESIS_HASH
    assert second.sequence == 2
    assert second.previous_hash == first.event_hash
    assert other.sequence == 1
    assert other.previous_hash == AUDIT_GENESIS_HASH
    assert repository.list_events() == (other, first, second)
    assert repository.list_events(stream_id=first.stream_id) == (first, second)

    report = repository.verify()
    assert report.event_count == 3
    assert [(head.stream_id, head.sequence) for head in report.stream_heads] == [
        ("aqa_control:experiment:other", 1),
        ("aqa_control:experiment:semiconductor-v1", 2),
    ]


def test_repository_normalizes_global_stream_order_to_python_code_points(
    repository: AuditRepository,
) -> None:
    later = _append(repository, stream_id="aqa_control:job:a_a", offset=1)
    earlier = _append(repository, stream_id="aqa_control:job:a-a")

    assert repository.list_events() == (earlier, later)
    assert tuple(head.stream_id for head in repository.verify().stream_heads) == (
        earlier.stream_id,
        later.stream_id,
    )


def test_repository_owned_reads_have_an_explicit_transaction_boundary(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    expected = _append(repository)
    observed: list[str] = []

    def record_begin(connection: object) -> None:
        del connection
        observed.append("begin")

    def record_commit(connection: object) -> None:
        del connection
        observed.append("commit")

    sqlalchemy_event.listen(sqlite_engine, "begin", record_begin)
    sqlalchemy_event.listen(sqlite_engine, "commit", record_commit)
    try:
        assert repository.list_events() == (expected,)
    finally:
        sqlalchemy_event.remove(sqlite_engine, "begin", record_begin)
        sqlalchemy_event.remove(sqlite_engine, "commit", record_commit)

    assert observed == ["begin", "commit"]


def test_same_transaction_supports_multiple_appends_and_rolls_back_together(
    repository: AuditRepository,
) -> None:
    with repository.transaction() as connection:
        first_committed = repository.append(
            stream_id="aqa_control:job:committed",
            event_type="job.started",
            occurred_at=_OCCURRED_AT,
            payload=_payload({"state": "running"}, idempotency_key="test:commit-start"),
            connection=connection,
        )
        second_committed = repository.append(
            stream_id="aqa_control:job:committed",
            event_type="job.completed",
            occurred_at=_OCCURRED_AT + timedelta(seconds=1),
            payload=_payload({"state": "completed"}, idempotency_key="test:commit-end"),
            connection=connection,
        )
        assert repository.list_events(
            stream_id="aqa_control:job:committed",
            connection=connection,
        ) == (first_committed, second_committed)

    assert repository.list_events(stream_id="aqa_control:job:committed") == (
        first_committed,
        second_committed,
    )

    with (
        pytest.raises(RuntimeError, match="abort transaction"),
        repository.transaction() as connection,
    ):
        first = repository.append(
            stream_id="aqa_control:job:rollback",
            event_type="job.started",
            occurred_at=_OCCURRED_AT,
            payload=_payload({"state": "running"}, idempotency_key="test:rollback-start"),
            connection=connection,
        )
        second = repository.append(
            stream_id="aqa_control:job:rollback",
            event_type="job.completed",
            occurred_at=_OCCURRED_AT + timedelta(seconds=1),
            payload=_payload({"state": "completed"}, idempotency_key="test:rollback-end"),
            connection=connection,
        )
        assert second.sequence == first.sequence + 1
        raise RuntimeError("abort transaction")

    assert repository.list_events(stream_id="aqa_control:job:rollback") == ()


def test_serialized_transaction_is_shared_across_repository_instances(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    participant = AuditRepository(sqlite_engine, writer=AuditWriter.CONTROL)
    with repository.transaction() as connection:
        committed = participant.append(
            stream_id="aqa_control:job:cross-repository-commit",
            event_type="job.completed",
            occurred_at=_OCCURRED_AT,
            payload=_payload({"state": "completed"}),
            connection=connection,
        )

    assert repository.list_events(stream_id=committed.stream_id) == (committed,)

    with (
        pytest.raises(RuntimeError, match="cross-repository rollback"),
        repository.transaction() as connection,
    ):
        participant.append(
            stream_id="aqa_control:job:cross-repository-rollback",
            event_type="job.failed",
            occurred_at=_OCCURRED_AT,
            payload=_payload({"state": "failed"}),
            connection=connection,
        )
        raise RuntimeError("cross-repository rollback")

    assert repository.list_events(stream_id="aqa_control:job:cross-repository-rollback") == ()


def test_append_retry_returns_the_exact_prior_event_without_creating_a_new_tail(
    repository: AuditRepository,
) -> None:
    arguments = {
        "stream_id": "aqa_control:job:idempotent-retry",
        "event_type": "job.completed",
        "occurred_at": _OCCURRED_AT,
        "payload": _payload(
            {"state": "completed"},
            idempotency_key="request:ambiguous-commit",
        ),
    }

    first = repository.append(**arguments)
    retry = repository.append(**arguments)

    assert retry is first or retry == first
    assert repository.list_events(stream_id=first.stream_id) == (first,)


def test_append_rejects_conflicting_idempotency_reuse_without_changing_the_stream(
    repository: AuditRepository,
) -> None:
    key = "request:conflicting-reuse"
    first = repository.append(
        stream_id="aqa_control:job:idempotency-conflict",
        event_type="job.completed",
        occurred_at=_OCCURRED_AT,
        payload=_payload({"state": "completed"}, idempotency_key=key),
    )

    with pytest.raises(AuditValidationError, match="reused with different content"):
        repository.append(
            stream_id=first.stream_id,
            event_type="job.failed",
            occurred_at=_OCCURRED_AT,
            payload=_payload({"state": "failed"}, idempotency_key=key),
        )

    assert repository.list_events(stream_id=first.stream_id) == (first,)


def test_sqlite_append_rejects_a_deferred_external_transaction(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    with (
        sqlite_engine.begin() as connection,
        pytest.raises(AuditPersistenceError, match="serialized transaction"),
    ):
        repository.append(
            stream_id="aqa_control:job:unsafe",
            event_type="job.started",
            occurred_at=_OCCURRED_AT,
            payload=_payload({}),
            connection=connection,
        )


def test_concurrent_sqlite_appends_allocate_one_contiguous_chain(
    repository: AuditRepository,
) -> None:
    def append(index: int) -> AuditEvent:
        return repository.append(
            stream_id="aqa_control:job:concurrent",
            event_type="job.progressed",
            occurred_at=_OCCURRED_AT + timedelta(microseconds=index),
            payload=_payload({"ordinal": index}, idempotency_key=f"test:{index}"),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = tuple(pool.map(append, range(24)))

    assert sorted(event.sequence for event in events) == list(range(1, 25))
    report = repository.verify(stream_id="aqa_control:job:concurrent")
    assert report.event_count == 24
    assert report.stream_heads[0].sequence == 24


def test_closed_payload_rejects_sql_text_before_persistence(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    injected = "x'); DROP TABLE aqa_audit_events; --"
    with pytest.raises(AuditValidationError, match="invalid"):
        repository.append(
            stream_id="aqa_control:security:sql",
            event_type="security.input_observed",
            occurred_at=_OCCURRED_AT,
            payload=_payload({"reason_code": injected}),
        )

    with sqlite_engine.connect() as connection:
        assert connection.scalar(select(aqa_audit_events.c.audit_event_id)) is None


@pytest.mark.parametrize(
    "field",
    [
        "audit_event_id",
        "sequence",
        "previous_hash",
        "event_type",
        "payload_hash",
        "event_hash",
        "content_hash",
    ],
)
def test_independent_verifier_detects_every_derived_or_continuity_tamper(
    field: str,
) -> None:
    event = AuditEvent.create(
        stream_id="aqa_control:job:one",
        sequence=1,
        previous_hash=AUDIT_GENESIS_HASH,
        event_type="job.completed",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=_payload({"state": "completed"}),
    )
    replacements: dict[str, object] = {
        "audit_event_id": "audit_" + ("1" * 64),
        "sequence": 2,
        "previous_hash": "1" * 64,
        "event_type": "job.failed",
        "payload_hash": "1" * 64,
        "event_hash": "1" * 64,
        "content_hash": "1" * 64,
    }
    corrupted = replace(event, **{field: replacements[field]})

    with pytest.raises(AuditIntegrityError, match="verification failed"):
        verify_audit_chain((corrupted,))


def test_independent_verifier_rejects_duplicate_idempotency_keys() -> None:
    payload = _payload({"state": "completed"}, idempotency_key="request:duplicate")
    first = AuditEvent.create(
        stream_id="aqa_control:job:duplicate-idempotency",
        sequence=1,
        previous_hash=AUDIT_GENESIS_HASH,
        event_type="job.completed",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=payload,
    )
    second = AuditEvent.create(
        stream_id=first.stream_id,
        sequence=2,
        previous_hash=first.event_hash,
        event_type="job.completed",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=payload,
    )

    with pytest.raises(AuditIntegrityError, match="duplicate idempotency"):
        verify_audit_chain((first, second))


def test_requested_missing_stream_and_incomplete_expected_head_fail_closed(
    repository: AuditRepository,
) -> None:
    with pytest.raises(AuditIntegrityError, match="does not exist"):
        repository.verify(stream_id="aqa_control:job:missing")
    with pytest.raises(AuditValidationError, match="expected audit sequence and hash"):
        repository.verify(stream_id="aqa_control:job:missing", expected_sequence=1)
    with pytest.raises(AuditValidationError, match="exactly one requested stream"):
        repository.verify(expected_sequence=1, expected_hash="0" * 64)


def test_expected_head_detects_valid_looking_tail_deletion(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    events = tuple(_append(repository, offset=offset) for offset in range(3))
    expected_tail = events[-1]
    assert (
        repository.verify(
            stream_id=expected_tail.stream_id,
            expected_sequence=expected_tail.sequence,
            expected_hash=expected_tail.event_hash,
        ).event_count
        == 3
    )

    with sqlite_engine.begin() as connection:
        connection.execute(
            delete(aqa_audit_events).where(
                aqa_audit_events.c.audit_event_id == expected_tail.audit_event_id
            )
        )

    assert repository.verify(stream_id=expected_tail.stream_id).event_count == 2
    with pytest.raises(AuditIntegrityError, match="expected head"):
        repository.verify(
            stream_id=expected_tail.stream_id,
            expected_sequence=expected_tail.sequence,
            expected_hash=expected_tail.event_hash,
        )


def test_audit_verification_report_rejects_invalid_elements_order_duplicates_and_count() -> None:
    first = AuditStreamHead(stream_id="aqa_control:job:a", sequence=1, event_hash="a" * 64)
    second = AuditStreamHead(stream_id="aqa_control:job:b", sequence=2, event_hash="b" * 64)

    assert AuditVerificationReport(event_count=3, stream_heads=(first, second)).event_count == 3
    with pytest.raises(AuditValidationError, match="event count"):
        AuditVerificationReport(event_count=-1, stream_heads=())
    with pytest.raises(AuditValidationError, match="immutable tuple"):
        AuditVerificationReport(event_count=0, stream_heads=[])  # type: ignore[arg-type]
    with pytest.raises(AuditValidationError, match="invalid element"):
        AuditVerificationReport(event_count=1, stream_heads=(object(),))  # type: ignore[arg-type]
    with pytest.raises(AuditValidationError, match="uniquely ordered"):
        AuditVerificationReport(event_count=3, stream_heads=(second, first))
    with pytest.raises(AuditValidationError, match="uniquely ordered"):
        AuditVerificationReport(event_count=2, stream_heads=(first, first))
    with pytest.raises(AuditValidationError, match="count does not match"):
        AuditVerificationReport(event_count=2, stream_heads=(first,))


def test_repository_verifier_detects_persisted_tampering(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    event = _append(repository)
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_audit_events)
            .where(aqa_audit_events.c.audit_event_id == event.audit_event_id)
            .values(event_type="experiment.modified")
        )

    with pytest.raises(AuditIntegrityError, match="verification failed"):
        repository.verify()


def test_append_fails_closed_when_the_persisted_stream_has_a_deep_gap(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    events = tuple(_append(repository, offset=offset) for offset in range(5))
    with sqlite_engine.begin() as connection:
        connection.execute(
            delete(aqa_audit_events).where(
                aqa_audit_events.c.audit_event_id == events[1].audit_event_id
            )
        )

    with pytest.raises(AuditIntegrityError, match="verification failed"):
        _append(repository, offset=5)


def test_append_fails_closed_when_an_older_event_was_tampered(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    events = tuple(_append(repository, offset=offset) for offset in range(5))
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_audit_events)
            .where(aqa_audit_events.c.audit_event_id == events[0].audit_event_id)
            .values(actor="aqa_execution")
        )

    with pytest.raises(AuditIntegrityError, match="malformed"):
        _append(repository, offset=5)


def test_repository_rejects_secret_like_persisted_payload(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    event = _append(repository)
    with sqlite_engine.begin() as connection:
        connection.execute(
            update(aqa_audit_events)
            .where(aqa_audit_events.c.audit_event_id == event.audit_event_id)
            .values(payload={"authorization": "not-a-real-value"})
        )

    with pytest.raises(AuditIntegrityError, match="malformed"):
        repository.verify()


@pytest.mark.parametrize(
    ("column", "persisted_value"),
    [
        ("occurred_at", "private-timestamp-sentinel"),
        ("payload", '{"idempotency_key":"private-json-sentinel"'),
        ("payload", b"\x80private-blob-sentinel"),
        ("payload", ("[" * 1_100) + "0" + ("]" * 1_100)),
    ],
)
def test_repository_normalizes_malformed_sqlite_result_values_without_disclosure(
    repository: AuditRepository,
    sqlite_engine: Engine,
    column: str,
    persisted_value: object,
) -> None:
    event = _append(repository)
    with sqlite_engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE aqa_audit_events SET {column} = ? WHERE audit_event_id = ?",
            (persisted_value, event.audit_event_id),
        )

    with pytest.raises(AuditIntegrityError, match="malformed") as captured:
        repository.verify()

    assert "private" not in str(captured.value)


def test_repository_keeps_sqlalchemy_failures_out_of_the_integrity_error_channel(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    def fail_statement(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, statement, parameters, context, executemany
        raise OperationalError("redacted", {}, RuntimeError("database unavailable"))

    sqlalchemy_event.listen(sqlite_engine, "before_cursor_execute", fail_statement)
    try:
        with pytest.raises(AuditPersistenceError, match="could not be read"):
            repository.verify()
    finally:
        sqlalchemy_event.remove(sqlite_engine, "before_cursor_execute", fail_statement)


def test_repository_redacts_transaction_acquisition_failures(
    repository: AuditRepository,
    sqlite_engine: Engine,
) -> None:
    sentinel = "private-transaction-error"

    def fail_serialization(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if statement == "BEGIN IMMEDIATE":
            raise OperationalError("redacted", {}, RuntimeError(sentinel))

    sqlalchemy_event.listen(sqlite_engine, "before_cursor_execute", fail_serialization)
    try:
        with pytest.raises(AuditPersistenceError, match="could not be persisted") as captured:
            _append(repository)
    finally:
        sqlalchemy_event.remove(sqlite_engine, "before_cursor_execute", fail_serialization)

    assert sentinel not in str(captured.value)


def test_postgres_advisory_lock_requests_are_deterministic_signed_and_globally_ordered() -> None:
    watermark = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_WATERMARK,
        "alpaca:sip:AAPL:1Min",
    )
    identity = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.MARKET_DATA_IDENTITY,
        "bar_" + ("a" * 64),
    )
    audit = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.AUDIT,
        "aqa_control:job:one",
    )

    assert watermark < identity < audit
    assert audit == PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.AUDIT,
        "aqa_control:job:one",
    )
    assert -(2**31) <= audit.resource_key < 2**31


def test_repository_and_verifier_reject_untrusted_contract_shapes(
    sqlite_engine: Engine,
) -> None:
    with pytest.raises(TypeError, match="concrete SQLAlchemy Engine"):
        AuditRepository(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="writer"):
        AuditRepository(sqlite_engine, writer="aqa_control")  # type: ignore[arg-type]

    verifier = AuditRepository(sqlite_engine)
    with pytest.raises(AuditPersistenceError, match="verifier"):
        verifier.transaction()

    with pytest.raises(AuditIntegrityError, match="input is invalid"):
        verify_audit_chain(object())  # type: ignore[arg-type]
    with pytest.raises(AuditIntegrityError, match="input is invalid"):
        verify_audit_chain((object(),))  # type: ignore[arg-type]

    later_stream = AuditEvent.create(
        stream_id="aqa_control:job:z",
        sequence=1,
        previous_hash=AUDIT_GENESIS_HASH,
        event_type="job.completed",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=_payload({}, idempotency_key="unordered-z"),
    )
    earlier_stream = AuditEvent.create(
        stream_id="aqa_control:job:a",
        sequence=1,
        previous_hash=AUDIT_GENESIS_HASH,
        event_type="job.completed",
        actor=AuditWriter.CONTROL,
        occurred_at=_OCCURRED_AT,
        payload=_payload({}, idempotency_key="unordered-a"),
    )
    with pytest.raises(AuditIntegrityError, match="deterministically ordered"):
        verify_audit_chain((later_stream, earlier_stream))


def test_advisory_lock_value_contract_rejects_invalid_namespaces_keys_and_resources() -> None:
    for invalid_namespace in (10, "AUDIT", None):
        with pytest.raises(TypeError, match="closed contract"):
            PostgresAdvisoryLockRequest(
                namespace=invalid_namespace,  # type: ignore[arg-type]
                resource_key=0,
            )

    for invalid_key in (True, -(2**31) - 1, 2**31):
        with pytest.raises(ValueError, match="signed 32-bit"):
            PostgresAdvisoryLockRequest(
                namespace=PostgresAdvisoryLockNamespace.AUDIT,
                resource_key=invalid_key,
            )

    with pytest.raises(TypeError, match="closed contract"):
        PostgresAdvisoryLockRequest.for_resource(
            90,  # type: ignore[arg-type]
            "aqa_control:job:one",
        )
    for invalid_resource in (None, "", "contains space", "x" * 513):
        with pytest.raises(ValueError, match="resource reference"):
            PostgresAdvisoryLockRequest.for_resource(
                PostgresAdvisoryLockNamespace.AUDIT,
                invalid_resource,  # type: ignore[arg-type]
            )

    with pytest.raises(TypeError, match="closed reason contract"):
        TransactionBoundaryError("foreign_connection")  # type: ignore[arg-type]


def test_serialized_transaction_contract_rejects_unsafe_connections_and_lock_sets(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    coordinator = SerializedTransactionCoordinator(sqlite_engine)
    request = PostgresAdvisoryLockRequest.for_resource(
        PostgresAdvisoryLockNamespace.AUDIT,
        "aqa_control:job:transaction-contract",
    )

    with coordinator.transaction() as connection:
        coordinator.validate_connection(connection, require_serialized_sqlite=True)
        coordinator.acquire_postgres_advisory_lock(connection, request)
        coordinator.acquire_postgres_advisory_locks(connection, (request, request))
        with pytest.raises(TypeError, match="typed contract"):
            coordinator.acquire_postgres_advisory_lock(
                connection,
                object(),  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="concrete sequence"):
            coordinator.acquire_postgres_advisory_locks(
                connection,
                "not-a-lock-sequence",  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="typed contract"):
            coordinator.acquire_postgres_advisory_locks(
                connection,
                (object(),),  # type: ignore[arg-type]
            )

    with (
        sqlite_engine.connect() as connection,
        pytest.raises(TransactionBoundaryError) as inactive,
    ):
        coordinator.validate_connection(connection, require_serialized_sqlite=False)
    assert inactive.value.violation is TransactionViolation.INACTIVE_TRANSACTION

    with (
        sqlite_engine.begin() as connection,
        pytest.raises(TransactionBoundaryError) as unserialized,
    ):
        coordinator.validate_connection(connection, require_serialized_sqlite=True)
    assert unserialized.value.violation is TransactionViolation.UNSERIALIZED_SQLITE

    foreign_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'foreign.sqlite3'}")
    try:
        with (
            foreign_engine.begin() as connection,
            pytest.raises(TransactionBoundaryError) as foreign,
        ):
            coordinator.validate_connection(connection, require_serialized_sqlite=False)
        assert foreign.value.violation is TransactionViolation.FOREIGN_CONNECTION
    finally:
        foreign_engine.dispose()

    with pytest.raises(TypeError, match="concrete SQLAlchemy Engine"):
        SerializedTransactionCoordinator(object())  # type: ignore[arg-type]


def test_sqlite_writer_reads_are_scoped_to_their_own_audit_authority(
    sqlite_engine: Engine,
) -> None:
    collector = AuditRepository(sqlite_engine, writer=AuditWriter.COLLECTOR)
    control = AuditRepository(sqlite_engine, writer=AuditWriter.CONTROL)
    collector_event = collector.append(
        stream_id="aqa_collector:data:scope",
        event_type="data.persisted",
        occurred_at=_OCCURRED_AT,
        payload=_payload({"state": "persisted"}, idempotency_key="collector-scope"),
    )
    control_event = control.append(
        stream_id="aqa_control:job:scope",
        event_type="job.completed",
        occurred_at=_OCCURRED_AT,
        payload=_payload({"state": "completed"}, idempotency_key="control-scope"),
    )

    assert collector.list_events() == (collector_event,)
    assert control.list_events() == (control_event,)
    with pytest.raises(AuditIntegrityError, match="does not exist"):
        collector.verify(stream_id=control_event.stream_id)
    with pytest.raises(AuditIntegrityError, match="does not exist"):
        control.verify(stream_id=collector_event.stream_id)
