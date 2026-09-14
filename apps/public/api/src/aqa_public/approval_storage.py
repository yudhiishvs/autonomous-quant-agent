"""Bounded explicit approval transactions. These receipts alone cannot submit orders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from adaptive_trader.platform.hashing import sha256_hex
from adaptive_trader.public_product.approvals import (
    ApprovalBinding,
    RiskLimits,
    approval_block_reason,
    validate_approval_limits,
)
from adaptive_trader.public_product.strategies import StrategyDefinition
from sqlalchemy import Connection, text

from aqa_public.approval_signing import ApprovalSigner
from aqa_public.broker import PaperAccount
from aqa_public.broker_storage import BrokerStore, ConnectionConflict
from aqa_public.storage import Store, verifier


def broker_hash(broker_id: UUID) -> str:
    return str(sha256_hex(("alpaca-paper-account-v1", str(broker_id))))


class ApprovalStore:
    def __init__(self, store: Store, signer: ApprovalSigner | None) -> None:
        self.store, self.signer = store, signer

    @staticmethod
    def _session(connection: Connection, owner: str, session: str) -> None:
        if (
            connection.execute(
                text(
                    "SELECT token_hash FROM aqa_public.sessions WHERE owner_id=:owner AND token_hash=:session AND expires_at>now() FOR SHARE"
                ),
                {"owner": owner, "session": verifier(session)},
            ).first()
            is None
        ):
            raise ConnectionConflict("Session expired. Sign in and review again.")

    @staticmethod
    def _account(connection: Connection, owner: str, account_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    "SELECT id,broker_id,state,revision,connection_generation FROM aqa_public.broker_accounts WHERE owner_id=:owner AND id=:id FOR UPDATE"
                ),
                {"owner": owner, "id": str(account_id)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ConnectionConflict("Paper account unavailable in this workspace.")
        return dict(row)

    @staticmethod
    def _version(connection: Connection, owner: str, version_id: UUID) -> StrategyDefinition:
        row = (
            connection.execute(
                text(
                    "SELECT definition,content_hash FROM aqa_public.strategy_versions WHERE owner_id=:owner AND id=:id"
                ),
                {"owner": owner, "id": str(version_id)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ConnectionConflict("Strategy version unavailable in this workspace.")
        definition = StrategyDefinition.model_validate(row["definition"])
        if definition.content_hash != row["content_hash"]:
            raise ConnectionConflict("Strategy version integrity could not be verified.")
        return definition

    @staticmethod
    def _fresh(account: dict[str, Any], verified: PaperAccount, revision: int) -> None:
        if (
            account["state"] != "connected"
            or account["broker_id"] != verified.id
            or account["revision"] != revision
        ):
            raise ConnectionConflict("Paper connection changed. Refresh and review again.")

    def draft(
        self,
        *,
        owner: str,
        session: str,
        account_id: UUID,
        version_id: UUID,
        limits: RiskLimits,
        request_id: UUID,
        verified: PaperAccount,
        revision: int,
    ) -> UUID:
        limits = RiskLimits.model_validate(limits)
        request_hash = sha256_hex((str(account_id), str(version_id), limits.content_hash))
        with self.store.tenant(owner) as connection:
            BrokerStore._authority(connection, owner)
            self._session(connection, owner, session)
            existing = connection.execute(
                text(
                    "SELECT id,request_hash FROM aqa_public.approvals WHERE owner_id=:owner AND request_id=:request"
                ),
                {"owner": owner, "request": str(request_id)},
            ).first()
            if existing:
                if existing[1] != request_hash:
                    raise ConnectionConflict(
                        "Review request was already used for different limits."
                    )
                return UUID(str(existing[0]))
            count = connection.execute(
                text("SELECT count(*) FROM aqa_public.approvals WHERE owner_id=:owner"),
                {"owner": owner},
            ).scalar_one()
            if count >= 100:
                raise ConnectionConflict("Approval history limit reached (100).")
            account = self._account(connection, owner, account_id)
            self._fresh(account, verified, revision)
            definition = self._version(connection, owner, version_id)
            validate_approval_limits(definition, limits, verified.equity)
            now = connection.execute(text("SELECT clock_timestamp()")).scalar_one().astimezone(UTC)
            binding = ApprovalBinding(
                approval_id=uuid4(),
                owner_id=UUID(owner),
                account_id=account_id,
                version_id=version_id,
                strategy_hash=definition.content_hash,
                broker_account_hash=broker_hash(verified.id),
                connection_generation=account["connection_generation"],
                limits=limits,
                expires_at=now + timedelta(days=limits.validity_days),
            )
            connection.execute(
                text("""
                INSERT INTO aqa_public.approvals(id,owner_id,account_id,version_id,request_id,request_hash,binding,binding_hash)
                VALUES (:id,:owner,:account,:version,:request,:request_hash,CAST(:binding AS jsonb),:hash)
            """),
                {
                    "id": str(binding.approval_id),
                    "owner": owner,
                    "account": str(account_id),
                    "version": str(version_id),
                    "request": str(request_id),
                    "request_hash": request_hash,
                    "binding": binding.model_dump_json(),
                    "hash": binding.content_hash,
                },
            )
            return UUID(str(binding.approval_id))

    @staticmethod
    def _row(connection: Connection, owner: str, approval_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    "SELECT * FROM aqa_public.approvals WHERE owner_id=:owner AND id=:id FOR UPDATE"
                ),
                {"owner": owner, "id": str(approval_id)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ConnectionConflict("Approval not found in this workspace.")
        return dict(row)

    @staticmethod
    def _revoked(connection: Connection, owner: str, approval_id: UUID) -> bool:
        return (
            connection.execute(
                text(
                    "SELECT 1 FROM aqa_public.approval_events WHERE owner_id=:owner AND approval_id=:id AND kind='revoked'"
                ),
                {"owner": owner, "id": str(approval_id)},
            ).first()
            is not None
        )

    def confirm(
        self,
        *,
        owner: str,
        session: str,
        approval_id: UUID,
        reviewed_hash: str,
        verified: PaperAccount,
        revision: int,
    ) -> None:
        if self.signer is None:
            raise ConnectionConflict("Approval signing is unavailable.")
        with self.store.tenant(owner) as connection:
            BrokerStore._authority(connection, owner)
            self._session(connection, owner, session)
            row = self._row(connection, owner, approval_id)
            binding = ApprovalBinding.model_validate(row["binding"])
            account = self._account(connection, owner, row["account_id"])
            self._fresh(account, verified, revision)
            definition = self._version(connection, owner, row["version_id"])
            validate_approval_limits(definition, binding.limits, verified.equity)
            now = connection.execute(text("SELECT clock_timestamp()")).scalar_one().astimezone(UTC)
            reason = approval_block_reason(
                binding,
                stored_hash=row["binding_hash"],
                state="revoked" if self._revoked(connection, owner, approval_id) else "approved",
                owner_id=UUID(owner),
                account_id=row["account_id"],
                version_id=row["version_id"],
                strategy_hash=definition.content_hash,
                broker_account_hash=broker_hash(verified.id),
                connection_generation=account["connection_generation"],
                account_connected=True,
                now=now,
            )
            if (
                reason
                or binding.approval_id != approval_id
                or reviewed_hash != binding.content_hash
            ):
                raise ConnectionConflict("Approval changed or expired. Create a new review.")
            if row["state"] == "approved":
                if not self.signer.verifies(
                    binding, signature=row["signature"], key_id=row["key_id"]
                ):
                    raise ConnectionConflict(
                        "Approval signature cannot be verified. Revoke and review again."
                    )
                return
            if row["state"] != "draft" or now >= row["review_until"]:
                raise ConnectionConflict("Review expired or revoked. Create a new review.")
            # One active receipt per account/version; prior grants become irrevocably superseded.
            previous = (
                connection.execute(
                    text(
                        "SELECT id FROM aqa_public.approvals WHERE owner_id=:owner AND account_id=:account AND version_id=:version AND state='approved'"
                    ),
                    {
                        "owner": owner,
                        "account": str(row["account_id"]),
                        "version": str(row["version_id"]),
                    },
                )
                .scalars()
                .all()
            )
            for previous_id in previous:
                self._revoke(connection, owner, previous_id)
            connection.execute(
                text(
                    "UPDATE aqa_public.approvals SET state='approved',signature=:signature,key_id=:key,approved_at=clock_timestamp() WHERE owner_id=:owner AND id=:id"
                ),
                {
                    "owner": owner,
                    "id": str(approval_id),
                    "signature": self.signer.sign(binding),
                    "key": self.signer.key_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO aqa_public.approval_events(approval_id,owner_id,kind,binding_hash) VALUES (:id,:owner,'approved',:hash)"
                ),
                {"owner": owner, "id": str(approval_id), "hash": binding.content_hash},
            )

    @staticmethod
    def _revoke(connection: Connection, owner: str, approval_id: UUID) -> None:
        connection.execute(
            text(
                "INSERT INTO aqa_public.approval_events(approval_id,owner_id,kind,binding_hash) SELECT id,owner_id,'revoked',binding_hash FROM aqa_public.approvals WHERE owner_id=:owner AND id=:id ON CONFLICT DO NOTHING"
            ),
            {"owner": owner, "id": str(approval_id)},
        )
        connection.execute(
            text(
                "UPDATE aqa_public.approvals SET state='revoked',revoked_at=coalesce(revoked_at,clock_timestamp()) WHERE owner_id=:owner AND id=:id"
            ),
            {"owner": owner, "id": str(approval_id)},
        )

    def revoke(self, owner: str, approval_id: UUID) -> None:
        with self.store.tenant(owner) as connection:
            BrokerStore._authority(connection, owner)
            self._row(connection, owner, approval_id)
            self._revoke(connection, owner, approval_id)

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self.store.tenant(owner) as connection:
            rows = (
                connection.execute(
                    text("""
                SELECT p.*,a.state AS account_state,a.connection_generation,a.broker_id,v.definition,v.content_hash AS strategy_hash,
                EXISTS(SELECT 1 FROM aqa_public.approval_events e WHERE e.owner_id=p.owner_id AND e.approval_id=p.id AND e.kind='revoked') AS was_revoked
                FROM aqa_public.approvals p
                JOIN aqa_public.broker_accounts a ON a.owner_id=p.owner_id AND a.id=p.account_id
                JOIN aqa_public.strategy_versions v ON v.owner_id=p.owner_id AND v.id=p.version_id
                WHERE p.owner_id=:owner ORDER BY p.created_at DESC,p.id LIMIT 100
            """),
                    {"owner": owner},
                )
                .mappings()
                .all()
            )
            now = datetime.now(UTC)
            return [self._response(dict(row), now) for row in rows]

    def _response(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        binding = ApprovalBinding.model_validate(row["binding"])
        definition = StrategyDefinition.model_validate(row["definition"])
        reason = approval_block_reason(
            binding,
            stored_hash=row["binding_hash"],
            state="revoked" if row["was_revoked"] else row["state"],
            owner_id=row["owner_id"],
            account_id=row["account_id"],
            version_id=row["version_id"],
            strategy_hash=definition.content_hash,
            broker_account_hash=broker_hash(row["broker_id"]),
            connection_generation=row["connection_generation"],
            account_connected=row["account_state"] == "connected",
            now=now,
        )
        if binding.approval_id != row["id"] or definition.content_hash != row["strategy_hash"]:
            reason = "approval_integrity_failed"
        if reason is None and (
            self.signer is None
            or not self.signer.verifies(binding, signature=row["signature"], key_id=row["key_id"])
        ):
            reason = "approval_signature_invalid"
        if reason == "approval_draft" and now >= row["review_until"]:
            reason = "review_expired"
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "version_id": row["version_id"],
            "binding_hash": row["binding_hash"],
            "limits": binding.limits.model_dump(mode="json"),
            "expires_at": binding.expires_at,
            "review_until": row["review_until"],
            "state": row["state"],
            "block_reason": reason,
            "execution_available": False,
        }
