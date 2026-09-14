"""Maintained Ed25519 signatures for explicit customer approval receipts."""

from __future__ import annotations

import base64
import binascii
import hashlib

from adaptive_trader.platform.security import RedactedSecret
from adaptive_trader.public_product.approvals import ApprovalBinding
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class ApprovalSigner:
    def __init__(self, key: RedactedSecret) -> None:
        raw = base64.b64decode(key.reveal(), altchars=b"-_", validate=True)
        self._key = Ed25519PrivateKey.from_private_bytes(raw)
        self.key_id = hashlib.sha256(self._key.public_key().public_bytes_raw()).hexdigest()

    @staticmethod
    def _message(binding: ApprovalBinding) -> bytes:
        binding = ApprovalBinding.model_validate(binding)
        return ("aqa-public-approval-v1:" + str(binding.content_hash)).encode("ascii")

    def sign(self, binding: ApprovalBinding) -> str:
        return base64.urlsafe_b64encode(self._key.sign(self._message(binding))).decode("ascii")

    def verifies(self, binding: ApprovalBinding, *, signature: str, key_id: str) -> bool:
        if key_id != self.key_id:
            return False
        try:
            raw = base64.b64decode(signature, altchars=b"-_", validate=True)
            self._key.public_key().verify(raw, self._message(binding))
            return True
        except (InvalidSignature, ValueError, binascii.Error):
            return False
