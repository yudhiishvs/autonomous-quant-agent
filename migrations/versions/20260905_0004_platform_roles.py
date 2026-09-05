"""Enforce least-privilege PostgreSQL grants and safe read views.

Revision ID: 20260905_0004
Revises: 20260905_0003
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260905_0004"
down_revision: str | None = "20260905_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTHORIZATION_ROLES = (
    "aqa_migrate",
    "aqa_collector",
    "aqa_scheduler",
    "aqa_strategy",
    "aqa_execution",
    "aqa_control",
    "aqa_readonly",
)
LOGIN_ROLES = tuple(f"{role}_login" for role in AUTHORIZATION_ROLES)
PLATFORM_ROLES = AUTHORIZATION_ROLES + LOGIN_ROLES
_DEFAULT_ACL_GRANTEES = ", ".join((*AUTHORIZATION_ROLES[1:], *LOGIN_ROLES))

SAFE_VIEW_NAMES = (
    "aqa_audit_events_v",
    "aqa_audit_status_v",
    "aqa_basket_watermarks_v",
    "aqa_data_gaps_v",
    "aqa_datasets_v",
    "aqa_decision_slots_v",
    "aqa_effective_bars_v",
    "aqa_execution_plans_v",
    "aqa_experiment_context_v",
    "aqa_fills_v",
    "aqa_incidents_v",
    "aqa_jobs_v",
    "aqa_orders_v",
    "aqa_reconciliations_v",
    "aqa_risk_decisions_v",
    "aqa_risk_latches_v",
    "aqa_security_metadata_v",
    "aqa_signals_v",
    "aqa_symbol_watermarks_v",
)
WRITER_AUDIT_VIEW_BY_ROLE = {
    "aqa_collector": "aqa_collector_audit_events_v",
    "aqa_scheduler": "aqa_scheduler_audit_events_v",
    "aqa_strategy": "aqa_strategy_audit_events_v",
    "aqa_execution": "aqa_execution_audit_events_v",
}

_ROLE_PREREQUISITE_SQL = """
DO $aqa_roles$
BEGIN
    IF (
        SELECT count(*)
        FROM pg_catalog.pg_roles
        WHERE rolname IN (
            'aqa_migrate', 'aqa_collector', 'aqa_scheduler', 'aqa_strategy',
            'aqa_execution', 'aqa_control', 'aqa_readonly'
        )
          AND NOT rolcanlogin
          AND NOT rolsuper
          AND rolinherit
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolreplication
          AND NOT rolbypassrls
          AND rolconfig IS NULL
          AND rolconnlimit = -1
          AND rolvaliduntil IS NULL
    ) <> 7 THEN
        RAISE EXCEPTION 'platform authorization roles are absent or unsafe; run cluster bootstrap';
    END IF;

    IF (
        SELECT count(*)
        FROM pg_catalog.pg_roles
        WHERE rolname IN (
            'aqa_migrate_login', 'aqa_collector_login', 'aqa_scheduler_login',
            'aqa_strategy_login', 'aqa_execution_login', 'aqa_control_login',
            'aqa_readonly_login'
        )
          AND rolcanlogin
          AND NOT rolsuper
          AND rolinherit
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolreplication
          AND NOT rolbypassrls
          AND rolconfig IS NULL
          AND rolconnlimit = -1
          AND rolvaliduntil IS NULL
    ) <> 7 THEN
        RAISE EXCEPTION 'platform login roles are absent or unsafe; run cluster bootstrap';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_db_role_setting AS setting
        JOIN pg_catalog.pg_roles AS role ON role.oid = setting.setrole
        WHERE role.rolname IN (
            'aqa_migrate', 'aqa_collector', 'aqa_scheduler', 'aqa_strategy',
            'aqa_execution', 'aqa_control', 'aqa_readonly',
            'aqa_migrate_login', 'aqa_collector_login', 'aqa_scheduler_login',
            'aqa_strategy_login', 'aqa_execution_login', 'aqa_control_login',
            'aqa_readonly_login'
        )
    ) THEN
        RAISE EXCEPTION 'platform roles may not carry role-specific settings';
    END IF;

    IF EXISTS (
        SELECT granted_role.rolname, member_role.rolname,
               membership.admin_option, membership.inherit_option, membership.set_option
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
        JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
        WHERE granted_role.rolname IN (
                  'aqa_migrate', 'aqa_collector', 'aqa_scheduler', 'aqa_strategy',
                  'aqa_execution', 'aqa_control', 'aqa_readonly',
                  'aqa_migrate_login', 'aqa_collector_login', 'aqa_scheduler_login',
                  'aqa_strategy_login', 'aqa_execution_login', 'aqa_control_login',
                  'aqa_readonly_login'
              )
           OR member_role.rolname IN (
                  'aqa_migrate', 'aqa_collector', 'aqa_scheduler', 'aqa_strategy',
                  'aqa_execution', 'aqa_control', 'aqa_readonly',
                  'aqa_migrate_login', 'aqa_collector_login', 'aqa_scheduler_login',
                  'aqa_strategy_login', 'aqa_execution_login', 'aqa_control_login',
                  'aqa_readonly_login'
              )
        EXCEPT (
            SELECT expected.authorization_role, expected.login_role,
                   false, true, true
            FROM (VALUES
                ('aqa_migrate', 'aqa_migrate_login'),
                ('aqa_collector', 'aqa_collector_login'),
                ('aqa_scheduler', 'aqa_scheduler_login'),
                ('aqa_strategy', 'aqa_strategy_login'),
                ('aqa_execution', 'aqa_execution_login'),
                ('aqa_control', 'aqa_control_login'),
                ('aqa_readonly', 'aqa_readonly_login')
            ) AS expected(authorization_role, login_role)
            UNION ALL
            SELECT 'aqa_migrate', session_user, true, false, true
            WHERE session_user NOT IN (
                  'aqa_migrate', 'aqa_collector', 'aqa_scheduler', 'aqa_strategy',
                  'aqa_execution', 'aqa_control', 'aqa_readonly',
                  'aqa_migrate_login', 'aqa_collector_login', 'aqa_scheduler_login',
                  'aqa_strategy_login', 'aqa_execution_login', 'aqa_control_login',
                  'aqa_readonly_login'
                  )
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_namespace AS namespace
                  WHERE namespace.nspname IN ('aqa', 'market_data')
                    AND pg_catalog.pg_get_userbyid(namespace.nspowner) <> session_user
                  UNION ALL
                  SELECT 1
                  FROM pg_catalog.pg_class AS relation
                  JOIN pg_catalog.pg_namespace AS namespace
                    ON namespace.oid = relation.relnamespace
                  WHERE namespace.nspname IN ('aqa', 'market_data')
                    AND pg_catalog.pg_get_userbyid(relation.relowner) <> session_user
                  UNION ALL
                  SELECT 1
                  FROM pg_catalog.pg_proc AS routine
                  JOIN pg_catalog.pg_namespace AS namespace
                    ON namespace.oid = routine.pronamespace
                  WHERE namespace.nspname IN ('aqa', 'market_data')
                    AND pg_catalog.pg_get_userbyid(routine.proowner) <> session_user
              )
              AND has_database_privilege(session_user, current_database(), 'CREATE')
        )
    ) THEN
        RAISE EXCEPTION 'platform roles have an unexpected membership';
    END IF;

    IF (
        SELECT count(*)
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
        JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
        WHERE (granted_role.rolname, member_role.rolname) IN (
            ('aqa_migrate', 'aqa_migrate_login'),
            ('aqa_collector', 'aqa_collector_login'),
            ('aqa_scheduler', 'aqa_scheduler_login'),
            ('aqa_strategy', 'aqa_strategy_login'),
            ('aqa_execution', 'aqa_execution_login'),
            ('aqa_control', 'aqa_control_login'),
            ('aqa_readonly', 'aqa_readonly_login')
        )
          AND NOT membership.admin_option
          AND membership.inherit_option
          AND membership.set_option
    ) <> 7 THEN
        RAISE EXCEPTION 'platform login memberships are absent or unsafe; run cluster bootstrap';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('aqa_migrate'), ('aqa_collector'), ('aqa_scheduler'), ('aqa_strategy'),
            ('aqa_execution'), ('aqa_control'), ('aqa_readonly'),
            ('aqa_migrate_login'), ('aqa_collector_login'), ('aqa_scheduler_login'),
            ('aqa_strategy_login'), ('aqa_execution_login'), ('aqa_control_login'),
            ('aqa_readonly_login')
        ) AS managed_role(role_name)
        WHERE has_schema_privilege(managed_role.role_name, 'public', 'USAGE')
           OR has_schema_privilege(managed_role.role_name, 'public', 'CREATE')
    ) THEN
        RAISE EXCEPTION 'platform roles retain unsafe public-schema authority; run cluster bootstrap';
    END IF;
END
$aqa_roles$
"""

_VIEW_DDL = (
    """
    CREATE VIEW aqa.aqa_experiment_context_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT
        experiment.experiment_hash,
        experiment.experiment_id,
        experiment.experiment_version,
        experiment.schema_version,
        experiment.configuration,
        experiment.content_hash AS experiment_content_hash,
        experiment.registered_at,
        symbol.experiment_symbol_id,
        symbol.symbol,
        symbol.role,
        symbol.ordinal,
        symbol.content_hash AS symbol_content_hash
    FROM aqa.aqa_experiments AS experiment
    JOIN aqa.aqa_experiment_symbols AS symbol
      ON symbol.experiment_hash = experiment.experiment_hash
    """,
    """
    CREATE VIEW aqa.aqa_security_metadata_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT security_event_id, symbol, source, effective_at, observed_at,
           tradable, fractionable, shortable, content_hash
    FROM aqa.aqa_security_metadata_events
    """,
    """
    CREATE VIEW aqa.aqa_effective_bars_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT
        identity.bar_identity_id,
        identity.provider,
        identity.feed,
        identity.adjustment,
        identity.symbol,
        identity.timeframe,
        identity.start_at,
        identity.end_at,
        event.bar_event_id,
        event.revision,
        event.schema_version,
        event.provider_timestamp,
        event.received_at,
        event.open,
        event.high,
        event.low,
        event.close,
        event.volume,
        event.trade_count,
        event.vwap,
        event.quality_flags,
        event.source,
        event.lineage_hash,
        event.normalized_payload_hash,
        event.content_hash,
        latest.version AS projection_version,
        latest.projected_at
    FROM aqa.aqa_bar_latest AS latest
    JOIN aqa.aqa_bar_identities AS identity
      ON identity.bar_identity_id = latest.bar_identity_id
    JOIN aqa.aqa_bar_events AS event
      ON event.bar_identity_id = latest.bar_identity_id
     AND event.bar_event_id = latest.bar_event_id
     AND event.revision = latest.revision
    """,
    """
    CREATE VIEW aqa.aqa_data_gaps_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT gap_id, experiment_hash, provider, feed, adjustment, symbol, timeframe,
           gap_start_at, gap_end_at, status, reason_code, attempt_count,
           detected_at, last_attempt_at, resolved_at, version
    FROM aqa.aqa_data_gaps
    """,
    """
    CREATE VIEW aqa.aqa_symbol_watermarks_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT symbol_watermark_id, experiment_hash, provider, feed, adjustment, symbol,
           timeframe, contiguous_through, quality_hash, latest_bar_event_id, version, updated_at
    FROM aqa.aqa_symbol_watermarks
    """,
    """
    CREATE VIEW aqa.aqa_basket_watermarks_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT basket_watermark_id, experiment_hash, role, timeframe, contiguous_through,
           component_hash, version, updated_at
    FROM aqa.aqa_basket_watermarks
    """,
    """
    CREATE VIEW aqa.aqa_datasets_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT dataset_id, artifact_id, experiment_hash, provider, feed, adjustment, timeframe,
           range_start_at, range_end_at, roles, symbols, row_counts, gap_summary,
           correction_summary, schema_version, logical_hash, physical_hash, manifest_hash,
           source_git_commit, dirty_worktree, uv_lock_hash, promotable, status, created_at
    FROM aqa.aqa_dataset_manifests
    """,
    """
    CREATE VIEW aqa.aqa_decision_slots_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT slot_id, experiment_hash, source_bar_end, decision_type, scheduled_at,
           data_deadline_at, signal_deadline_at, execution_deadline_at, state, claim_owner,
           lease_expires_at, attempt_count, reason_code, version, created_at, updated_at
    FROM aqa.aqa_decision_slots
    """,
    """
    CREATE VIEW aqa.aqa_signals_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT signal_id, slot_id, experiment_hash, provider_id, provider_version, schema_version,
           source_bar_end, emitted_at, expires_at, targets, reason_codes, content_hash
    FROM aqa.aqa_signal_envelopes
    """,
    """
    CREATE VIEW aqa.aqa_risk_latches_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT latch_event_id, experiment_hash, latch_type, sequence, action, reason_code,
           actor, occurred_at, content_hash
    FROM aqa.aqa_risk_latch_events
    """,
    """
    CREATE VIEW aqa.aqa_risk_decisions_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT risk_decision_id, slot_id, signal_id, experiment_hash, policy_id, policy_version,
           decided_at, input_hash, approved_targets, controls, reason_codes, gross_exposure,
           net_exposure, cash_weight, content_hash
    FROM aqa.aqa_risk_decisions
    """,
    """
    CREATE VIEW aqa.aqa_execution_plans_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT execution_plan_id, risk_decision_id, experiment_hash, target_version,
           forced_flat, targets, created_at, content_hash
    FROM aqa.aqa_execution_plans
    """,
    """
    CREATE VIEW aqa.aqa_orders_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT
        intent.order_intent_id,
        intent.execution_plan_id,
        intent.client_order_id,
        intent.symbol,
        intent.side,
        intent.effect,
        intent.phase,
        intent.sequence,
        intent.quantity,
        intent.notional,
        intent.reference_price,
        intent.order_type,
        intent.time_in_force,
        intent.created_at,
        broker.broker_order_id,
        broker.state,
        broker.submitted_at,
        broker.accepted_at,
        broker.updated_at,
        broker.cumulative_filled_quantity,
        broker.average_fill_price,
        broker.last_event_sequence,
        broker.safe_error_code,
        broker.version
    FROM aqa.aqa_order_intents AS intent
    LEFT JOIN aqa.aqa_broker_orders AS broker
      ON broker.client_order_id = intent.client_order_id
    """,
    """
    CREATE VIEW aqa.aqa_fills_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT fill_id, client_order_id, broker_execution_id, symbol, side, quantity, price,
           fee, occurred_at, content_hash
    FROM aqa.aqa_fills
    """,
    """
    CREATE VIEW aqa.aqa_reconciliations_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT reconciliation_id, experiment_hash, slot_id, execution_plan_id, account_id_hash,
           started_at, completed_at, status, blocking, positions, orders, discrepancies,
           content_hash
    FROM aqa.aqa_reconciliations
    """,
    """
    CREATE VIEW aqa.aqa_incidents_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT incident_id, idempotency_key, experiment_hash, incident_type, severity, status,
           reason_code, opened_at, resolved_at, version
    FROM aqa.aqa_incidents
    """,
    """
    CREATE VIEW aqa.aqa_jobs_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT job_id, job_type, idempotency_key, state, lease_owner, lease_expires_at,
           attempt_count, max_attempts, next_attempt_at, safe_error_code, version,
           created_at, updated_at
    FROM aqa.aqa_jobs
    """,
    """
    CREATE VIEW aqa.aqa_audit_status_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT DISTINCT ON (stream_id)
           stream_id, sequence, event_hash, event_type, actor, occurred_at
    FROM aqa.aqa_audit_events
    ORDER BY stream_id, sequence DESC
    """,
    """
    CREATE VIEW aqa.aqa_audit_events_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT audit_event_id, stream_id, sequence, previous_hash, event_type, actor,
           occurred_at, payload, payload_hash, event_hash, content_hash
    FROM aqa.aqa_audit_events
    """,
    """
    CREATE VIEW aqa.aqa_collector_audit_events_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT audit_event_id, stream_id, sequence, previous_hash, event_type, actor,
           occurred_at, payload, payload_hash, event_hash, content_hash
    FROM aqa.aqa_audit_events
    WHERE actor = 'aqa_collector'
      AND left(stream_id, char_length('aqa_collector:')) = 'aqa_collector:'
    """,
    """
    CREATE VIEW aqa.aqa_scheduler_audit_events_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT audit_event_id, stream_id, sequence, previous_hash, event_type, actor,
           occurred_at, payload, payload_hash, event_hash, content_hash
    FROM aqa.aqa_audit_events
    WHERE actor = 'aqa_scheduler'
      AND left(stream_id, char_length('aqa_scheduler:')) = 'aqa_scheduler:'
    """,
    """
    CREATE VIEW aqa.aqa_strategy_audit_events_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT audit_event_id, stream_id, sequence, previous_hash, event_type, actor,
           occurred_at, payload, payload_hash, event_hash, content_hash
    FROM aqa.aqa_audit_events
    WHERE actor = 'aqa_strategy'
      AND left(stream_id, char_length('aqa_strategy:')) = 'aqa_strategy:'
    """,
    """
    CREATE VIEW aqa.aqa_execution_audit_events_v
    WITH (security_barrier = true, security_invoker = false) AS
    SELECT audit_event_id, stream_id, sequence, previous_hash, event_type, actor,
           occurred_at, payload, payload_hash, event_hash, content_hash
    FROM aqa.aqa_audit_events
    WHERE actor = 'aqa_execution'
      AND left(stream_id, char_length('aqa_execution:')) = 'aqa_execution:'
    """,
)

_TABLE_OWNERSHIP_DDL = (
    "ALTER TABLE aqa.aqa_experiments OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_experiment_symbols OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_security_metadata_events OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_bar_identities OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_bar_events OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_bar_latest OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_data_gaps OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_symbol_watermarks OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_basket_watermarks OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_dataset_manifests OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_decision_slots OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_signal_envelopes OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_risk_latch_events OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_risk_decisions OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_execution_plans OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_order_intents OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_broker_orders OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_order_events OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_fills OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_reconciliations OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_incidents OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_jobs OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_job_attempts OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_outbox_events OWNER TO aqa_migrate",
    "ALTER TABLE aqa.aqa_audit_events OWNER TO aqa_migrate",
)

_VIEW_OWNERSHIP_DDL = (
    "ALTER VIEW aqa.aqa_audit_events_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_audit_status_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_basket_watermarks_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_data_gaps_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_datasets_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_decision_slots_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_effective_bars_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_execution_plans_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_experiment_context_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_fills_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_incidents_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_jobs_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_orders_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_reconciliations_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_risk_decisions_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_risk_latches_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_security_metadata_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_signals_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_symbol_watermarks_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_collector_audit_events_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_scheduler_audit_events_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_strategy_audit_events_v OWNER TO aqa_migrate",
    "ALTER VIEW aqa.aqa_execution_audit_events_v OWNER TO aqa_migrate",
)

_MARKET_DATA_OWNERSHIP_DDL = (
    "ALTER SCHEMA market_data OWNER TO aqa_migrate",
    "ALTER TABLE market_data.alembic_version OWNER TO aqa_migrate",
    "ALTER TABLE market_data.collection_universes OWNER TO aqa_migrate",
    "ALTER TABLE market_data.ingestion_runs OWNER TO aqa_migrate",
    "ALTER TABLE market_data.bar_observations OWNER TO aqa_migrate",
    "ALTER TABLE market_data.current_bars OWNER TO aqa_migrate",
    "ALTER TABLE market_data.collector_leases OWNER TO aqa_migrate",
    "ALTER TABLE market_data.collector_checkpoints OWNER TO aqa_migrate",
    "ALTER TABLE market_data.data_gaps OWNER TO aqa_migrate",
    "ALTER TABLE market_data.collector_events OWNER TO aqa_migrate",
    "ALTER FUNCTION market_data.reject_bar_observation_mutation() OWNER TO aqa_migrate",
    "ALTER FUNCTION market_data.guard_checkpoint_update() OWNER TO aqa_migrate",
    "ALTER FUNCTION market_data.reject_checkpoint_removal() OWNER TO aqa_migrate",
)

_REFERENTIAL_INTEGRITY_OWNER_DDL = (
    "GRANT UPDATE (registered_at) ON aqa.aqa_experiments TO aqa_migrate",
    "GRANT UPDATE (created_at) ON aqa.aqa_bar_identities TO aqa_migrate",
    "GRANT UPDATE (created_at) ON aqa.aqa_bar_events TO aqa_migrate",
    "GRANT UPDATE (created_at) ON aqa.aqa_decision_slots TO aqa_migrate",
    "GRANT UPDATE (emitted_at) ON aqa.aqa_signal_envelopes TO aqa_migrate",
    "GRANT UPDATE (decided_at) ON aqa.aqa_risk_decisions TO aqa_migrate",
    "GRANT UPDATE (created_at) ON aqa.aqa_execution_plans TO aqa_migrate",
    "GRANT UPDATE (created_at) ON aqa.aqa_order_intents TO aqa_migrate",
    "GRANT UPDATE (submitted_at) ON aqa.aqa_broker_orders TO aqa_migrate",
    "GRANT UPDATE (created_at) ON aqa.aqa_jobs TO aqa_migrate",
    "GRANT UPDATE (created_at) ON market_data.collection_universes TO aqa_migrate",
    "GRANT UPDATE (inserted_at) ON market_data.bar_observations TO aqa_migrate",
    "GRANT UPDATE (started_at) ON market_data.ingestion_runs TO aqa_migrate",
)

_MIGRATION_LOGIN_REFERENTIAL_INTEGRITY_OWNER_SQL = f"""
DO $aqa_ri_owner_acl$
BEGIN
    IF session_user = 'aqa_migrate_login' THEN
        {"; ".join(f"{statement} GRANTED BY aqa_migrate_login" for statement in _REFERENTIAL_INTEGRITY_OWNER_DDL)};
    END IF;
END
$aqa_ri_owner_acl$
"""

_COLUMN_ACL_NORMALIZATION_SQL = """
DO $aqa_column_acls$
DECLARE
    target record;
    grantee text;
    rendered_grantee text;
BEGIN
    FOR target IN
        SELECT namespace.nspname AS schema_name,
               relation.relname AS relation_name,
               attribute.attname AS column_name
        FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname IN ('aqa', 'market_data')
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
          AND attribute.attacl IS NOT NULL
    LOOP
        FOREACH grantee IN ARRAY ARRAY[
            'PUBLIC', 'aqa_migrate', 'aqa_collector', 'aqa_scheduler', 'aqa_strategy',
            'aqa_execution', 'aqa_control', 'aqa_readonly',
            'aqa_migrate_login', 'aqa_collector_login', 'aqa_scheduler_login',
            'aqa_strategy_login', 'aqa_execution_login', 'aqa_control_login',
            'aqa_readonly_login'
        ]
        LOOP
            rendered_grantee := CASE
                WHEN grantee = 'PUBLIC' THEN 'PUBLIC'
                ELSE format('%I', grantee)
            END;
            EXECUTE format(
                'REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM %s',
                target.column_name,
                target.schema_name,
                target.relation_name,
                rendered_grantee
            );
        END LOOP;
    END LOOP;
END
$aqa_column_acls$
"""

_TYPE_ACL_NORMALIZATION_SQL = """
DO $aqa_type_acls$
DECLARE
    target record;
BEGIN
    FOR target IN
        SELECT namespace.nspname AS schema_name,
               managed_type.typname AS type_name
        FROM pg_catalog.pg_type AS managed_type
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = managed_type.typnamespace
        WHERE namespace.nspname IN ('aqa', 'market_data')
          AND managed_type.typtype <> 'p'
          AND managed_type.typelem = 0
        ORDER BY namespace.nspname, managed_type.typname
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON TYPE %I.%I FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy, aqa_execution, aqa_control, aqa_readonly, aqa_migrate_login, aqa_collector_login, aqa_scheduler_login, aqa_strategy_login, aqa_execution_login, aqa_control_login, aqa_readonly_login',
            target.schema_name,
            target.type_name
        );
    END LOOP;
END
$aqa_type_acls$
"""

_AUDIT_POLICY_DDL = (
    "ALTER TABLE aqa.aqa_audit_events ENABLE ROW LEVEL SECURITY",
    """
    CREATE POLICY aqa_collector_audit_insert ON aqa.aqa_audit_events
    FOR INSERT TO aqa_collector
    WITH CHECK (
        actor = 'aqa_collector'
        AND left(stream_id, char_length('aqa_collector:')) = 'aqa_collector:'
        AND (
            event_type LIKE 'bar.%'
            OR event_type LIKE 'data.%'
            OR event_type LIKE 'gap.%'
            OR event_type LIKE 'dataset.%'
            OR event_type LIKE 'collector.%'
            OR event_type LIKE 'security.%'
        )
    )
    """,
    """
    CREATE POLICY aqa_scheduler_audit_insert ON aqa.aqa_audit_events
    FOR INSERT TO aqa_scheduler
    WITH CHECK (
        actor = 'aqa_scheduler'
        AND left(stream_id, char_length('aqa_scheduler:')) = 'aqa_scheduler:'
        AND (
            event_type LIKE 'slot.%'
            OR event_type LIKE 'scheduler.%'
            OR event_type LIKE 'security.%'
        )
    )
    """,
    """
    CREATE POLICY aqa_strategy_audit_insert ON aqa.aqa_audit_events
    FOR INSERT TO aqa_strategy
    WITH CHECK (
        actor = 'aqa_strategy'
        AND left(stream_id, char_length('aqa_strategy:')) = 'aqa_strategy:'
        AND (
            event_type LIKE 'signal.%'
            OR event_type LIKE 'strategy.%'
            OR event_type LIKE 'security.%'
        )
    )
    """,
    """
    CREATE POLICY aqa_execution_audit_insert ON aqa.aqa_audit_events
    FOR INSERT TO aqa_execution
    WITH CHECK (
        actor = 'aqa_execution'
        AND left(stream_id, char_length('aqa_execution:')) = 'aqa_execution:'
        AND (
            event_type LIKE 'risk.%'
            OR event_type LIKE 'latch.%'
            OR event_type LIKE 'execution.%'
            OR event_type LIKE 'intent.%'
            OR event_type LIKE 'order.%'
            OR event_type LIKE 'fill.%'
            OR event_type LIKE 'reconciliation.%'
            OR event_type LIKE 'incident.%'
            OR event_type LIKE 'security.%'
        )
    )
    """,
    """
    CREATE POLICY aqa_control_audit_insert ON aqa.aqa_audit_events
    FOR INSERT TO aqa_control
    WITH CHECK (
        actor = 'aqa_control'
        AND left(stream_id, char_length('aqa_control:')) = 'aqa_control:'
        AND (
            event_type LIKE 'experiment.%'
            OR event_type LIKE 'operator.%'
            OR event_type LIKE 'job.%'
            OR event_type LIKE 'control.%'
            OR event_type LIKE 'security.%'
        )
    )
    """,
)

_GRANT_DDL = (
    """
    GRANT SELECT ON TABLE
        aqa.aqa_experiments,
        aqa.aqa_experiment_symbols,
        aqa.aqa_security_metadata_events,
        aqa.aqa_bar_identities,
        aqa.aqa_bar_events,
        aqa.aqa_bar_latest,
        aqa.aqa_data_gaps,
        aqa.aqa_symbol_watermarks,
        aqa.aqa_basket_watermarks,
        aqa.aqa_dataset_manifests,
        aqa.aqa_collector_audit_events_v
    TO aqa_collector
    """,
    """
    GRANT INSERT ON TABLE
        aqa.aqa_bar_identities,
        aqa.aqa_bar_events,
        aqa.aqa_bar_latest,
        aqa.aqa_data_gaps,
        aqa.aqa_symbol_watermarks,
        aqa.aqa_basket_watermarks,
        aqa.aqa_dataset_manifests,
        aqa.aqa_audit_events
    TO aqa_collector
    """,
    """
    GRANT UPDATE ON TABLE
        aqa.aqa_bar_latest,
        aqa.aqa_data_gaps,
        aqa.aqa_symbol_watermarks,
        aqa.aqa_basket_watermarks
    TO aqa_collector
    """,
    """
    GRANT SELECT, INSERT ON TABLE aqa.aqa_decision_slots TO aqa_scheduler
    """,
    "GRANT INSERT ON TABLE aqa.aqa_audit_events TO aqa_scheduler",
    "GRANT UPDATE ON TABLE aqa.aqa_decision_slots TO aqa_scheduler",
    """
    GRANT SELECT ON TABLE
        aqa.aqa_scheduler_audit_events_v,
        aqa.aqa_experiment_context_v,
        aqa.aqa_data_gaps_v,
        aqa.aqa_symbol_watermarks_v,
        aqa.aqa_basket_watermarks_v,
        aqa.aqa_datasets_v
    TO aqa_scheduler
    """,
    """
    GRANT SELECT, INSERT ON TABLE aqa.aqa_signal_envelopes TO aqa_strategy
    """,
    "GRANT INSERT ON TABLE aqa.aqa_audit_events TO aqa_strategy",
    """
    GRANT SELECT ON TABLE
        aqa.aqa_strategy_audit_events_v,
        aqa.aqa_experiment_context_v,
        aqa.aqa_security_metadata_v,
        aqa.aqa_effective_bars_v,
        aqa.aqa_data_gaps_v,
        aqa.aqa_symbol_watermarks_v,
        aqa.aqa_basket_watermarks_v,
        aqa.aqa_datasets_v,
        aqa.aqa_decision_slots_v
    TO aqa_strategy
    """,
    """
    GRANT SELECT ON TABLE
        aqa.aqa_experiments,
        aqa.aqa_experiment_symbols,
        aqa.aqa_security_metadata_events,
        aqa.aqa_bar_identities,
        aqa.aqa_bar_events,
        aqa.aqa_bar_latest,
        aqa.aqa_data_gaps,
        aqa.aqa_symbol_watermarks,
        aqa.aqa_basket_watermarks,
        aqa.aqa_dataset_manifests,
        aqa.aqa_decision_slots,
        aqa.aqa_signal_envelopes,
        aqa.aqa_risk_latch_events,
        aqa.aqa_risk_decisions,
        aqa.aqa_execution_plans,
        aqa.aqa_order_intents,
        aqa.aqa_broker_orders,
        aqa.aqa_order_events,
        aqa.aqa_fills,
        aqa.aqa_reconciliations,
        aqa.aqa_incidents,
        aqa.aqa_execution_audit_events_v
    TO aqa_execution
    """,
    """
    GRANT INSERT ON TABLE
        aqa.aqa_risk_latch_events,
        aqa.aqa_risk_decisions,
        aqa.aqa_execution_plans,
        aqa.aqa_order_intents,
        aqa.aqa_broker_orders,
        aqa.aqa_order_events,
        aqa.aqa_fills,
        aqa.aqa_reconciliations,
        aqa.aqa_incidents,
        aqa.aqa_audit_events
    TO aqa_execution
    """,
    """
    GRANT UPDATE ON TABLE
        aqa.aqa_broker_orders,
        aqa.aqa_incidents
    TO aqa_execution
    """,
    """
    GRANT SELECT, INSERT, UPDATE ON TABLE
        aqa.aqa_jobs,
        aqa.aqa_outbox_events
    TO aqa_control
    """,
    "GRANT INSERT ON TABLE aqa.aqa_audit_events TO aqa_control",
    "GRANT SELECT, INSERT ON TABLE aqa.aqa_job_attempts TO aqa_control",
    "GRANT INSERT ON TABLE aqa.aqa_risk_latch_events TO aqa_control",
    """
    GRANT SELECT ON TABLE
        aqa.aqa_audit_events_v,
        aqa.aqa_audit_status_v,
        aqa.aqa_basket_watermarks_v,
        aqa.aqa_data_gaps_v,
        aqa.aqa_datasets_v,
        aqa.aqa_decision_slots_v,
        aqa.aqa_effective_bars_v,
        aqa.aqa_execution_plans_v,
        aqa.aqa_experiment_context_v,
        aqa.aqa_fills_v,
        aqa.aqa_incidents_v,
        aqa.aqa_jobs_v,
        aqa.aqa_orders_v,
        aqa.aqa_reconciliations_v,
        aqa.aqa_risk_decisions_v,
        aqa.aqa_risk_latches_v,
        aqa.aqa_security_metadata_v,
        aqa.aqa_signals_v,
        aqa.aqa_symbol_watermarks_v
    TO aqa_control
    """,
    """
    GRANT SELECT ON TABLE
        aqa.aqa_audit_events_v,
        aqa.aqa_audit_status_v,
        aqa.aqa_basket_watermarks_v,
        aqa.aqa_data_gaps_v,
        aqa.aqa_datasets_v,
        aqa.aqa_decision_slots_v,
        aqa.aqa_effective_bars_v,
        aqa.aqa_execution_plans_v,
        aqa.aqa_experiment_context_v,
        aqa.aqa_fills_v,
        aqa.aqa_incidents_v,
        aqa.aqa_jobs_v,
        aqa.aqa_orders_v,
        aqa.aqa_reconciliations_v,
        aqa.aqa_risk_decisions_v,
        aqa.aqa_risk_latches_v,
        aqa.aqa_security_metadata_v,
        aqa.aqa_signals_v,
        aqa.aqa_symbol_watermarks_v
    TO aqa_readonly
    """,
)

_DEFAULT_ACL_NORMALIZATION_DDL = (
    *(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate IN SCHEMA {schema} "
        f"REVOKE ALL PRIVILEGES ON {object_kind} FROM {_DEFAULT_ACL_GRANTEES}"
        for schema in ("aqa", "market_data")
        for object_kind in ("TABLES", "SEQUENCES", "ROUTINES", "TYPES")
    ),
    "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate "
    f"REVOKE ALL PRIVILEGES ON SCHEMAS FROM {_DEFAULT_ACL_GRANTEES}",
)


def upgrade() -> None:
    """Apply schema-local authorization after cluster roles have been bootstrapped."""

    op.execute(_ROLE_PREREQUISITE_SQL)
    op.execute("GRANT USAGE, CREATE ON SCHEMA aqa, market_data TO aqa_migrate")
    for statement in _TABLE_OWNERSHIP_DDL:
        op.execute(statement)
    for statement in _MARKET_DATA_OWNERSHIP_DDL[1:]:
        op.execute(statement)
    op.execute("ALTER SCHEMA aqa OWNER TO aqa_migrate")
    op.execute("ALTER SCHEMA market_data OWNER TO aqa_migrate")

    # A legacy owner has only a bounded, non-inherited SET membership. Once ownership is
    # transferred, all ACL and default-ACL work must execute as the durable migration owner.
    op.execute("SET ROLE aqa_migrate")
    op.execute(_COLUMN_ACL_NORMALIZATION_SQL)
    op.execute(
        """
        DO $aqa_transition_database_authority$
        BEGIN
            IF session_user NOT IN (
                'aqa_migrate', 'aqa_collector', 'aqa_scheduler', 'aqa_strategy',
                'aqa_execution', 'aqa_control', 'aqa_readonly',
                'aqa_migrate_login', 'aqa_collector_login', 'aqa_scheduler_login',
                'aqa_strategy_login', 'aqa_execution_login', 'aqa_control_login',
                'aqa_readonly_login'
            ) THEN
                EXECUTE format(
                    'REVOKE CREATE ON DATABASE %I FROM %I',
                    current_database(),
                    session_user
                );
            END IF;
        END
        $aqa_transition_database_authority$
        """
    )
    # Revision 0003 deliberately removes its temporary backfill reads. Restore only the source
    # reads required by the durable security-definer views now owned by the migration role.
    op.execute(
        """
        GRANT SELECT ON TABLE
            aqa.aqa_bar_identities,
            aqa.aqa_bar_events,
            aqa.aqa_bar_latest
        TO aqa_migrate
        """
    )
    for statement in _VIEW_DDL:
        op.execute(statement)
    for statement in _VIEW_OWNERSHIP_DDL:
        op.execute(statement)

    op.execute(
        """
        DO $aqa_database$
        BEGIN
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON DATABASE %I FROM aqa_migrate, aqa_collector, aqa_scheduler, aqa_strategy, aqa_execution, aqa_control, aqa_readonly, aqa_migrate_login, aqa_collector_login, aqa_scheduler_login, aqa_strategy_login, aqa_execution_login, aqa_control_login, aqa_readonly_login',
                current_database()
            );
            EXECUTE format(
                'REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC',
                current_database()
            );
            EXECUTE format(
                'GRANT CONNECT, CREATE ON DATABASE %I TO aqa_migrate',
                current_database()
            );
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO aqa_collector, aqa_scheduler, aqa_strategy, aqa_execution, aqa_control, aqa_readonly',
                current_database()
            );
        END
        $aqa_database$
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON SCHEMA aqa
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA aqa
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA aqa
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA aqa
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(_TYPE_ACL_NORMALIZATION_SQL)
    op.execute(
        """
        GRANT USAGE ON SCHEMA aqa
        TO aqa_collector, aqa_scheduler, aqa_strategy,
           aqa_execution, aqa_control, aqa_readonly
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON SCHEMA market_data
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA market_data
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA market_data
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(
        """
        REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA market_data
        FROM PUBLIC, aqa_collector, aqa_scheduler, aqa_strategy,
             aqa_execution, aqa_control, aqa_readonly,
             aqa_migrate_login, aqa_collector_login, aqa_scheduler_login,
             aqa_strategy_login, aqa_execution_login, aqa_control_login,
             aqa_readonly_login
        """
    )
    op.execute(
        """
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE
        ON ALL TABLES IN SCHEMA aqa, market_data FROM aqa_migrate
        """
    )
    # PostgreSQL referential-integrity triggers execute their row-lock lookup as the relation
    # owner. The owner therefore needs SELECT plus UPDATE on at least one column of every
    # referenced relation even though table-wide application DML remains revoked. The narrow
    # column grants use non-key lifecycle timestamps and are restored after this table-level
    # REVOKE. Keep the relation list equal to the checked-in foreign-key targets.
    op.execute(
        """
        REVOKE SELECT ON TABLE
            aqa.aqa_experiments,
            aqa.aqa_bar_identities,
            aqa.aqa_bar_events,
            aqa.aqa_decision_slots,
            aqa.aqa_signal_envelopes,
            aqa.aqa_risk_decisions,
            aqa.aqa_execution_plans,
            aqa.aqa_order_intents,
            aqa.aqa_broker_orders,
            aqa.aqa_jobs,
            market_data.collection_universes,
            market_data.bar_observations,
            market_data.ingestion_runs
        FROM aqa_migrate
        """
    )
    op.execute(
        """
        GRANT SELECT ON TABLE
            aqa.aqa_experiments,
            aqa.aqa_bar_identities,
            aqa.aqa_bar_events,
            aqa.aqa_decision_slots,
            aqa.aqa_signal_envelopes,
            aqa.aqa_risk_decisions,
            aqa.aqa_execution_plans,
            aqa.aqa_order_intents,
            aqa.aqa_broker_orders,
            aqa.aqa_jobs,
            market_data.collection_universes,
            market_data.bar_observations,
            market_data.ingestion_runs
        TO aqa_migrate
        """
    )
    op.execute("RESET ROLE")
    op.execute(_MIGRATION_LOGIN_REFERENTIAL_INTEGRITY_OWNER_SQL)
    op.execute("SET ROLE aqa_migrate")
    op.execute("GRANT USAGE ON SCHEMA market_data TO aqa_migrate")
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON TABLE market_data.alembic_version TO aqa_migrate
        """
    )
    op.execute("GRANT USAGE ON SCHEMA market_data TO aqa_collector")
    op.execute(
        """
        GRANT SELECT ON TABLE
            market_data.collection_universes,
            market_data.ingestion_runs,
            market_data.bar_observations,
            market_data.current_bars,
            market_data.collector_leases,
            market_data.collector_checkpoints,
            market_data.data_gaps,
            market_data.collector_events
        TO aqa_collector
        """
    )
    op.execute(
        """
        GRANT INSERT ON TABLE
            market_data.collection_universes,
            market_data.ingestion_runs,
            market_data.bar_observations,
            market_data.current_bars,
            market_data.collector_leases,
            market_data.collector_checkpoints,
            market_data.collector_events
        TO aqa_collector
        """
    )
    op.execute(
        """
        GRANT UPDATE ON TABLE
            market_data.ingestion_runs,
            market_data.current_bars,
            market_data.collector_leases,
            market_data.collector_checkpoints
        TO aqa_collector
        """
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate REVOKE EXECUTE ON ROUTINES FROM PUBLIC"
    )
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate REVOKE USAGE ON TYPES FROM PUBLIC")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate REVOKE ALL PRIVILEGES ON SCHEMAS FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate "
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLES FROM aqa_migrate"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate IN SCHEMA aqa "
        "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate IN SCHEMA market_data "
        "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate IN SCHEMA aqa "
        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE aqa_migrate IN SCHEMA market_data "
        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC"
    )
    for statement in _DEFAULT_ACL_NORMALIZATION_DDL:
        op.execute(statement)
    for statement in _AUDIT_POLICY_DDL:
        op.execute(statement)
    for statement in _GRANT_DDL:
        op.execute(statement)


def downgrade() -> None:
    """Refuse removal of the platform authorization boundary and its owned state."""

    raise RuntimeError("Destructive downgrade of platform roles and grants is not supported")
