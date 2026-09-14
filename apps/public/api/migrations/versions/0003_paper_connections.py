"""Add owner-bound paper OAuth attempts, connection generations and encrypted grants."""

from alembic import op

revision = "public_0003"
down_revision = "public_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE aqa_public.broker_authority (
            owner_id uuid PRIMARY KEY REFERENCES aqa_public.customers,
            generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0)
        );
        CREATE TABLE aqa_public.broker_oauth_attempts (
            state_hash text PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES aqa_public.customers,
            session_hash text NOT NULL,
            generation bigint NOT NULL,
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX broker_oauth_owner ON aqa_public.broker_oauth_attempts(owner_id, expires_at);
        CREATE TABLE aqa_public.broker_accounts (
            id uuid PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES aqa_public.customers,
            broker_id uuid NOT NULL UNIQUE,
            state text NOT NULL CHECK (state IN ('connected', 'disconnected', 'reconnect_required')),
            encrypted_token text,
            snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
            revision bigint NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((state = 'connected') = (encrypted_token IS NOT NULL))
        );
        CREATE INDEX broker_accounts_owner ON aqa_public.broker_accounts(owner_id, updated_at DESC);
        ALTER TABLE aqa_public.broker_authority ENABLE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.broker_authority FORCE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.broker_oauth_attempts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.broker_oauth_attempts FORCE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.broker_accounts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.broker_accounts FORCE ROW LEVEL SECURITY;
        CREATE POLICY broker_authority_tenant ON aqa_public.broker_authority
            USING (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid)
            WITH CHECK (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid);
        CREATE POLICY broker_attempt_tenant ON aqa_public.broker_oauth_attempts
            USING (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid)
            WITH CHECK (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid);
        CREATE POLICY broker_account_tenant ON aqa_public.broker_accounts
            USING (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid)
            WITH CHECK (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid);
        GRANT SELECT, INSERT, UPDATE ON aqa_public.broker_authority, aqa_public.broker_accounts TO aqa_public_runtime;
        GRANT SELECT, INSERT, DELETE ON aqa_public.broker_oauth_attempts TO aqa_public_runtime;
        GRANT UPDATE(token_hash) ON aqa_public.sessions TO aqa_public_runtime;
    """)


def downgrade() -> None:
    op.execute("REVOKE UPDATE(token_hash) ON aqa_public.sessions FROM aqa_public_runtime")
    op.execute(
        "DROP TABLE aqa_public.broker_accounts, aqa_public.broker_oauth_attempts, aqa_public.broker_authority"
    )
