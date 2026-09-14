"""Create owner-private, disposable development configuration without exposing values."""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    root = Path(__file__).resolve().parent
    local = root / ".local"
    local.mkdir(mode=0o700, exist_ok=False)
    postgres, runtime, migrate, oidc, admin = (secrets.token_urlsafe(32) for _ in range(5))

    def write(name: str, content: str) -> None:
        fd = os.open(local / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)

    write("postgres_password", postgres)
    write("oidc_secret", oidc)
    write("encryption_key", Fernet.generate_key().decode())
    write(
        "approval_signing_key",
        base64.urlsafe_b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode(),
    )
    write(
        "database_url",
        f"postgresql+psycopg://aqa_public_runtime:{runtime}@127.0.0.1:55438/collector_test",
    )
    write(
        "migration_url",
        f"postgresql+psycopg://aqa_public_migrate:{migrate}@127.0.0.1:55438/collector_test",
    )
    write(
        "keycloak.env",
        f"KC_BOOTSTRAP_ADMIN_USERNAME=local-admin\nKC_BOOTSTRAP_ADMIN_PASSWORD={admin}\n",
    )
    write(
        "init.sql",
        f"""
CREATE ROLE aqa_public_runtime LOGIN PASSWORD '{runtime}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE aqa_public_migrate LOGIN PASSWORD '{migrate}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT CONNECT ON DATABASE collector_test TO aqa_public_runtime, aqa_public_migrate;
GRANT CREATE ON DATABASE collector_test TO aqa_public_migrate;
""",
    )
    write(
        "realm.json",
        json.dumps(
            {
                "realm": "paper",
                "enabled": True,
                "displayName": "Paper workspace",
                "registrationAllowed": True,
                "registrationEmailAsUsername": True,
                "resetPasswordAllowed": True,
                "verifyEmail": True,
                "duplicateEmailsAllowed": False,
                "rememberMe": False,
                "bruteForceProtected": True,
                "failureFactor": 5,
                # Keycloak policy expression, not a password.
                "passwordPolicy": "length(12) and notUsername and notEmail",  # pragma: allowlist secret
                "accessTokenLifespan": 900,
                "ssoSessionIdleTimeout": 900,
                "ssoSessionMaxLifespan": 3600,
                "smtpServer": {
                    "host": "mail",
                    "port": "1025",
                    "from": "identity@example.invalid",
                    "auth": "false",
                    "ssl": "false",
                    "starttls": "false",
                },
                "clients": [
                    {
                        "clientId": "paper-web",
                        "enabled": True,
                        "publicClient": False,
                        "secret": oidc,
                        "standardFlowEnabled": True,
                        "directAccessGrantsEnabled": False,
                        "serviceAccountsEnabled": False,
                        "redirectUris": ["http://127.0.0.1:5178/auth/callback"],
                        "webOrigins": ["http://127.0.0.1:5178"],
                        "attributes": {"pkce.code.challenge.method": "S256"},
                        "protocolMappers": [
                            {
                                "name": "paper-web-audience",
                                "protocol": "openid-connect",
                                "protocolMapper": "oidc-audience-mapper",
                                "config": {
                                    "included.client.audience": "paper-web",
                                    "access.token.claim": "true",
                                    "id.token.claim": "false",
                                },
                            }
                        ],
                    }
                ],
            }
        ),
    )
    print(
        "Disposable development configuration created in apps/public/.local. Values were not printed."
    )


if __name__ == "__main__":
    main()
