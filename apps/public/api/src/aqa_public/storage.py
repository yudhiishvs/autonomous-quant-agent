"""PostgreSQL sessions and tenant-owned immutable strategy versions."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from adaptive_trader.public_product.strategies import StrategyDefinition
from sqlalchemy import Connection, create_engine, text


def verifier(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class CustomerSession:
    owner: str
    csrf_hash: str
    encrypted_token: str


class Store:
    def __init__(self, url: str) -> None:
        self.engine = create_engine(
            url,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=5000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=10000",
            },
            pool_size=5,
            max_overflow=5,
            pool_timeout=5,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        if self.engine.dialect.name != "postgresql":
            raise ValueError("Public application requires PostgreSQL.")

    def readiness(self) -> None:
        with self.engine.connect() as connection:
            role = connection.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            if role[0] != "aqa_public_runtime" or role[1] or role[2]:
                raise ValueError("Public API requires its restricted runtime database role.")
            if (
                connection.execute(
                    text("SELECT version_num FROM aqa_public.alembic_version")
                ).scalar_one()
                != "public_0002"
            ):
                raise ValueError("Public schema migration required.")

    @contextmanager
    def tenant(self, owner: str) -> Iterator[Connection]:
        owner = str(UUID(owner))
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('aqa_public.owner', :owner, true)"), {"owner": owner}
            )
            yield connection

    def rate_limit(self, key: str, *, limit: int, window: int) -> bool:
        bucket = int(time.time()) // window
        with self.engine.begin() as connection:
            count = connection.execute(
                text("""
                INSERT INTO aqa_public.rate_limits (key, bucket, count) VALUES (:key, :bucket, 1)
                ON CONFLICT (key) DO UPDATE SET bucket = EXCLUDED.bucket,
                count = CASE WHEN rate_limits.bucket = EXCLUDED.bucket THEN rate_limits.count + 1 ELSE 1 END
                RETURNING count
            """),
                {"key": verifier(key), "bucket": bucket},
            ).scalar_one()
            return bool(count <= limit)

    def begin_login(self, browser: str, state: str, nonce: str, encrypted_verifier: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM aqa_public.login_attempts WHERE expires_at < now()")
            )
            connection.execute(
                text(
                    "INSERT INTO aqa_public.login_attempts VALUES (:state, :browser, :nonce, :verifier, now() + interval '5 minutes')"
                ),
                {
                    "state": verifier(state),
                    "browser": verifier(browser),
                    "nonce": nonce,
                    "verifier": encrypted_verifier,
                },
            )

    def consume_login(self, browser: str, state: str) -> tuple[str, str] | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "DELETE FROM aqa_public.login_attempts WHERE state_hash=:state AND browser_hash=:browser AND expires_at>now() RETURNING nonce, encrypted_verifier"
                ),
                {"state": verifier(state), "browser": verifier(browser)},
            ).first()
            return None if row is None else (row[0], row[1])

    def sign_in(
        self, issuer: str, subject: str, email: str, encrypted_token: str, expires: float
    ) -> tuple[str, str]:
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.engine.begin() as connection:
            owner = connection.execute(
                text("""
                INSERT INTO aqa_public.customers (id, issuer, subject, email) VALUES (:id, :issuer, :subject, :email)
                ON CONFLICT (issuer, subject) DO UPDATE SET email=EXCLUDED.email RETURNING id
            """),
                {"id": str(uuid4()), "issuer": issuer, "subject": subject, "email": email},
            ).scalar_one()
            connection.execute(text("DELETE FROM aqa_public.sessions WHERE expires_at < now()"))
            connection.execute(
                text(
                    "INSERT INTO aqa_public.sessions VALUES (:hash, :owner, :csrf, :token, to_timestamp(:expires))"
                ),
                {
                    "hash": verifier(session),
                    "owner": owner,
                    "csrf": verifier(csrf),
                    "token": encrypted_token,
                    "expires": expires,
                },
            )
        return session, csrf

    def session(self, token: str) -> CustomerSession | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT owner_id, csrf_hash, encrypted_token FROM aqa_public.sessions WHERE token_hash=:hash AND expires_at>now()"
                ),
                {"hash": verifier(token)},
            ).first()
            return None if row is None else CustomerSession(str(row[0]), row[1], row[2])

    def sign_out(self, token: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM aqa_public.sessions WHERE token_hash=:hash"),
                {"hash": verifier(token)},
            )

    def versions(self, owner: str) -> list[dict[str, Any]]:
        with self.tenant(owner) as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id, name, definition, content_hash, created_at FROM aqa_public.strategy_versions WHERE owner_id=:owner ORDER BY created_at DESC, id DESC LIMIT 100"
                    ),
                    {"owner": owner},
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    def version(self, owner: str, version_id: UUID) -> dict[str, Any] | None:
        with self.tenant(owner) as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, name, definition, content_hash, created_at FROM aqa_public.strategy_versions WHERE owner_id=:owner AND id=:id"
                    ),
                    {"owner": owner, "id": str(version_id)},
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def save_version(
        self,
        owner: str,
        name: str,
        definition: StrategyDefinition,
        *,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        version_id = str(uuid4())
        request_id = request_id or uuid4()
        with self.tenant(owner) as connection:
            # Serialize one customer's quota and insertion across API instances.
            connection.execute(
                text("SELECT id FROM aqa_public.customers WHERE id=:owner FOR UPDATE"),
                {"owner": owner},
            ).scalar_one()
            existing = (
                connection.execute(
                    text(
                        "SELECT id, name, definition, content_hash, created_at FROM aqa_public.strategy_versions WHERE owner_id=:owner AND request_id=:request"
                    ),
                    {"owner": owner, "request": str(request_id)},
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["name"] != name or existing["content_hash"] != definition.content_hash:
                    raise ValueError("Save request was already used for a different configuration.")
                return dict(existing)
            count = connection.execute(
                text("SELECT count(*) FROM aqa_public.strategy_versions WHERE owner_id=:owner"),
                {"owner": owner},
            ).scalar_one()
            if count >= 100:
                raise ValueError("Strategy version limit reached (100).")
            row = (
                connection.execute(
                    text("""
                INSERT INTO aqa_public.strategy_versions (id, owner_id, name, definition, content_hash, request_id)
                VALUES (:id, :owner, :name, CAST(:definition AS jsonb), :hash, :request)
                RETURNING id, name, definition, content_hash, created_at
            """),
                    {
                        "id": version_id,
                        "owner": owner,
                        "name": name,
                        "definition": json.dumps(definition.model_dump(mode="json")),
                        "hash": definition.content_hash,
                        "request": str(request_id),
                    },
                )
                .mappings()
                .one()
            )
            return dict(row)
