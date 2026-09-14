"""Immutable non-AI approval bindings and append-only consent/revocation evidence."""

from alembic import op

revision = "public_0004"
down_revision = "public_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE aqa_public.broker_accounts ADD COLUMN connection_generation bigint NOT NULL DEFAULT 0 CHECK (connection_generation >= 0);
        ALTER TABLE aqa_public.broker_accounts ADD CONSTRAINT broker_account_owner_id UNIQUE(owner_id,id);
        ALTER TABLE aqa_public.strategy_versions ADD CONSTRAINT strategy_version_owner_id UNIQUE(owner_id,id);
        CREATE TABLE aqa_public.approvals (
            id uuid PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES aqa_public.customers,
            account_id uuid NOT NULL,
            version_id uuid NOT NULL,
            request_id uuid NOT NULL,
            request_hash text NOT NULL,
            binding jsonb NOT NULL CHECK (jsonb_typeof(binding)='object'),
            binding_hash text NOT NULL,
            state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','approved','revoked')),
            signature text,
            key_id text,
            created_at timestamptz NOT NULL DEFAULT now(),
            review_until timestamptz NOT NULL DEFAULT now()+interval '5 minutes',
            approved_at timestamptz,
            revoked_at timestamptz,
            UNIQUE(owner_id,id),
            UNIQUE(owner_id,request_id),
            FOREIGN KEY(owner_id,account_id) REFERENCES aqa_public.broker_accounts(owner_id,id),
            FOREIGN KEY(owner_id,version_id) REFERENCES aqa_public.strategy_versions(owner_id,id),
            CHECK (state<>'approved' OR (signature IS NOT NULL AND key_id IS NOT NULL AND approved_at IS NOT NULL))
        );
        CREATE INDEX approvals_owner_recent ON aqa_public.approvals(owner_id,created_at DESC,id);
        CREATE UNIQUE INDEX one_current_approval ON aqa_public.approvals(owner_id,account_id,version_id) WHERE state='approved';
        CREATE TABLE aqa_public.approval_events (
            approval_id uuid NOT NULL,
            owner_id uuid NOT NULL,
            kind text NOT NULL CHECK (kind IN ('approved','revoked')),
            binding_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(approval_id,kind),
            FOREIGN KEY(owner_id,approval_id) REFERENCES aqa_public.approvals(owner_id,id)
        );
        CREATE INDEX approval_events_owner ON aqa_public.approval_events(owner_id,approval_id);
        ALTER TABLE aqa_public.approvals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.approvals FORCE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.approval_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.approval_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY approval_tenant ON aqa_public.approvals
            USING (owner_id=nullif(current_setting('aqa_public.owner',true),'')::uuid)
            WITH CHECK (owner_id=nullif(current_setting('aqa_public.owner',true),'')::uuid);
        CREATE POLICY approval_event_tenant ON aqa_public.approval_events
            USING (owner_id=nullif(current_setting('aqa_public.owner',true),'')::uuid)
            WITH CHECK (owner_id=nullif(current_setting('aqa_public.owner',true),'')::uuid);
        GRANT SELECT, INSERT ON aqa_public.approvals, aqa_public.approval_events TO aqa_public_runtime;
        GRANT UPDATE(state,signature,key_id,approved_at,revoked_at) ON aqa_public.approvals TO aqa_public_runtime;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE aqa_public.approval_events, aqa_public.approvals;
        ALTER TABLE aqa_public.strategy_versions DROP CONSTRAINT strategy_version_owner_id;
        ALTER TABLE aqa_public.broker_accounts DROP CONSTRAINT broker_account_owner_id;
        ALTER TABLE aqa_public.broker_accounts DROP COLUMN connection_generation;
    """)
