"""Install signed execution, order, fill, and reconciliation contracts.

Revision ID: 20260905_0008
Revises: 20260905_0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0008"
down_revision: str | None = "20260905_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORDER_STATES = (
    "'PLANNED', 'INTENT_COMMITTED', 'SUBMISSION_STARTED', 'SUBMITTED', "
    "'ACCEPTED', 'PENDING', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_REQUESTED', "
    "'CANCELED', 'REJECTED', 'EXPIRED', 'SUBMISSION_UNKNOWN', "
    "'RECONCILIATION_REQUIRED'"
)
_EFFECTS = (
    "'OPEN_LONG', 'INCREASE_LONG', 'REDUCE_LONG', 'CLOSE_LONG', "
    "'OPEN_SHORT', 'INCREASE_SHORT', 'REDUCE_SHORT', 'CLOSE_SHORT', "
    "'FORCED_FLAT_LONG', 'FORCED_FLAT_SHORT'"
)
_EMPTY_EXECUTION_GUARD = sa.text(
    """
    DO $aqa_signed_execution_contract_guard$
    BEGIN
        IF EXISTS (SELECT 1 FROM aqa.aqa_execution_plans LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_order_intents LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_broker_orders LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_order_events LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_fills LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_reconciliations LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_incidents LIMIT 1)
           OR EXISTS (SELECT 1 FROM aqa.aqa_risk_decisions LIMIT 1) THEN
            RAISE EXCEPTION
                'revision 20260905_0008 requires an explicit signed-execution backfill '
                'for existing rows';
        END IF;
    END
    $aqa_signed_execution_contract_guard$
    """
)


def upgrade() -> None:
    """Replace unused provisional relations with the signed execution ledger."""

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("revision 20260905_0008 requires PostgreSQL")
    op.execute("SET ROLE aqa_migrate")
    provisional_tables = (
        "aqa_execution_plans",
        "aqa_order_intents",
        "aqa_broker_orders",
        "aqa_order_events",
        "aqa_fills",
        "aqa_reconciliations",
        "aqa_incidents",
    )
    op.execute(
        "GRANT UPDATE ON TABLE "
        + ", ".join(f"aqa.{table}" for table in (*provisional_tables, "aqa_risk_decisions"))
        + " TO aqa_migrate"
    )
    op.execute(
        "LOCK TABLE "
        + ", ".join(f"aqa.{table}" for table in (*provisional_tables, "aqa_risk_decisions"))
        + " IN ACCESS EXCLUSIVE MODE"
    )
    connection.execute(_EMPTY_EXECUTION_GUARD)
    _extend_risk_decision_contract()
    _drop_provisional_contract()
    _create_execution_plans()
    _create_order_intents()
    _create_broker_orders()
    _create_order_events()
    _create_fills()
    _create_reconciliations()
    _create_incidents()
    _create_read_views_and_grants()


def _extend_risk_decision_contract() -> None:
    for column in (
        sa.Column(
            "account_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "planning_positions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "planning_prices",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "security_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    ):
        op.add_column("aqa_risk_decisions", column, schema="aqa")
    op.execute(
        """
        CREATE OR REPLACE VIEW aqa.aqa_risk_decisions_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT risk_decision_id, slot_id, signal_id, experiment_hash, policy_id,
               policy_version, decided_at, signal_hash, policy_hash, statistics_hash,
               latch_state_hash, correlation_id, execution_scope, proposed_targets,
               approved_targets, before_exposure, after_exposure, controls, reason_codes,
               active_latches, required_latch_event_ids, gross_exposure, net_exposure,
               cash_weight, content_hash, account_snapshot, planning_positions,
               planning_prices, security_metadata
        FROM aqa.aqa_risk_decisions
        """
    )


def _drop_provisional_contract() -> None:
    for view in (
        "aqa_orders_v",
        "aqa_fills_v",
        "aqa_reconciliations_v",
        "aqa_incidents_v",
        "aqa_execution_plans_v",
    ):
        op.execute(f"DROP VIEW aqa.{view}")
    for table in (
        "aqa_order_events",
        "aqa_fills",
        "aqa_broker_orders",
        "aqa_order_intents",
        "aqa_reconciliations",
        "aqa_execution_plans",
        "aqa_incidents",
    ):
        op.drop_table(table, schema="aqa")


def _create_execution_plans() -> None:
    op.create_table(
        "aqa_execution_plans",
        sa.Column("execution_plan_id", sa.String(length=128), nullable=False),
        sa.Column("risk_decision_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("risk_decision_hash", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("forced_flat", sa.Boolean(), nullable=False),
        sa.Column(
            "targets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "current_positions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "reference_prices",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("equity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "target_version BETWEEN 1 AND 999999",
            name=op.f("ck_aqa_execution_plans_execution_target_version_range"),
        ),
        sa.CheckConstraint(
            "created_at < deadline_at",
            name=op.f("ck_aqa_execution_plans_execution_deadline_ordered"),
        ),
        sa.CheckConstraint(
            "equity > 0",
            name=op.f("ck_aqa_execution_plans_execution_equity_positive"),
        ),
        _finite_check("aqa_execution_plans", "equity"),
        *(_hash_check("aqa_execution_plans", name) for name in _plan_hash_columns()),
        sa.ForeignKeyConstraint(
            ["risk_decision_id"],
            ["aqa.aqa_risk_decisions.risk_decision_id"],
            name=op.f("fk_aqa_execution_plans_risk_decision_id_aqa_risk_decisions"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_execution_plans_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint(
            "execution_plan_id",
            name=op.f("pk_aqa_execution_plans"),
        ),
        sa.UniqueConstraint(
            "risk_decision_id",
            "target_version",
            name="execution_risk_target_version",
        ),
        sa.UniqueConstraint(
            "content_hash",
            name=op.f("uq_aqa_execution_plans_content_hash"),
        ),
        schema="aqa",
    )


def _plan_hash_columns() -> tuple[str, ...]:
    return ("risk_decision_hash", "payload_hash", "signature", "content_hash")


def _create_order_intents() -> None:
    op.create_table(
        "aqa_order_intents",
        sa.Column("order_intent_id", sa.String(length=128), nullable=False),
        sa.Column("execution_plan_id", sa.String(length=128), nullable=False),
        sa.Column("risk_decision_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("notional", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("reference_price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("final_target_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("forced_flat", sa.Boolean(), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("time_in_force", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_aqa_order_intents_order_side")),
        sa.CheckConstraint(
            f"effect IN ({_EFFECTS})",
            name=op.f("ck_aqa_order_intents_order_effect"),
        ),
        sa.CheckConstraint(
            "phase IN ('EXIT', 'ENTRY', 'FLATTEN')",
            name=op.f("ck_aqa_order_intents_order_phase"),
        ),
        sa.CheckConstraint(
            "sequence BETWEEN 0 AND 15",
            name=op.f("ck_aqa_order_intents_order_sequence_range"),
        ),
        sa.CheckConstraint(
            "target_version BETWEEN 1 AND 999999",
            name=op.f("ck_aqa_order_intents_order_target_version_range"),
        ),
        sa.CheckConstraint(
            "quantity > 0 AND notional > 0 AND reference_price > 0 "
            "AND notional = quantity * reference_price",
            name=op.f("ck_aqa_order_intents_order_numbers_valid"),
        ),
        sa.CheckConstraint(
            "order_type = 'MARKET'", name=op.f("ck_aqa_order_intents_order_type_market")
        ),
        sa.CheckConstraint(
            "time_in_force = 'DAY'", name=op.f("ck_aqa_order_intents_order_time_in_force_day")
        ),
        sa.CheckConstraint(
            "created_at < deadline_at",
            name=op.f("ck_aqa_order_intents_order_deadline_ordered"),
        ),
        sa.CheckConstraint(
            "((effect IN ('OPEN_LONG', 'INCREASE_LONG') AND side = 'BUY' AND phase = 'ENTRY') "
            "OR (effect IN ('OPEN_SHORT', 'INCREASE_SHORT') AND side = 'SELL' AND phase = 'ENTRY') "
            "OR (effect IN ('REDUCE_LONG', 'CLOSE_LONG') AND side = 'SELL' AND phase = 'EXIT') "
            "OR (effect IN ('REDUCE_SHORT', 'CLOSE_SHORT') AND side = 'BUY' AND phase = 'EXIT') "
            "OR (effect = 'FORCED_FLAT_LONG' AND side = 'SELL' AND phase = 'FLATTEN') "
            "OR (effect = 'FORCED_FLAT_SHORT' AND side = 'BUY' AND phase = 'FLATTEN'))",
            name=op.f("ck_aqa_order_intents_order_effect_side_phase_consistent"),
        ),
        sa.CheckConstraint(
            "((forced_flat AND phase = 'FLATTEN' AND "
            "effect IN ('FORCED_FLAT_LONG', 'FORCED_FLAT_SHORT')) OR "
            "(NOT forced_flat AND phase <> 'FLATTEN' AND "
            "effect NOT IN ('FORCED_FLAT_LONG', 'FORCED_FLAT_SHORT')))",
            name=op.f("ck_aqa_order_intents_order_forced_flat_consistent"),
        ),
        _finite_check(
            "aqa_order_intents",
            "quantity",
            "notional",
            "reference_price",
            "final_target_quantity",
        ),
        *(
            _hash_check("aqa_order_intents", name)
            for name in ("target_hash", "payload_hash", "content_hash")
        ),
        sa.ForeignKeyConstraint(
            ["execution_plan_id"],
            ["aqa.aqa_execution_plans.execution_plan_id"],
            name=op.f("fk_aqa_order_intents_execution_plan_id_aqa_execution_plans"),
        ),
        sa.ForeignKeyConstraint(
            ["risk_decision_id"],
            ["aqa.aqa_risk_decisions.risk_decision_id"],
            name=op.f("fk_aqa_order_intents_risk_decision_id_aqa_risk_decisions"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_order_intents_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("order_intent_id", name=op.f("pk_aqa_order_intents")),
        sa.UniqueConstraint(
            "client_order_id",
            name=op.f("uq_aqa_order_intents_client_order_id"),
        ),
        sa.UniqueConstraint(
            "content_hash",
            name=op.f("uq_aqa_order_intents_content_hash"),
        ),
        sa.UniqueConstraint(
            "execution_plan_id",
            "phase",
            "sequence",
            "symbol",
            name="order_plan_phase_sequence_symbol",
        ),
        sa.UniqueConstraint(
            "client_order_id",
            "order_intent_id",
            name="order_client_intent_identity",
        ),
        schema="aqa",
    )


def _create_broker_orders() -> None:
    op.create_table(
        "aqa_broker_orders",
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("order_intent_id", sa.String(length=128), nullable=False),
        sa.Column("broker_order_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cumulative_filled_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("average_fill_price", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            f"state IN ({_ORDER_STATES})",
            name=op.f("ck_aqa_broker_orders_broker_order_state"),
        ),
        sa.CheckConstraint(
            "cumulative_filled_quantity >= 0",
            name=op.f("ck_aqa_broker_orders_broker_order_filled_nonnegative"),
        ),
        sa.CheckConstraint(
            "((cumulative_filled_quantity = 0 AND average_fill_price IS NULL) OR "
            "(cumulative_filled_quantity > 0 AND average_fill_price > 0))",
            name=op.f("ck_aqa_broker_orders_broker_order_average_consistent"),
        ),
        sa.CheckConstraint(
            "accepted_at IS NULL OR (submitted_at IS NOT NULL AND accepted_at >= submitted_at)",
            name=op.f("ck_aqa_broker_orders_broker_order_acceptance_ordered"),
        ),
        sa.CheckConstraint(
            "submitted_at IS NULL OR submitted_at <= updated_at",
            name=op.f("ck_aqa_broker_orders_broker_order_submission_ordered"),
        ),
        sa.CheckConstraint(
            "accepted_at IS NULL OR accepted_at <= updated_at",
            name=op.f("ck_aqa_broker_orders_broker_order_update_ordered"),
        ),
        sa.CheckConstraint(
            "state <> 'INTENT_COMMITTED' OR "
            "(broker_order_id IS NULL AND submitted_at IS NULL AND accepted_at IS NULL)",
            name=op.f("ck_aqa_broker_orders_broker_order_committed_clean"),
        ),
        _finite_check(
            "aqa_broker_orders",
            "cumulative_filled_quantity",
            nullable=("average_fill_price",),
        ),
        _hash_check("aqa_broker_orders", "content_hash"),
        sa.CheckConstraint(
            "last_event_sequence >= 0",
            name=op.f("ck_aqa_broker_orders_broker_order_event_sequence_nonnegative"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_aqa_broker_orders_broker_order_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["client_order_id", "order_intent_id"],
            [
                "aqa.aqa_order_intents.client_order_id",
                "aqa.aqa_order_intents.order_intent_id",
            ],
            name="broker_order_intent_identity",
        ),
        sa.PrimaryKeyConstraint("client_order_id", name=op.f("pk_aqa_broker_orders")),
        sa.UniqueConstraint(
            "order_intent_id",
            name=op.f("uq_aqa_broker_orders_order_intent_id"),
        ),
        sa.UniqueConstraint(
            "broker_order_id",
            name=op.f("uq_aqa_broker_orders_broker_order_id"),
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_broker_orders_state_updated",
        "aqa_broker_orders",
        ["state", "updated_at"],
        schema="aqa",
    )


def _create_order_events() -> None:
    op.create_table(
        "aqa_order_events",
        sa.Column("order_event_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=False),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("broker_event_id", sa.String(length=128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1",
            name=op.f("ck_aqa_order_events_order_event_sequence_positive"),
        ),
        sa.CheckConstraint(
            f"from_state IN ({_ORDER_STATES})",
            name=op.f("ck_aqa_order_events_order_event_from_state"),
        ),
        sa.CheckConstraint(
            f"to_state IN ({_ORDER_STATES})",
            name=op.f("ck_aqa_order_events_order_event_to_state"),
        ),
        _hash_check("aqa_order_events", "payload_hash"),
        _hash_check("aqa_order_events", "content_hash"),
        sa.ForeignKeyConstraint(
            ["client_order_id"],
            ["aqa.aqa_broker_orders.client_order_id"],
            name=op.f("fk_aqa_order_events_client_order_id_aqa_broker_orders"),
        ),
        sa.PrimaryKeyConstraint("order_event_id", name=op.f("pk_aqa_order_events")),
        sa.UniqueConstraint(
            "client_order_id",
            "sequence",
            name="order_event_sequence",
        ),
        sa.UniqueConstraint(
            "broker_event_id",
            name=op.f("uq_aqa_order_events_broker_event_id"),
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_order_events_client_sequence",
        "aqa_order_events",
        ["client_order_id", "sequence"],
        schema="aqa",
    )


def _create_fills() -> None:
    op.create_table(
        "aqa_fills",
        sa.Column("fill_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=48), nullable=False),
        sa.Column("broker_execution_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("price", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("fee", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_aqa_fills_fill_side")),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0 AND fee >= 0",
            name=op.f("ck_aqa_fills_fill_numbers_valid"),
        ),
        _finite_check("aqa_fills", "quantity", "price", "fee"),
        _hash_check("aqa_fills", "payload_hash"),
        _hash_check("aqa_fills", "content_hash"),
        sa.ForeignKeyConstraint(
            ["client_order_id"],
            ["aqa.aqa_broker_orders.client_order_id"],
            name=op.f("fk_aqa_fills_client_order_id_aqa_broker_orders"),
        ),
        sa.PrimaryKeyConstraint("fill_id", name=op.f("pk_aqa_fills")),
        sa.UniqueConstraint(
            "broker_execution_id",
            name=op.f("uq_aqa_fills_broker_execution_id"),
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_fills_client_occurred",
        "aqa_fills",
        ["client_order_id", "occurred_at"],
        schema="aqa",
    )


def _create_reconciliations() -> None:
    op.create_table(
        "aqa_reconciliations",
        sa.Column("reconciliation_id", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("slot_id", sa.String(length=128), nullable=True),
        sa.Column("execution_plan_id", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("account_id_hash", sa.String(length=64), nullable=False),
        sa.Column("account_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expected_positions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_positions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_cash", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("observed_cash", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("expected_equity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("observed_equity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("mark_prices", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fill_hashes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("order_hashes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("require_flat", sa.Boolean(), nullable=False),
        sa.Column("required_flat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discrepancies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "started_at <= completed_at",
            name=op.f("ck_aqa_reconciliations_reconciliation_times_ordered"),
        ),
        sa.CheckConstraint(
            "started_at <= account_observed_at AND account_observed_at <= completed_at",
            name=op.f("ck_aqa_reconciliations_reconciliation_account_observed_during_run"),
        ),
        sa.CheckConstraint(
            "(require_flat AND required_flat_at IS NOT NULL) OR "
            "(NOT require_flat AND required_flat_at IS NULL)",
            name=op.f("ck_aqa_reconciliations_reconciliation_flat_deadline_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('CLEAN', 'BLOCKING')",
            name=op.f("ck_aqa_reconciliations_reconciliation_status"),
        ),
        _finite_check(
            "aqa_reconciliations",
            "expected_cash",
            "observed_cash",
            "expected_equity",
            "observed_equity",
        ),
        *(
            _hash_check("aqa_reconciliations", name)
            for name in ("account_id_hash", "payload_hash", "content_hash")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_reconciliations_experiment_hash_aqa_experiments"),
        ),
        sa.ForeignKeyConstraint(
            ["slot_id"],
            ["aqa.aqa_decision_slots.slot_id"],
            name=op.f("fk_aqa_reconciliations_slot_id_aqa_decision_slots"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_plan_id"],
            ["aqa.aqa_execution_plans.execution_plan_id"],
            name=op.f("fk_aqa_reconciliations_execution_plan_id_aqa_execution_plans"),
        ),
        sa.PrimaryKeyConstraint(
            "reconciliation_id",
            name=op.f("pk_aqa_reconciliations"),
        ),
        sa.UniqueConstraint(
            "content_hash",
            name=op.f("uq_aqa_reconciliations_content_hash"),
        ),
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_reconciliations_experiment_completed",
        "aqa_reconciliations",
        ["experiment_hash", "completed_at"],
        schema="aqa",
    )
    op.create_index(
        "ix_aqa_reconciliations_status_completed",
        "aqa_reconciliations",
        ["status", "completed_at"],
        schema="aqa",
    )


def _create_incidents() -> None:
    op.create_table(
        "aqa_incidents",
        sa.Column("incident_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("experiment_hash", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("incident_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'error', 'critical')",
            name=op.f("ck_aqa_incidents_incident_severity"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name=op.f("ck_aqa_incidents_incident_status"),
        ),
        sa.CheckConstraint(
            "(status = 'resolved' AND resolved_at IS NOT NULL) OR "
            "(status = 'open' AND resolved_at IS NULL)",
            name=op.f("ck_aqa_incidents_incident_resolution_consistent"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_aqa_incidents_incident_version_positive"),
        ),
        _hash_check("aqa_incidents", "content_hash"),
        sa.ForeignKeyConstraint(
            ["experiment_hash"],
            ["aqa.aqa_experiments.experiment_hash"],
            name=op.f("fk_aqa_incidents_experiment_hash_aqa_experiments"),
        ),
        sa.PrimaryKeyConstraint("incident_id", name=op.f("pk_aqa_incidents")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_aqa_incidents_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "content_hash",
            name=op.f("uq_aqa_incidents_content_hash"),
        ),
        schema="aqa",
    )


def _hash_check(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column}) = 64",
        name=op.f(f"ck_{table}_{column}_sha256_length"),
    )


def _finite_check(
    table: str,
    *columns: str,
    nullable: tuple[str, ...] = (),
) -> sa.CheckConstraint:
    terms = [
        (
            f"({column} IS NULL OR {column} NOT IN ('NaN', 'Infinity', '-Infinity'))"
            if column in nullable
            else f"{column} NOT IN ('NaN', 'Infinity', '-Infinity')"
        )
        for column in (*columns, *nullable)
    ]
    return sa.CheckConstraint(
        " AND ".join(terms),
        name=op.f(f"ck_{table}_financial_values_finite"),
    )


def _create_read_views_and_grants() -> None:
    view_statements = (
        """
        CREATE VIEW aqa.aqa_execution_plans_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT execution_plan_id, risk_decision_id, risk_decision_hash,
               experiment_hash, correlation_id, target_version, forced_flat,
               targets, created_at, deadline_at, content_hash
        FROM aqa.aqa_execution_plans
        """,
        """
        CREATE VIEW aqa.aqa_orders_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT intent.order_intent_id, intent.execution_plan_id,
               intent.risk_decision_id, intent.experiment_hash, intent.correlation_id,
               intent.client_order_id, intent.symbol, intent.side, intent.effect,
               intent.phase, intent.sequence, intent.target_version, intent.quantity,
               intent.notional, intent.reference_price, intent.final_target_quantity,
               intent.forced_flat, intent.created_at, intent.deadline_at,
               broker.broker_order_id, broker.state, broker.submitted_at,
               broker.accepted_at, broker.updated_at,
               broker.cumulative_filled_quantity, broker.average_fill_price,
               broker.last_event_sequence, broker.safe_error_code, broker.version
        FROM aqa.aqa_order_intents AS intent
        LEFT JOIN aqa.aqa_broker_orders AS broker
          ON broker.client_order_id = intent.client_order_id
        """,
        """
        CREATE VIEW aqa.aqa_fills_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT fill_id, client_order_id, broker_execution_id, symbol, side,
               quantity, price, fee, occurred_at, content_hash
        FROM aqa.aqa_fills
        """,
        """
        CREATE VIEW aqa.aqa_reconciliations_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT reconciliation_id, experiment_hash, slot_id, execution_plan_id,
               correlation_id, account_id_hash, account_observed_at,
               started_at, completed_at, status,
               expected_positions, observed_positions, expected_cash, observed_cash,
               expected_equity, observed_equity, mark_prices, fill_hashes, order_hashes,
               require_flat, required_flat_at, discrepancies, content_hash
        FROM aqa.aqa_reconciliations
        """,
        """
        CREATE VIEW aqa.aqa_incidents_v
        WITH (security_barrier = true, security_invoker = false) AS
        SELECT incident_id, idempotency_key, experiment_hash, correlation_id,
               incident_type, severity, status, reason_code, opened_at,
               resolved_at, content_hash, version
        FROM aqa.aqa_incidents
        """,
    )
    view_names = (
        "aqa_execution_plans_v",
        "aqa_orders_v",
        "aqa_fills_v",
        "aqa_reconciliations_v",
        "aqa_incidents_v",
    )
    tables = (
        "aqa_execution_plans",
        "aqa_order_intents",
        "aqa_broker_orders",
        "aqa_order_events",
        "aqa_fills",
        "aqa_reconciliations",
        "aqa_incidents",
    )
    for statement in view_statements:
        op.execute(statement)
    for table in tables:
        op.execute(f"ALTER TABLE aqa.{table} OWNER TO aqa_migrate")
        op.execute(f"REVOKE ALL PRIVILEGES ON aqa.{table} FROM PUBLIC")
    for view in view_names:
        op.execute(f"ALTER VIEW aqa.{view} OWNER TO aqa_migrate")
        op.execute(f"REVOKE ALL PRIVILEGES ON aqa.{view} FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT ON TABLE "
        + ", ".join(f"aqa.{table}" for table in tables)
        + " TO aqa_execution"
    )
    op.execute("GRANT UPDATE ON TABLE aqa.aqa_broker_orders, aqa.aqa_incidents TO aqa_execution")
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE "
        + ", ".join(f"aqa.{table}" for table in tables)
        + " FROM aqa_migrate"
    )
    op.execute(
        "GRANT SELECT ON TABLE aqa.aqa_execution_plans, aqa.aqa_order_intents, "
        "aqa.aqa_broker_orders TO aqa_migrate"
    )
    op.execute(
        "GRANT UPDATE (created_at) ON TABLE aqa.aqa_execution_plans, "
        "aqa.aqa_order_intents TO aqa_migrate"
    )
    op.execute("GRANT UPDATE (submitted_at) ON TABLE aqa.aqa_broker_orders TO aqa_migrate")
    op.execute(
        "GRANT SELECT ON TABLE "
        + ", ".join(f"aqa.{view}" for view in view_names)
        + " TO aqa_control, aqa_readonly"
    )


def downgrade() -> None:
    """Refuse removal of signed execution evidence."""

    raise RuntimeError("Destructive downgrade of signed-execution contracts is not supported")
