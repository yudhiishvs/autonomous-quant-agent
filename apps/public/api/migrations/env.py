"""Run explicit public-schema migrations with separate migration credentials."""

import os

from adaptive_trader.platform.security import SecretFileVariable, load_secret_file
from alembic import context
from sqlalchemy import create_engine

secret = load_secret_file(
    os.environ["AQA_DATABASE_URL_FILE"], source=SecretFileVariable.DATABASE_URL
)
engine = create_engine(secret.reveal(), hide_parameters=True)
with engine.connect() as connection:
    # Alembic's version table belongs to the new schema, never the private platform.
    connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS aqa_public")
    connection.commit()
    context.configure(connection=connection, version_table_schema="aqa_public")
    with context.begin_transaction():
        context.run_migrations()
