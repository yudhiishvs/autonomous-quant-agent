"""A receipt must be verified with the configured key and exact immutable binding."""

import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from adaptive_trader.public_product.approvals import ApprovalBinding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aqa_public.approval_signing import ApprovalSigner


def signing_key(tmp_path, name="approval"):
    path = tmp_path / name
    path.write_text(
        base64.urlsafe_b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
    )
    path.chmod(0o600)
    return load_secret_file(path, source=SecretFileVariable.PUBLIC_APPROVAL_SIGNING_KEY)


def test_signature_binds_every_field_and_trusted_key(tmp_path):
    signer = ApprovalSigner(signing_key(tmp_path))
    other = ApprovalSigner(signing_key(tmp_path, "other"))
    binding = ApprovalBinding.model_validate(
        {
            "approval_id": str(uuid4()),
            "owner_id": str(uuid4()),
            "account_id": str(uuid4()),
            "version_id": str(uuid4()),
            "strategy_hash": "a" * 64,
            "broker_account_hash": "c" * 64,
            "connection_generation": 0,
            "expires_at": datetime(2026, 9, 15, tzinfo=UTC),
            "limits": {
                "max_order_notional": "1000",
                "max_account_exposure": "5000",
                "max_position_shares": 10,
                "max_daily_loss": "100",
                "max_daily_turnover": "10000",
            },
        }
    )
    signature = signer.sign(binding)
    assert signer.verifies(binding, signature=signature, key_id=signer.key_id)
    assert not other.verifies(binding, signature=signature, key_id=signer.key_id)
    assert not other.verifies(binding, signature=signature, key_id=other.key_id)
    for field, value in (
        ("owner_id", uuid4()),
        ("account_id", uuid4()),
        ("version_id", uuid4()),
        ("approval_id", uuid4()),
        ("connection_generation", 1),
        ("broker_account_hash", "d" * 64),
        ("strategy_hash", "b" * 64),
    ):
        assert not signer.verifies(
            binding.model_copy(update={field: value}), signature=signature, key_id=signer.key_id
        )
    for value in ("", "invalid!", "a" * 128, "é"):
        assert not signer.verifies(binding, signature=value, key_id=signer.key_id)
    with pytest.raises(ValueError):
        signer.sign(binding.model_copy(update={"connection_generation": False}))
