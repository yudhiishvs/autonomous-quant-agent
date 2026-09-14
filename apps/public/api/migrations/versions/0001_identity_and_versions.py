"""Add verified customer sessions and immutable owned strategy configurations."""

from alembic import op

revision = "public_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE aqa_public.customers (
            id uuid PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
            email text NOT NULL, UNIQUE (issuer, subject)
        );
        CREATE TABLE aqa_public.login_attempts (
            state_hash text PRIMARY KEY, browser_hash text NOT NULL, nonce text NOT NULL,
            encrypted_verifier text NOT NULL, expires_at timestamptz NOT NULL
        );
        CREATE INDEX login_expiry ON aqa_public.login_attempts (expires_at);
        CREATE TABLE aqa_public.sessions (
            token_hash text PRIMARY KEY, owner_id uuid NOT NULL REFERENCES aqa_public.customers,
            csrf_hash text NOT NULL, encrypted_token text NOT NULL, expires_at timestamptz NOT NULL
        );
        CREATE INDEX session_expiry ON aqa_public.sessions (expires_at);
        CREATE INDEX session_owner ON aqa_public.sessions (owner_id);
        CREATE TABLE aqa_public.rate_limits (key text PRIMARY KEY, bucket bigint NOT NULL, count bigint NOT NULL);
        CREATE TABLE aqa_public.strategy_versions (
            id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES aqa_public.customers,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
            definition jsonb NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX version_owner ON aqa_public.strategy_versions (owner_id, created_at DESC, id DESC);
        ALTER TABLE aqa_public.strategy_versions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE aqa_public.strategy_versions FORCE ROW LEVEL SECURITY;
        CREATE POLICY version_tenant ON aqa_public.strategy_versions
            USING (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid)
            WITH CHECK (owner_id = nullif(current_setting('aqa_public.owner', true), '')::uuid);
        REVOKE ALL ON SCHEMA aqa_public FROM PUBLIC;
        GRANT USAGE ON SCHEMA aqa_public TO aqa_public_runtime;
        GRANT SELECT, INSERT, UPDATE ON aqa_public.customers TO aqa_public_runtime;
        GRANT SELECT, INSERT, DELETE ON aqa_public.sessions, aqa_public.login_attempts TO aqa_public_runtime;
        GRANT SELECT, INSERT, UPDATE ON aqa_public.rate_limits TO aqa_public_runtime;
        GRANT SELECT, INSERT ON aqa_public.strategy_versions TO aqa_public_runtime;
        GRANT SELECT ON aqa_public.alembic_version TO aqa_public_runtime;
    """)


def downgrade() -> None:
    op.execute(
        "DROP TABLE aqa_public.strategy_versions, aqa_public.rate_limits, aqa_public.sessions, aqa_public.login_attempts, aqa_public.customers"
    )
