#!/usr/bin/env python3
"""Destructive, loopback-only PostgreSQL logical backup/restore smoke test.

The source database must be the explicitly disposable ``collector_test`` database. The
script resets its application schemas, writes deterministic synthetic platform state,
restores a plain-text logical dump into a uniquely named fresh database, verifies both
copies, and removes the restored database. It never prints a database URL or password.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import Engine, MetaData, create_engine, insert, inspect, select, text
from sqlalchemy.engine import URL
from sqlalchemy.schema import DropSchema

from adaptive_trader.collection.migrations import (
    database_revision,
    require_database_at_head,
    upgrade_database,
)
from adaptive_trader.collection.postgres import normalize_postgres_url, postgres_connect_args
from adaptive_trader.collection.schema import SCHEMA_NAME as MARKET_DATA_SCHEMA
from adaptive_trader.platform.canonical import canonical_json_bytes
from adaptive_trader.platform.domain import AuditPayload, AuditWriter
from adaptive_trader.platform.execution import (
    DeterministicFakePaperBroker,
    ExecutionPlanningRequest,
    ExecutionService,
    Position,
    ReconciliationRequest,
    SubmissionSafetySnapshot,
    plan_signed_orders,
    reconcile,
)
from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.platform.risk import (
    AccountSnapshot,
    ExposureSnapshot,
    PlanningPrice,
    RiskDecision,
    RiskExecutionScope,
    SecurityMetadataSnapshot,
    SignedPosition,
)
from adaptive_trader.platform.risk.latches import RiskLatchState
from adaptive_trader.platform.storage.execution import SignedExecutionRepository
from adaptive_trader.platform.storage.market_data import BarIdentity, BarWrite, MarketDataRepository
from adaptive_trader.platform.storage.repositories import AuditRepository
from adaptive_trader.platform.storage.risk import SignedRiskRepository
from adaptive_trader.platform.storage.tables import (
    PLATFORM_SCHEMA,
    aqa_decision_slots,
    aqa_experiments,
    aqa_signal_envelopes,
)

_SOURCE_DATABASE = "collector_test"
_RESTORE_PREFIX = "collector_test_restore_"
_FIXTURE_TIME = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
_EXPERIMENT_HASH = "b" * 64
_SIGNAL_HASH = "a" * 64
_POLICY_HASH = "c" * 64
_SLOT_ID = f"slot_{'1' * 64}"
_SIGNAL_ID = f"signal_{'2' * 64}"
_CORRELATION_ID = f"correlation_{'d' * 64}"
_REQUIRED_RELATIONS = (
    "aqa_audit_events",
    "aqa_bar_events",
    "aqa_decision_slots",
    "aqa_fills",
    "aqa_order_intents",
    "aqa_reconciliations",
)
_PROHIBITED_FIXTURE_KEYS = (
    b'"alpaca_api_key"',
    b'"alpaca_secret_key"',
    b'"api_key"',
    b'"authorization"',
    b'"password"',
    b'"secret"',
    b'"token"',
)


class BackupRestoreError(RuntimeError):
    """The guarded backup/restore proof could not complete safely."""


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    """Portable content evidence for one migrated database."""

    revision: str
    tables: tuple[tuple[str, int, tuple[str, ...]], ...]
    audit_chain_root: str
    privileges: tuple[tuple[str, str, str, str, bool], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "audit_chain_root": self.audit_chain_root,
            "revision": self.revision,
            "privileges": self.privileges,
            "tables": {
                name: {"content_hashes": hashes, "row_count": count}
                for name, count, hashes in self.tables
            },
        }

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.as_dict())


def _source_url() -> URL:
    raw = os.environ.get("APA_TEST_POSTGRES_URL", "").strip()
    if not raw:
        raise BackupRestoreError("APA_TEST_POSTGRES_URL is required")
    if os.environ.get("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE") != "YES":
        raise BackupRestoreError("APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE=YES is required")
    try:
        url = normalize_postgres_url(raw)
    except (TypeError, ValueError):
        raise BackupRestoreError("the PostgreSQL test URL is invalid") from None
    if url.host not in {"127.0.0.1", "::1", "localhost"}:
        raise BackupRestoreError("backup/restore smoke requires a loopback PostgreSQL host")
    if url.database != _SOURCE_DATABASE:
        raise BackupRestoreError(f"backup/restore smoke requires the {_SOURCE_DATABASE} database")
    if not url.username or not url.password:
        raise BackupRestoreError("backup/restore smoke requires explicit database credentials")
    if any(
        os.environ.get(name) for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")
    ):
        raise BackupRestoreError(
            "backup/restore smoke rejects inherited PostgreSQL routing settings"
        )
    return url


def _render_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _engine(url: URL, *, application_name: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args=postgres_connect_args(application_name, migration=True),
    )


def _pg_environment(url: URL) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    if url.password is not None:
        environment["PGPASSWORD"] = url.password
    environment["PGCONNECT_TIMEOUT"] = "10"
    return environment


def _pg_connection_arguments(url: URL, *, database: str) -> list[str]:
    if url.host is None or url.username is None:
        raise BackupRestoreError("PostgreSQL URL must include a host and username")
    return [
        "--host",
        url.host,
        "--port",
        str(url.port or 5432),
        "--username",
        url.username,
        "--dbname",
        database,
    ]


def _utility(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise BackupRestoreError(f"required PostgreSQL utility is unavailable: {name}")
    return executable


def _run(command: list[str], *, environment: dict[str, str], utility: str) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            env=environment,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        raise BackupRestoreError(f"PostgreSQL utility failed: {utility}") from None


def _reset_and_migrate(url: URL) -> None:
    engine = _engine(url, application_name="aqa-backup-smoke-reset")
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(PLATFORM_SCHEMA, cascade=True, if_exists=True))
            connection.execute(DropSchema(MARKET_DATA_SCHEMA, cascade=True, if_exists=True))
    finally:
        engine.dispose()
    upgrade_database(_render_url(url))
    require_database_at_head(_render_url(url))


def _exposure(target: Decimal) -> ExposureSnapshot:
    return ExposureSnapshot(
        gross=abs(target),
        net=target,
        positive=max(target, Decimal(0)),
        short_abs=max(-target, Decimal(0)),
        group_gross=(),
        cluster_gross=(),
    )


def _risk_decision() -> RiskDecision:
    target = Decimal("0.10")
    exposure = _exposure(target)
    return RiskDecision.create(
        slot_id=_SLOT_ID,
        signal_id=_SIGNAL_ID,
        signal_hash=_SIGNAL_HASH,
        experiment_hash=_EXPERIMENT_HASH,
        policy_id="backup_smoke",
        policy_version=1,
        policy_hash=_POLICY_HASH,
        correlation_id=_CORRELATION_ID,
        decided_at=_FIXTURE_TIME,
        input_hash=sha256_hex(("backup-smoke-risk-input", target)),
        statistics_hash=sha256_hex(("backup-smoke-statistics", target)),
        account_snapshot=AccountSnapshot(
            account_id_hash=sha256_hex(
                ("broker-account-v1", DeterministicFakePaperBroker.INITIAL_ACCOUNT_ID)
            ),
            equity=Decimal(1000),
            cash=Decimal(1000),
            buying_power=Decimal(1000),
            observed_at=_FIXTURE_TIME,
        ),
        planning_positions=(SignedPosition("AAA", Decimal(0)),),
        planning_prices=(PlanningPrice("AAA", Decimal(100), _FIXTURE_TIME, True),),
        security_metadata=(
            SecurityMetadataSnapshot(
                "AAA",
                True,
                True,
                True,
                True,
                True,
                True,
                _FIXTURE_TIME,
            ),
        ),
        original_proposal=(("AAA", "LONG", None, target),),
        proposed_targets=(("AAA", target),),
        final_targets=(("AAA", target),),
        before_exposure=exposure,
        after_exposure=exposure,
        ordered_controls=(),
        block_reasons=(),
        flatten_reasons=(),
        source_timestamps=(
            ("account", _FIXTURE_TIME),
            ("reconciliation", _FIXTURE_TIME),
        ),
        latch_state_hash=RiskLatchState.empty(experiment_hash=_EXPERIMENT_HASH).content_hash,
        active_latches=(),
        required_latch_events=(),
        execution_scope=RiskExecutionScope.FULL,
    )


def _seed_dependencies(engine: Engine, decision: RiskDecision) -> None:
    source_start = _FIXTURE_TIME - timedelta(minutes=15)
    with engine.begin() as connection:
        connection.execute(
            insert(aqa_experiments).values(
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="backup_restore_fixture",
                experiment_version=1,
                schema_version=1,
                configuration={"fixture": "deterministic_synthetic"},
                content_hash=_EXPERIMENT_HASH,
                registered_at=source_start,
            )
        )
        connection.execute(
            insert(aqa_decision_slots).values(
                slot_id=_SLOT_ID,
                experiment_hash=_EXPERIMENT_HASH,
                experiment_id="backup_restore_fixture",
                experiment_version=1,
                signal_provider_id="deterministic_fixture",
                signal_provider_version="1",
                session_date=date(2026, 7, 6),
                source_interval_start=source_start,
                source_interval_end=_FIXTURE_TIME,
                decision_type="strategy",
                ready_at=_FIXTURE_TIME,
                deadline_at=_FIXTURE_TIME + timedelta(minutes=1),
                required_completion_at=_FIXTURE_TIME + timedelta(minutes=2),
                state="READY",
                claim_owner=None,
                claimed_at=None,
                lease_expires_at=None,
                attempt_count=0,
                completed_at=None,
                reason_code=None,
                correlation_id=_CORRELATION_ID,
                content_hash="6" * 64,
                version=1,
                created_at=source_start,
                updated_at=source_start,
            )
        )
        connection.execute(
            insert(aqa_signal_envelopes).values(
                signal_id=_SIGNAL_ID,
                slot_id=_SLOT_ID,
                experiment_hash=_EXPERIMENT_HASH,
                provider_id="deterministic_fixture",
                provider_version="1",
                contract_version=1,
                correlation_id=_CORRELATION_ID,
                provider_source_mode="builtin",
                experiment_id="backup_restore_fixture",
                experiment_version=1,
                data_contract_hash="7" * 64,
                policy_hash=_POLICY_HASH,
                source_bar_end=_FIXTURE_TIME,
                created_at=_FIXTURE_TIME,
                expires_at=_FIXTURE_TIME + timedelta(minutes=1),
                active_symbols=["AAA"],
                availability_mask=[True],
                actions=["LONG"],
                expected_edge_bps=[None],
                proposed_signed_target_inputs=["0.10"],
                artifact_id=None,
                artifact_hash=None,
                promotable=False,
                paper_submission_eligible=False,
                content_hash=_SIGNAL_HASH,
            )
        )


def _seed_market_data(engine: Engine) -> None:
    start = _FIXTURE_TIME - timedelta(minutes=1)
    write = BarWrite(
        identity=BarIdentity(
            provider="fixture",
            feed="synthetic",
            adjustment="raw",
            symbol="AAA",
            timeframe="1Min",
            start_at=start,
            end_at=_FIXTURE_TIME,
        ),
        received_at=_FIXTURE_TIME,
        provider_timestamp=_FIXTURE_TIME,
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal("100.25"),
        volume=Decimal(1000),
        trade_count=10,
        vwap=Decimal("100.10"),
        quality_flags=("complete",),
        source="fixture",
        source_payload_hash=sha256_hex(("backup-smoke-bar", 1)),
        source_mode="offline_fixture",
        source_event_id="backup-smoke-bar-1",
    )
    MarketDataRepository(engine).append(write)


def _seed_execution(engine: Engine, decision: RiskDecision) -> None:
    SignedRiskRepository(engine).persist(decision)
    planning = plan_signed_orders(
        ExecutionPlanningRequest(
            risk_decision=decision,
            current_positions=(Position("AAA", Decimal(0)),),
            reference_prices=(("AAA", Decimal(100)),),
            equity=Decimal(1000),
            target_version=1,
            created_at=_FIXTURE_TIME + timedelta(seconds=1),
            deadline_at=_FIXTURE_TIME + timedelta(minutes=1),
        )
    )
    repository = SignedExecutionRepository(engine)
    submitted_at = _FIXTURE_TIME + timedelta(seconds=2)
    broker = DeterministicFakePaperBroker(
        initial_time=submitted_at,
        initial_cash=decision.account_snapshot.cash,
    )
    broker.set_mark_prices((("AAA", Decimal(100)),))
    safety = SubmissionSafetySnapshot(
        evaluated_at=submitted_at,
        session_open=True,
        data_complete=True,
        account_observed_at=submitted_at,
        security_observed_at=_FIXTURE_TIME,
        reconciliation_observed_at=_FIXTURE_TIME,
        price_observed_at=_FIXTURE_TIME,
        reconciliation_clean=True,
        ambiguous_order_exists=False,
        blocking_latch_exists=False,
        entry_disabled=False,
        active_symbols=("AAA",),
        shortable_symbols=("AAA",),
    )
    outcomes = ExecutionService(repository=repository, broker=broker).submit_plan(
        planning,
        safety_provider=lambda _intent: safety,
    )
    if len(outcomes) != 1 or not outcomes[0].state.terminal:
        raise BackupRestoreError("deterministic execution fixture did not fill")
    completed_at = submitted_at + timedelta(seconds=1)
    account = broker.account(observed_at=completed_at)
    request = ReconciliationRequest(
        experiment_hash=_EXPERIMENT_HASH,
        slot_id=_SLOT_ID,
        execution_plan_id=planning.plan.execution_plan_id,
        correlation_id=_CORRELATION_ID,
        active_symbols=("AAA",),
        short_eligible_symbols=("AAA",),
        baseline_positions=planning.plan.current_positions,
        baseline_cash=decision.account_snapshot.cash,
        fills=repository.fills(),
        order_fills=repository.fills(),
        intents=repository.all_intents(),
        durable_orders=repository.all_orders(),
        broker_orders=repository.all_orders(),
        broker_positions=broker.positions(),
        broker_account=account,
        expected_account_id_hash=decision.account_snapshot.account_id_hash,
        mark_prices=planning.plan.reference_prices,
        started_at=submitted_at,
        completed_at=completed_at,
    )
    receipt = reconcile(request)
    if receipt.discrepancies:
        raise BackupRestoreError("deterministic execution fixture did not reconcile")
    repository.record_reconciliation_bundle(
        receipt,
        request=request,
        latch_event=None,
        incident=None,
    )


def _populate_fixture(url: URL) -> None:
    engine = _engine(url, application_name="aqa-backup-smoke-fixture")
    try:
        decision = _risk_decision()
        _seed_dependencies(engine, decision)
        _seed_market_data(engine)
        _seed_execution(engine, decision)
        AuditRepository(engine, writer=AuditWriter.CONTROL).append(
            stream_id="aqa_control:backup_restore:fixture",
            event_type="control.backup_fixture_ready",
            occurred_at=_FIXTURE_TIME + timedelta(seconds=4),
            payload=AuditPayload.from_mapping(
                {
                    "reason_code": "deterministic_synthetic_fixture",
                    "idempotency_key": "backup_" + sha256_hex("backup_restore_fixture_ready"),
                }
            ),
        )
    finally:
        engine.dispose()


def _portable_value(value: object) -> object:
    """Normalize PostgreSQL driver date values without changing stored semantics."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise BackupRestoreError("backup fixture contains a naive timestamp")
        return value.astimezone(UTC)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _row_hashes(engine: Engine, schema: str, table_name: str) -> tuple[str, ...]:
    reflected = MetaData()
    reflected.reflect(bind=engine, schema=schema, only=[table_name])
    table = reflected.tables[f"{schema}.{table_name}"]
    primary = tuple(table.primary_key.columns)
    order_columns = primary or tuple(table.columns)
    with engine.connect() as connection:
        rows = connection.execute(select(table).order_by(*order_columns)).mappings()
        hashes: list[str] = []
        for row in rows:
            payload = {
                str(column.name): _portable_value(row[column.name]) for column in table.columns
            }
            serialized = canonical_json_bytes(payload).lower()
            if any(key in serialized for key in _PROHIBITED_FIXTURE_KEYS):
                raise BackupRestoreError("backup fixture contains a credential-shaped field")
            hashes.append(sha256_hex(payload))
        return tuple(hashes)


def _database_privileges(engine: Engine, database: str) -> tuple[tuple[str, str, bool], ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE r.rolname END AS grantee, "
                "a.privilege_type, a.is_grantable FROM pg_catalog.pg_database d "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))) a "
                "LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee WHERE d.datname = :database "
                "ORDER BY grantee, a.privilege_type, a.is_grantable"
            ),
            {"database": database},
        ).all()
    return tuple((str(row[0]), str(row[1]), bool(row[2])) for row in rows)


def _restore_database_privileges(source: URL, target_name: str) -> None:
    """Replay only the source's database ACL onto the newly generated drill database."""
    if not target_name.startswith(_RESTORE_PREFIX) or target_name == _SOURCE_DATABASE:
        raise BackupRestoreError("restore database privilege target is unsafe")
    engine = _engine(source, application_name="aqa-backup-smoke-acl")
    try:
        source_acl = _database_privileges(engine, _SOURCE_DATABASE)
        target_acl = _database_privileges(engine, target_name)
        quote = engine.dialect.identifier_preparer.quote_identifier
        target = quote(target_name)
        with engine.begin() as connection:
            for role in sorted({entry[0] for entry in source_acl + target_acl}):
                grantee = "PUBLIC" if role == "PUBLIC" else quote(role)
                connection.execute(
                    text(f"REVOKE ALL PRIVILEGES ON DATABASE {target} FROM {grantee}")
                )
            for role, privilege, grantable in source_acl:
                if privilege not in {"CONNECT", "CREATE", "TEMPORARY"}:
                    raise BackupRestoreError("source database privilege is unsupported")
                grantee = "PUBLIC" if role == "PUBLIC" else quote(role)
                option = " WITH GRANT OPTION" if grantable else ""
                connection.execute(
                    text(f"GRANT {privilege} ON DATABASE {target} TO {grantee}{option}")
                )
    finally:
        engine.dispose()


def _privilege_snapshot(engine: Engine) -> tuple[tuple[str, str, str, str, bool], ...]:
    """Compare semantic database, schema, relation and column ACLs, independent of ACL order."""
    with engine.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT 'relation:' || n.nspname AS scope, c.relname AS object_name,
                   CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE r.rolname END AS grantee,
                   a.privilege_type, a.is_grantable
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(c.relacl,
                pg_catalog.acldefault(CASE WHEN c.relkind = 'S' THEN 'S' ELSE 'r' END::"char", c.relowner))) a
            LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
            WHERE n.nspname IN ('aqa', 'market_data') AND c.relkind IN ('r', 'p', 'v', 'S')
            UNION ALL
            SELECT 'schema', n.nspname, CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE r.rolname END,
                   a.privilege_type, a.is_grantable
            FROM pg_catalog.pg_namespace n
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(n.nspacl, pg_catalog.acldefault('n', n.nspowner))) a
            LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
            WHERE n.nspname IN ('aqa', 'market_data')
            UNION ALL
            SELECT 'column:' || n.nspname, c.relname || '.' || att.attname,
                   CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE r.rolname END,
                   a.privilege_type, a.is_grantable
            FROM pg_catalog.pg_attribute att JOIN pg_catalog.pg_class c ON c.oid = att.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(att.attacl) a
            LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
            WHERE n.nspname IN ('aqa', 'market_data') AND att.attnum > 0 AND NOT att.attisdropped
        """)
        ).all()
        database = connection.scalar(text("SELECT current_database()"))
    acl = [(str(row[0]), str(row[1]), str(row[2]), str(row[3]), bool(row[4])) for row in rows]
    acl.extend(
        ("database", "current", role, privilege, grantable)
        for role, privilege, grantable in _database_privileges(engine, str(database))
    )
    return _effective_privileges(acl)


def _effective_privileges(
    entries: list[tuple[str, str, str, str, bool]],
) -> tuple[tuple[str, str, str, str, bool], ...]:
    # ACLs may contain the same right from several grantors. The effective right is
    # grantable if any grantor supplied a grant option; a second plain grant adds no power.
    effective: dict[tuple[str, str, str, str], bool] = {}
    for scope, name, role, privilege, grantable in entries:
        key = (scope, name, role, privilege)
        effective[key] = effective.get(key, False) or grantable
    return tuple(sorted((*key, grantable) for key, grantable in effective.items()))


def _snapshot(url: URL) -> DatabaseSnapshot:
    rendered = _render_url(url)
    current, expected = database_revision(rendered)
    if current is None or current != expected:
        raise BackupRestoreError("database is not at the expected migration revision")
    engine = _engine(url, application_name="aqa-backup-smoke-verify")
    try:
        inspector = inspect(engine)
        names = tuple(sorted(inspector.get_table_names(schema=PLATFORM_SCHEMA)))
        tables = tuple(
            (name, len(hashes), hashes)
            for name in names
            for hashes in (_row_hashes(engine, PLATFORM_SCHEMA, name),)
        )
        counts = {name: count for name, count, _ in tables}
        missing = tuple(name for name in _REQUIRED_RELATIONS if counts.get(name, 0) < 1)
        if missing:
            raise BackupRestoreError(
                "deterministic fixture is missing required state: " + ", ".join(missing)
            )
        audit = AuditRepository(engine).verify()
        audit_root = sha256_hex(
            tuple((head.stream_id, head.sequence, head.event_hash) for head in audit.stream_heads)
        )
        return DatabaseSnapshot(
            revision=current,
            tables=tables,
            audit_chain_root=audit_root,
            privileges=_privilege_snapshot(engine),
        )
    finally:
        engine.dispose()


def _assert_dump_has_no_connection_secret(path: Path, source_url: URL) -> None:
    payload = path.read_bytes()
    password = source_url.password
    if password and password.encode() in payload:
        raise BackupRestoreError("logical backup unexpectedly contains the connection password")


def _create_database(source: URL, target_name: str, environment: dict[str, str]) -> None:
    arguments = _pg_connection_arguments(source, database="postgres")
    command = [
        _utility("createdb"),
        *arguments[:-2],
        "--template",
        "template0",
        "--encoding",
        "UTF8",
        "--owner",
        AuditWriter.MIGRATION.value,
        target_name,
    ]
    _run(command, environment=environment, utility="createdb")


def _drop_database(source: URL, target_name: str, environment: dict[str, str]) -> None:
    arguments = _pg_connection_arguments(source, database="postgres")
    command = [
        _utility("dropdb"),
        *arguments[:-2],
        "--if-exists",
        target_name,
    ]
    _run(command, environment=environment, utility="dropdb")


def run_smoke() -> dict[str, object]:
    """Run the complete guarded logical backup/restore proof and return safe evidence."""

    source = _source_url()
    for utility in ("createdb", "dropdb", "pg_dump", "psql"):
        _utility(utility)
    target_name = f"{_RESTORE_PREFIX}{uuid4().hex[:12]}"
    if not target_name.startswith(_RESTORE_PREFIX) or len(target_name) > 63:
        raise BackupRestoreError("generated restore database name is unsafe")
    target = source.set(database=target_name)
    environment = _pg_environment(source)
    created = False
    with TemporaryDirectory(prefix="aqa-backup-restore-") as directory:
        backup_path = Path(directory) / "platform.sql"
        try:
            _reset_and_migrate(source)
            _populate_fixture(source)
            before = _snapshot(source)
            _run(
                [
                    _utility("pg_dump"),
                    *_pg_connection_arguments(source, database=_SOURCE_DATABASE),
                    "--format=plain",
                    "--no-owner",
                    "--file",
                    str(backup_path),
                ],
                environment=environment,
                utility="pg_dump",
            )
            _assert_dump_has_no_connection_secret(backup_path, source)
            _create_database(source, target_name, environment)
            created = True
            _restore_database_privileges(source, target_name)
            _run(
                [
                    _utility("psql"),
                    "--no-psqlrc",
                    *_pg_connection_arguments(source, database=target_name),
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--command",
                    "SET ROLE " + AuditWriter.MIGRATION.value,
                    "--file",
                    str(backup_path),
                ],
                environment=environment,
                utility="psql",
            )
            upgrade_database(_render_url(target))
            require_database_at_head(_render_url(target))
            after = _snapshot(target)
            if before != after:
                raise BackupRestoreError("restored logical state differs from the source")
            return {
                "backup_format": "postgresql_plain_logical",
                "content_hash": before.content_hash,
                "credential_values_in_fixture": False,
                "required_relations": {
                    name: next(count for table, count, _ in before.tables if table == name)
                    for name in _REQUIRED_RELATIONS
                },
                "revision": before.revision,
                "source_and_restore_identical": True,
                "privileges_preserved": True,
                "status": "ok",
            }
        finally:
            if created:
                _drop_database(source, target_name, environment)


def main() -> None:
    try:
        # Alembic configures migration progress on stderr. The smoke command exposes one
        # machine-readable result; failures still leave this context before propagating.
        with redirect_stderr(StringIO()):
            report = run_smoke()
    except BackupRestoreError as error:
        print(json.dumps({"error": str(error), "status": "failed"}, sort_keys=True))
        raise SystemExit(1) from None
    json.dump(report, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
