"""Run the bounded PostgreSQL platform migration entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from adaptive_trader.platform.storage.migration_runner import migrate_platform_database

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply platform migrations with a bounded legacy-owner handoff and the fixed "
            "non-superuser migration login."
        ),
    )
    parser.add_argument(
        "--database-url-file",
        required=True,
        help="Owner-private file containing the target or legacy-owner PostgreSQL URL.",
    )
    parser.add_argument(
        "--bootstrap-admin-database-url-file",
        help=(
            "Owner-private cluster-administrator URL file, required only to finalize a legacy "
            "role handoff."
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Load the bounded migration authority and apply the checked-in Alembic head."""

    arguments = _arguments()
    base_database_url = load_secret_file(
        Path(arguments.database_url_file),
        source=SecretFileVariable.DATABASE_URL,
    )
    bootstrap_admin_database_url = (
        None
        if arguments.bootstrap_admin_database_url_file is None
        else load_secret_file(
            Path(arguments.bootstrap_admin_database_url_file),
            source=SecretFileVariable.DATABASE_URL,
        )
    )
    migrate_platform_database(
        base_database_url,
        application_root=_PROJECT_ROOT,
        bootstrap_admin_database_url=bootstrap_admin_database_url,
    )
    print("PostgreSQL migrations are at the checked-in head.")


if __name__ == "__main__":
    main()
