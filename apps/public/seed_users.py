"""Provision two synthetic local identities for browser tests, never production accounts."""

from pathlib import Path

import requests

from adaptive_trader.platform.security import SecretFileVariable, load_secret_file


def main() -> None:
    # Only this development bootstrap consumes the generated local admin environment file.
    path = Path(__file__).resolve().parent / ".local/keycloak.env"
    configuration = load_secret_file(path, source=SecretFileVariable.OPERATOR_TOKEN).reveal()
    values = dict(line.split("=", 1) for line in configuration.splitlines() if "=" in line)
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(
            "http://127.0.0.1:8188/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": values["KC_BOOTSTRAP_ADMIN_USERNAME"],
                "password": values["KC_BOOTSTRAP_ADMIN_PASSWORD"],
            },
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise SystemExit("Local identity bootstrap failed.")
        session.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        for name in ("fixture-one", "fixture-two"):
            response = session.post(
                "http://127.0.0.1:8188/admin/realms/paper/users",
                json={
                    "username": name + "@example.invalid",
                    "email": name + "@example.invalid",
                    "firstName": "Synthetic",
                    "lastName": name,
                    "emailVerified": True,
                    "enabled": True,
                    "credentials": [
                        {
                            "type": "password",
                            "temporary": False,
                            "value": "SYNTHETIC-LOCAL-PASSWORD-ONLY",
                        }
                    ],
                },
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code not in (201, 409):
                raise SystemExit("Local fixture creation failed.")
    print("Two synthetic identities available in the disposable realm only.")


if __name__ == "__main__":
    main()
