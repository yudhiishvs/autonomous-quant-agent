"""Tenant transactions for OAuth, immutable account claims and disconnect fencing."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from aqa_public.broker import PaperAccount
from aqa_public.storage import Store, verifier


class ConnectionConflict(Exception):
    """Safe connection conflict with no cross-customer account details."""


class BrokerStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _authority(connection: Connection, owner: str) -> int:
        connection.execute(
            text(
                "INSERT INTO aqa_public.broker_authority(owner_id) VALUES (:owner) ON CONFLICT DO NOTHING"
            ),
            {"owner": owner},
        )
        return int(
            connection.execute(
                text(
                    "SELECT generation FROM aqa_public.broker_authority WHERE owner_id=:owner FOR UPDATE"
                ),
                {"owner": owner},
            ).scalar_one()
        )

    def begin(self, owner: str, session: str, state: str) -> None:
        with self.store.tenant(owner) as connection:
            generation = self._authority(connection, owner)
            connection.execute(
                text(
                    "DELETE FROM aqa_public.broker_oauth_attempts WHERE owner_id=:owner AND expires_at<now()"
                ),
                {"owner": owner},
            )
            count = connection.execute(
                text("SELECT count(*) FROM aqa_public.broker_oauth_attempts WHERE owner_id=:owner"),
                {"owner": owner},
            ).scalar_one()
            if count >= 5:
                raise ConnectionConflict(
                    "Five connections are already awaiting consent. Wait five minutes before trying again."
                )
            connection.execute(
                text(
                    "INSERT INTO aqa_public.broker_oauth_attempts VALUES (:state,:owner,:session,:generation,now()+interval '5 minutes')"
                ),
                {
                    "state": verifier(state),
                    "owner": owner,
                    "session": verifier(session),
                    "generation": generation,
                },
            )

    def consume(self, owner: str, session: str, state: str) -> int:
        with self.store.tenant(owner) as connection:
            row = connection.execute(
                text(
                    "DELETE FROM aqa_public.broker_oauth_attempts WHERE owner_id=:owner AND state_hash=:state AND session_hash=:session AND expires_at>now() RETURNING generation"
                ),
                {"owner": owner, "state": verifier(state), "session": verifier(session)},
            ).first()
            if row is None:
                raise ConnectionConflict("Connection expired or already used. Start again.")
            return int(row[0])

    def connect(
        self,
        *,
        owner: str,
        session: str,
        generation: int,
        account: PaperAccount,
        encrypted_token: str,
    ) -> dict[str, Any]:
        try:
            with self.store.tenant(owner) as connection:
                if self._authority(connection, owner) != generation:
                    raise ConnectionConflict(
                        "A disconnect superseded this connection. Start again."
                    )
                # Serialize credential publication with sign-out; never publish from a stale session.
                if (
                    connection.execute(
                        text(
                            "SELECT token_hash FROM aqa_public.sessions WHERE token_hash=:session AND owner_id=:owner AND expires_at>now() FOR SHARE"
                        ),
                        {"session": verifier(session), "owner": owner},
                    ).first()
                    is None
                ):
                    raise ConnectionConflict(
                        "The initiating session expired. Sign in and connect again."
                    )
                existing = connection.execute(
                    text(
                        "SELECT id FROM aqa_public.broker_accounts WHERE owner_id=:owner AND broker_id=:broker"
                    ),
                    {"owner": owner, "broker": str(account.id)},
                ).first()
                count = connection.execute(
                    text("SELECT count(*) FROM aqa_public.broker_accounts WHERE owner_id=:owner"),
                    {"owner": owner},
                ).scalar_one()
                if existing is None and count >= 3:
                    raise ConnectionConflict(
                        "The development limit is three paper accounts per workspace."
                    )
                statement = (
                    "INSERT INTO aqa_public.broker_accounts(id,owner_id,broker_id,state,encrypted_token,snapshot) "
                    "VALUES (:id,:owner,:broker,'connected',:token,CAST(:snapshot AS jsonb)) "
                    "RETURNING id, state, snapshot, revision, updated_at"
                    if existing is None
                    else "UPDATE aqa_public.broker_accounts SET state='connected', encrypted_token=:token, "
                    "snapshot=CAST(:snapshot AS jsonb), revision=revision+1, connection_generation=connection_generation+1, updated_at=now() "
                    "WHERE owner_id=:owner AND broker_id=:broker "
                    "RETURNING id, state, snapshot, revision, updated_at"
                )
                row = (
                    connection.execute(
                        text(statement),
                        {
                            "id": str(uuid4()),
                            "owner": owner,
                            "broker": str(account.id),
                            "token": encrypted_token,
                            "snapshot": json.dumps(account.snapshot()),
                        },
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise ConnectionConflict(
                        "This paper account cannot be connected to this workspace."
                    )
                return dict(row)
        except IntegrityError:
            raise ConnectionConflict(
                "This paper account cannot be connected to this workspace."
            ) from None

    def accounts(self, owner: str) -> list[dict[str, Any]]:
        with self.store.tenant(owner) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT id, state, snapshot, revision, updated_at FROM aqa_public.broker_accounts WHERE owner_id=:owner ORDER BY updated_at DESC, id LIMIT 3"
                    ),
                    {"owner": owner},
                ).mappings()
            ]

    def credential(self, owner: str, account_id: UUID) -> dict[str, Any] | None:
        with self.store.tenant(owner) as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT broker_id, encrypted_token, revision, connection_generation FROM aqa_public.broker_accounts WHERE owner_id=:owner AND id=:id AND state='connected'"
                    ),
                    {"owner": owner, "id": str(account_id)},
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def refresh(self, owner: str, account_id: UUID, revision: int, account: PaperAccount) -> bool:
        with self.store.tenant(owner) as connection:
            return (
                connection.execute(
                    text(
                        "UPDATE aqa_public.broker_accounts SET snapshot=CAST(:snapshot AS jsonb), updated_at=now(), revision=revision+1 WHERE owner_id=:owner AND id=:id AND broker_id=:broker AND revision=:revision AND state='connected' RETURNING id"
                    ),
                    {
                        "owner": owner,
                        "id": str(account_id),
                        "broker": str(account.id),
                        "revision": revision,
                        "snapshot": json.dumps(account.snapshot()),
                    },
                ).first()
                is not None
            )

    def disconnect(
        self,
        owner: str,
        account_id: UUID,
        *,
        state: Literal["disconnected", "reconnect_required"] = "disconnected",
        expected_revision: int | None = None,
    ) -> bool:
        with self.store.tenant(owner) as connection:
            self._authority(connection, owner)
            row = connection.execute(
                text(
                    "UPDATE aqa_public.broker_accounts SET state=:state, encrypted_token=NULL, revision=revision+1, connection_generation=connection_generation+1, updated_at=now() WHERE owner_id=:owner AND id=:id AND (CAST(:expected AS bigint) IS NULL OR revision=:expected) RETURNING id"
                ),
                {
                    "owner": owner,
                    "id": str(account_id),
                    "state": state,
                    "expected": expected_revision,
                },
            ).first()
            if row is None:
                return False
            connection.execute(
                text(
                    "UPDATE aqa_public.broker_authority SET generation=generation+1 WHERE owner_id=:owner"
                ),
                {"owner": owner},
            )
            connection.execute(
                text("DELETE FROM aqa_public.broker_oauth_attempts WHERE owner_id=:owner"),
                {"owner": owner},
            )
            return True
