"""Immutable experiment registration as a deployment-time transaction boundary."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Connection, Engine, and_, insert, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.config import ExperimentDefinition
from adaptive_trader.platform.domain import (
    AuditPayload,
    AuditWriter,
    DeterministicId,
    require_utc_instant,
)
from adaptive_trader.platform.errors import DomainValidationError
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.tables import aqa_experiment_symbols, aqa_experiments
from adaptive_trader.platform.storage.transactions import (
    PostgresAdvisoryLockNamespace,
    PostgresAdvisoryLockRequest,
    SerializedTransactionCoordinator,
)


class ExperimentPersistenceError(RuntimeError):
    """Experiment registration could not preserve immutable identity."""


class ExperimentRepository:
    """Register one content-addressed experiment and its complete symbol partition atomically."""

    def __init__(self, engine: Engine) -> None:
        if not isinstance(engine, Engine) or engine.dialect.name not in {"postgresql", "sqlite"}:
            raise TypeError("experiment repository requires PostgreSQL or SQLite")
        self._engine = engine
        self._transactions = SerializedTransactionCoordinator(engine)
        self._audit = AuditRepository(engine, writer=AuditWriter.MIGRATION)

    def register(
        self,
        experiment: ExperimentDefinition,
        *,
        registered_at: datetime,
    ) -> ExperimentDefinition:
        """Insert an immutable experiment once; identical deployment retries are no-ops."""

        if type(experiment) is not ExperimentDefinition:
            raise TypeError("experiment registration requires an immutable definition")
        try:
            instant = require_utc_instant(registered_at, field_name="registered_at")
        except DomainValidationError:
            raise ExperimentPersistenceError("experiment registration time must be UTC") from None
        configuration = _configuration(experiment)
        symbols = _symbol_rows(experiment, created_at=instant)
        try:
            with self._transactions.transaction() as connection:
                self._transactions.acquire_postgres_advisory_lock(
                    connection,
                    PostgresAdvisoryLockRequest.for_resource(
                        PostgresAdvisoryLockNamespace.EXPERIMENT,
                        f"{experiment.experiment_id}:{experiment.experiment_version}",
                    ),
                )
                rows = (
                    connection.execute(
                        select(aqa_experiments).where(
                            or_(
                                aqa_experiments.c.experiment_hash == experiment.content_hash,
                                and_(
                                    aqa_experiments.c.experiment_id == experiment.experiment_id,
                                    aqa_experiments.c.experiment_version
                                    == experiment.experiment_version,
                                ),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                if rows:
                    if len(rows) != 1 or not _same_experiment(
                        rows[0],
                        experiment=experiment,
                        configuration=configuration,
                    ):
                        raise ExperimentPersistenceError(
                            "experiment identity already belongs to different content"
                        )
                    _verify_symbols(connection, experiment=experiment, expected=symbols)
                    return experiment

                connection.execute(
                    insert(aqa_experiments).values(
                        experiment_hash=experiment.content_hash,
                        experiment_id=experiment.experiment_id,
                        experiment_version=experiment.experiment_version,
                        schema_version=experiment.schema_version,
                        configuration=configuration,
                        content_hash=experiment.content_hash,
                        registered_at=instant,
                    )
                )
                connection.execute(insert(aqa_experiment_symbols), symbols)
                self._audit.append(
                    stream_id=f"{AuditWriter.MIGRATION.value}:experiment:{experiment.content_hash}",
                    event_type="experiment.registered",
                    occurred_at=instant,
                    payload=AuditPayload.from_mapping(
                        {
                            "configuration_hash": experiment.content_hash,
                            "count": len(symbols),
                            "experiment_hash": experiment.content_hash,
                            "idempotency_key": f"experiment_{experiment.content_hash}",
                            "version": experiment.experiment_version,
                        }
                    ),
                    connection=connection,
                )
                return experiment
        except ExperimentPersistenceError:
            raise
        except (IntegrityError, SQLAlchemyError, TypeError, ValueError):
            raise ExperimentPersistenceError("experiment registration failed") from None


def _configuration(experiment: ExperimentDefinition) -> dict[str, JsonValue]:
    decoded = json.loads(canonical_json_bytes(experiment.hash_payload()))
    if type(decoded) is not dict:
        raise ExperimentPersistenceError("experiment configuration is not a mapping")
    return decoded


def _symbol_rows(
    experiment: ExperimentDefinition,
    *,
    created_at: datetime,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    memberships = (
        ("active", experiment.active_tradable),
        ("benchmark", experiment.benchmark_only),
        ("context", experiment.context_only),
        ("excluded", experiment.excluded),
    )
    for role, symbols in memberships:
        for ordinal, symbol in enumerate(symbols):
            digest = sha256_hex(
                {
                    "experiment_hash": experiment.content_hash,
                    "ordinal": ordinal,
                    "role": role,
                    "schema": "experiment-symbol-v1",
                    "symbol": symbol,
                }
            )
            rows.append(
                {
                    "experiment_symbol_id": DeterministicId(
                        prefix="experiment_symbol",
                        digest=digest,
                    ).value,
                    "experiment_hash": experiment.content_hash,
                    "symbol": symbol,
                    "role": role,
                    "ordinal": ordinal,
                    "content_hash": digest,
                    "created_at": created_at,
                }
            )
    return rows


def _same_experiment(
    row: RowMapping,
    *,
    experiment: ExperimentDefinition,
    configuration: dict[str, JsonValue],
) -> bool:
    return bool(
        row["experiment_hash"] == experiment.content_hash
        and row["experiment_id"] == experiment.experiment_id
        and row["experiment_version"] == experiment.experiment_version
        and row["schema_version"] == experiment.schema_version
        and row["configuration"] == configuration
        and row["content_hash"] == experiment.content_hash
    )


def _verify_symbols(
    connection: Connection,
    *,
    experiment: ExperimentDefinition,
    expected: list[dict[str, object]],
) -> None:
    persisted = (
        connection.execute(
            select(aqa_experiment_symbols)
            .where(aqa_experiment_symbols.c.experiment_hash == experiment.content_hash)
            .order_by(aqa_experiment_symbols.c.role, aqa_experiment_symbols.c.ordinal)
        )
        .mappings()
        .all()
    )
    expected_by_id = {str(row["experiment_symbol_id"]): row for row in expected}
    if len(persisted) != len(expected_by_id):
        raise ExperimentPersistenceError("registered experiment symbol partition is incomplete")
    for row in persisted:
        expected_row = expected_by_id.get(str(row["experiment_symbol_id"]))
        if expected_row is None or any(
            row[field] != expected_row[field]
            for field in ("experiment_hash", "symbol", "role", "ordinal", "content_hash")
        ):
            raise ExperimentPersistenceError("registered experiment symbol partition is invalid")


__all__ = ["ExperimentPersistenceError", "ExperimentRepository"]
