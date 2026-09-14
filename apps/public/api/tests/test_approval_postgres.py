"""Real tenant-role approval, consent, concurrency and disconnect fencing tests."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
import test_postgres as pg
from adaptive_trader.public_product.approvals import RiskLimits
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_approval_signing import signing_key
from test_broker import account_data
from test_postgres import LocalIdentity, definition, sign_in

from aqa_public.app import create_app
from aqa_public.approval_signing import ApprovalSigner
from aqa_public.approval_storage import ApprovalStore
from aqa_public.broker import AlpacaConnection, BrokerSettings, PaperAccount
from aqa_public.broker_storage import BrokerStore, ConnectionConflict

pytestmark = pg.pytestmark
settings = pg.settings
store = pg.store


@pytest.fixture
def journey(store, tmp_path):
    token, csrf, owner = sign_in(store)
    accounts = BrokerStore(store)
    account = PaperAccount.model_validate(account_data())
    saved = accounts.connect(
        owner=owner, session=token, generation=0, account=account, encrypted_token="synthetic"
    )
    version = store.save_version(owner, "Approval target", definition())
    signer = ApprovalSigner(signing_key(tmp_path))
    approvals = ApprovalStore(store, signer)
    kwargs = dict(
        owner=owner,
        session=token,
        account_id=saved["id"],
        version_id=version["id"],
        request_id=uuid4(),
        revision=saved["revision"],
        verified=account,
        limits=RiskLimits(
            max_order_notional="1000",
            max_account_exposure="5000",
            max_position_shares=10,
            max_daily_loss="100",
            max_daily_turnover="10000",
        ),
    )
    return approvals, accounts, kwargs, csrf


def confirm(approvals, kwargs, approval_id):
    row = next(row for row in approvals.list(kwargs["owner"]) if row["id"] == approval_id)
    approvals.confirm(
        owner=kwargs["owner"],
        session=kwargs["session"],
        approval_id=approval_id,
        reviewed_hash=row["binding_hash"],
        verified=kwargs["verified"],
        revision=kwargs["revision"],
    )


def test_durable_review_concurrent_consent_and_irreversible_revocation(store, journey):
    approvals, _, kwargs, _ = journey
    approval_id = approvals.draft(**kwargs)
    assert approvals.draft(**kwargs) == approval_id
    with pytest.raises(ConnectionConflict, match="different limits"):
        approvals.draft(
            **{**kwargs, "limits": kwargs["limits"].model_copy(update={"max_position_shares": 20})}
        )
    assert approvals.list(kwargs["owner"])[0]["block_reason"] == "approval_draft"
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: confirm(approvals, kwargs, approval_id), range(2)))
    row = approvals.list(kwargs["owner"])[0]
    assert row["state"] == "approved" and row["block_reason"] is None
    assert row["execution_available"] is False
    with store.tenant(kwargs["owner"]) as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM aqa_public.approval_events")).scalar_one()
            == 1
        )
    approvals.revoke(kwargs["owner"], approval_id)
    approvals.revoke(kwargs["owner"], approval_id)
    # A compromised mutable projection cannot erase the append-only revocation event.
    with store.tenant(kwargs["owner"]) as connection:
        connection.execute(
            text("UPDATE aqa_public.approvals SET state='approved' WHERE id=:id"),
            {"id": approval_id},
        )
    assert approvals.list(kwargs["owner"])[0]["block_reason"] == "approval_revoked"
    with pytest.raises(ConnectionConflict):
        confirm(approvals, kwargs, approval_id)
    for statement in (
        "DELETE FROM aqa_public.approval_events",
        "UPDATE aqa_public.approval_events SET kind='approved'",
        "UPDATE aqa_public.approvals SET binding='{}'::jsonb",
        "DELETE FROM aqa_public.approvals",
    ):
        with pytest.raises(DBAPIError), store.tenant(kwargs["owner"]) as connection:
            connection.execute(text(statement))


def test_cross_owner_references_pool_context_and_guessed_approvals(store, journey):
    approvals, _, kwargs, _ = journey
    approval_id = approvals.draft(**kwargs)
    token, _, bob = sign_in(store)
    assert approvals.list(bob) == []
    with pytest.raises(ConnectionConflict):
        approvals.revoke(bob, approval_id)
    with pytest.raises(ConnectionConflict):
        approvals.draft(**{**kwargs, "owner": bob, "session": token, "request_id": uuid4()})
    for owner, count in ((bob, 0), (kwargs["owner"], 1), (bob, 0)):
        with store.tenant(owner) as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM aqa_public.approvals")).scalar_one()
                == count
            )
    with store.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM aqa_public.approvals")).scalar_one() == 0
        )
    # Even a direct runtime insert cannot combine Bob's ownership and Alice's objects.
    with pytest.raises(DBAPIError), store.tenant(bob) as connection:
        connection.execute(
            text("""INSERT INTO aqa_public.approvals(id,owner_id,account_id,version_id,request_id,request_hash,binding,binding_hash)
            VALUES (:id,:owner,:account,:version,:request,'x','{}','x')"""),
            {
                "id": uuid4(),
                "owner": bob,
                "account": kwargs["account_id"],
                "version": kwargs["version_id"],
                "request": uuid4(),
            },
        )


def test_refresh_preserves_approval_but_reconnect_invalidates_it(store, journey):
    approvals, accounts, kwargs, _ = journey
    approval_id = approvals.draft(**kwargs)
    confirm(approvals, kwargs, approval_id)
    assert accounts.refresh(
        kwargs["owner"], kwargs["account_id"], kwargs["revision"], kwargs["verified"]
    )
    assert approvals.list(kwargs["owner"])[0]["block_reason"] is None
    accounts.disconnect(kwargs["owner"], kwargs["account_id"])
    assert approvals.list(kwargs["owner"])[0]["block_reason"] == "account_disconnected"
    accounts.connect(
        owner=kwargs["owner"],
        session=kwargs["session"],
        generation=1,
        account=kwargs["verified"],
        encrypted_token="reconnected",
    )
    assert approvals.list(kwargs["owner"])[0]["block_reason"] == "connection_changed"


def test_signout_and_stale_connection_cannot_confirm(store, journey):
    approvals, accounts, kwargs, _ = journey
    approval_id = approvals.draft(**kwargs)
    accounts.refresh(kwargs["owner"], kwargs["account_id"], kwargs["revision"], kwargs["verified"])
    with pytest.raises(ConnectionConflict, match="connection changed"):
        confirm(approvals, kwargs, approval_id)
    store.sign_out(kwargs["session"])
    with pytest.raises(ConnectionConflict, match="Session expired"):
        confirm(approvals, kwargs, approval_id)


def test_new_limits_supersede_previous_receipt_and_key_loss_allows_revocation(store, journey):
    approvals, _, kwargs, _ = journey
    first = approvals.draft(**kwargs)
    confirm(approvals, kwargs, first)
    second = approvals.draft(**{**kwargs, "request_id": uuid4()})
    confirm(approvals, kwargs, second)
    rows = {row["id"]: row for row in approvals.list(kwargs["owner"])}
    assert rows[first]["state"] == "revoked"
    assert rows[second]["block_reason"] is None
    unavailable = ApprovalStore(store, None)
    assert unavailable.list(kwargs["owner"])[0]["block_reason"] == "approval_signature_invalid"
    unavailable.revoke(kwargs["owner"], second)
    assert unavailable.list(kwargs["owner"])[0]["state"] == "revoked"


def test_api_requires_reviewed_hash_csrf_and_explicit_consent(store, settings, tmp_path, journey):
    _, accounts, kwargs, csrf = journey
    settings = replace(settings, approval_signing_key=signing_key(tmp_path, "api-key"))

    class LocalBroker(AlpacaConnection):
        def account(self, access):
            return kwargs["verified"]

    broker = LocalBroker(
        BrokerSettings("synthetic", settings.client_secret, settings.encryption_key),
        settings.origin + "/broker/alpaca/callback",
    )
    accounts.connect(
        owner=kwargs["owner"],
        session=kwargs["session"],
        generation=0,
        account=kwargs["verified"],
        encrypted_token=broker.encrypt(
            owner=kwargs["owner"], account_id=kwargs["verified"].id, access="synthetic"
        ),
    )
    headers = {"Origin": settings.origin, "X-CSRF-Token": csrf}
    with TestClient(
        create_app(settings, store=store, identity=LocalIdentity(settings), broker=broker),
        base_url=settings.origin,
    ) as client:
        client.cookies.set("aqa_session", kwargs["session"])
        payload = {
            "account_id": str(kwargs["account_id"]),
            "version_id": str(kwargs["version_id"]),
            "request_id": str(uuid4()),
            "limits": kwargs["limits"].model_dump(mode="json"),
        }
        assert client.post("/api/v1/approvals", json=payload).status_code == 403
        result = client.post("/api/v1/approvals", json=payload, headers=headers)
        assert result.status_code == 201, result.text
        row = result.json()
        path = "/api/v1/approvals/" + row["id"]
        assert (
            client.post(
                path + "/confirm", json={"reviewed_hash": row["binding_hash"]}, headers=headers
            ).status_code
            == 422
        )
        consent = {
            "confirm": "approve_paper_strategy_with_reviewed_limits",
            "reviewed_hash": "0" * 64,
        }
        assert client.post(path + "/confirm", json=consent, headers=headers).status_code == 409
        consent["reviewed_hash"] = row["binding_hash"]
        approved = client.post(path + "/confirm", json=consent, headers=headers)
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"] == "approved"
        assert approved.json()["execution_available"] is False
        assert client.get("/api/v1/approvals").json()["approvals"][0]["block_reason"] is None
        revoked = client.post(
            path + "/revoke", json={"confirm": "revoke_without_cancelling_orders"}, headers=headers
        )
        assert revoked.json()["state"] == "revoked"


def test_expired_review_and_forged_signature_never_become_effective(store, journey):
    from datetime import UTC, datetime, timedelta

    from adaptive_trader.public_product.approvals import ApprovalBinding

    approvals, _, kwargs, _ = journey
    original = approvals.draft(**kwargs)
    with store.tenant(kwargs["owner"]) as connection:
        source = connection.execute(
            text("SELECT binding FROM aqa_public.approvals WHERE id=:id"), {"id": original}
        ).scalar_one()
        expired = ApprovalBinding.model_validate(source).model_copy(update={"approval_id": uuid4()})
        connection.execute(
            text("""INSERT INTO aqa_public.approvals(id,owner_id,account_id,version_id,request_id,request_hash,binding,binding_hash,review_until)
            VALUES (:id,:owner,:account,:version,:request,'expired',CAST(:binding AS jsonb),:hash,:deadline)"""),
            {
                "id": expired.approval_id,
                "owner": kwargs["owner"],
                "account": kwargs["account_id"],
                "version": kwargs["version_id"],
                "request": uuid4(),
                "binding": expired.model_dump_json(),
                "hash": expired.content_hash,
                "deadline": datetime.now(UTC) - timedelta(seconds=1),
            },
        )
    with pytest.raises(ConnectionConflict, match="Review expired"):
        confirm(approvals, kwargs, expired.approval_id)
    assert approvals.list(kwargs["owner"])[0]["block_reason"] == "review_expired"
    # A runtime database credential does not possess the separate signing key.
    with store.tenant(kwargs["owner"]) as connection:
        connection.execute(
            text(
                "UPDATE aqa_public.approvals SET state='approved',signature='fabricated',key_id=:key,approved_at=now() WHERE id=:id"
            ),
            {"key": approvals.signer.key_id, "id": original},
        )
    rows = {row["id"]: row for row in approvals.list(kwargs["owner"])}
    assert rows[original]["block_reason"] == "approval_signature_invalid"
    with pytest.raises(ConnectionConflict, match="signature"):
        confirm(approvals, kwargs, original)


def test_shared_history_quota_is_enforced_without_losing_idempotent_retry(store, journey):
    approvals, _, kwargs, _ = journey
    original = approvals.draft(**kwargs)
    for _ in range(99):
        approvals.draft(**{**kwargs, "request_id": uuid4()})
    with pytest.raises(ConnectionConflict, match="history limit"):
        approvals.draft(**{**kwargs, "request_id": uuid4()})
    assert approvals.draft(**kwargs) == original
    assert len(approvals.list(kwargs["owner"])) == 100
