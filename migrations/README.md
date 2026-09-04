# Database migrations

These Alembic migrations own the PostgreSQL `market_data` schema used by the
standalone collector. The database URL is supplied at runtime through
`APA_MARKET_DATA_MIGRATION_DATABASE_URL`; it is never stored in this repository.
