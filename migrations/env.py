"""Alembic environment.

The database URL is taken from the application's own engine, never from
`alembic.ini`. One source of truth means a migration can never be run against a
different database than the app talks to, and no credentials are committed —
`alembic.ini` deliberately carries no `sqlalchemy.url`.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

# Importing the app's engine also normalises the URL (Railway hands out
# `postgres://`, which SQLAlchemy 2 will not accept) and registers every model
# on Base.metadata for autogenerate.
from app.database import Base, engine
import app.models  # noqa: F401  (registers tables on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade --sql`).

    Useful when a DBA has to review the statements before they touch a
    production database.
    """
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most columns in place; batch mode rewrites the
        # table instead. Harmless on Postgres, essential on SQLite.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the app's engine."""
    connectable = config.attributes.get("connection", None)

    if connectable is not None:
        # A connection was handed in by the caller (the startup bootstrap in
        # app/migrate.py), so reuse it rather than opening a second one.
        _run(connectable)
        return

    with engine.connect() as connection:
        _run(connection)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
