"""Provision fixed PostgreSQL authorization and login roles once per cluster."""

from __future__ import annotations

import argparse
from pathlib import Path

from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from adaptive_trader.platform.storage.role_bootstrap import bootstrap_platform_database_roles

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision the platform PostgreSQL roles without running migrations.",
    )
    parser.add_argument(
        "--admin-database-url-file",
        required=True,
        help="Owner-private file containing the cluster-administrator PostgreSQL URL.",
    )
    return parser.parse_args()


def main() -> None:
    """Load the administrator URL and run the bounded cluster bootstrap."""

    arguments = _arguments()
    admin_database_url = load_secret_file(
        Path(arguments.admin_database_url_file),
        source=SecretFileVariable.DATABASE_URL,
    )
    result = bootstrap_platform_database_roles(
        admin_database_url,
        application_root=_PROJECT_ROOT,
    )
    print(
        "PostgreSQL role bootstrap complete: "
        f"{len(result.created_authorization_roles)} authorization role(s) created, "
        f"{len(result.created_login_roles)} login role(s) created, "
        f"{len(result.reconciled_login_roles)} login role(s) reconciled."
    )


if __name__ == "__main__":
    main()
