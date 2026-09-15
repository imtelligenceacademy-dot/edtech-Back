"""Database engine, session factory, and the declarative Base.

SQLite is configured with WAL journaling and enforced foreign keys — neither is
on by default in SQLite and both matter for correctness/concurrency.
"""

from __future__ import annotations

import sqlite3

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

IS_SQLITE = settings.database_url.startswith("sqlite")


def _engine_url(url: str) -> str:
    """Normalize the DB URL. Railway/Heroku hand out ``postgres://`` or
    ``postgresql://`` (which default to psycopg2); rewrite both to use the
    installed psycopg (v3) driver. SQLite and other URLs are left untouched."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


_connect_args = {"check_same_thread": False} if IS_SQLITE else {}

# Postgres connection pool. The default (5 + 10 overflow = 15) is exactly our
# expected peak of ~15 concurrent users, and PDF downloads / AI SSE streams hold
# a connection for their whole duration — so a simultaneous burst could brush the
# ceiling. 10 + 20 gives comfortable headroom. SQLite (dev) uses its own pool and
# ignores these, so only pass them for Postgres.
_pool_args = {} if IS_SQLITE else {"pool_size": 10, "max_overflow": 20, "pool_timeout": 30}

engine = create_engine(
    _engine_url(settings.database_url),
    connect_args=_connect_args,
    echo=False,
    future=True,
    # Verify connections before use — cloud Postgres drops idle connections.
    pool_pre_ping=not IS_SQLITE,
    **_pool_args,
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Enable foreign-key enforcement and WAL mode on every SQLite connection.

    Decided from the connection being opened, not from the configured database.
    This listener is registered on `Engine` itself, so it sees every engine in
    the process — and keyed on a module-level flag it got both cases wrong at
    once: with Postgres configured, the throwaway SQLite databases the tests
    build ran with foreign keys *off*, so the cascade behaviour they exist to
    check was not being enforced in the one CI job that runs against Postgres.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the initial schema. `create_all` never alters existing
# tables, so we add any missing columns by hand. `ALTER TABLE ... ADD COLUMN` is
# supported by both SQLite and Postgres and is idempotent + data-preserving; the
# DDL below is written to be valid on both dialects (plain types, integer
# DEFAULTs). New NOT-NULL columns carry a DEFAULT, so the ADD backfills existing
# rows (e.g. every current school/lesson becomes year 2).
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "progress": {
        "completed_at": "DATETIME",
        "unlocked_override": "BOOLEAN NOT NULL DEFAULT 0",
        "last_slide": "INTEGER",
        "slide_total": "INTEGER",
    },
    "schools": {
        "program_year": "INTEGER NOT NULL DEFAULT 2",
    },
    "lessons": {
        "year": "INTEGER NOT NULL DEFAULT 2",
        "course": "VARCHAR(16)",
    },
    "users": {
        "ict_fair_access": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "security_logs": {
        "detail": "VARCHAR(300) NOT NULL DEFAULT ''",
    },
}


# Where the two databases spell the same column differently. Small and closed:
# the dict above is frozen — new columns go in a migration — so this is all of
# it, and `tests/test_added_column_ddl.py` checks the result against what the
# models themselves compile to on each dialect rather than trusting this list.
_POSTGRES_SPELLING = (
    # Postgres has no DATETIME at all, so this did not produce a wrong column,
    # it produced `type "datetime" does not exist` — out of `ensure_added_columns`,
    # out of `run_migrations`, out of the startup lifespan, and the app did not
    # boot. The zone has to come with it: the models are `DateTime(timezone=True)`,
    # and a bare TIMESTAMP would be drift nobody would notice until later.
    ("DATETIME", "TIMESTAMP WITH TIME ZONE"),
)


def _dialect_ddl(ddl: str, dialect: str) -> str:
    """One column's DDL, spelled the way this database spells it.

    `dialect` is the target engine's, which is not always the configured one.
    `ensure_added_columns` takes an engine precisely so it can be run against
    another database, and the tests hand it throwaway SQLite ones — so deciding
    this from a module-level flag meant the Postgres CI job emitted Postgres DDL
    into SQLite, which is the same mistake in the other direction and in the one
    job that exists to catch it.
    """
    if dialect != "postgresql":
        return ddl
    for sqlite_spelling, postgres_spelling in _POSTGRES_SPELLING:
        ddl = ddl.replace(sqlite_spelling, postgres_spelling)
    # Postgres wants true/false for a BOOLEAN default; `DEFAULT 0` is the
    # portable form for SQLite.
    if "BOOLEAN" in ddl.upper():
        ddl = ddl.replace("DEFAULT 0", "DEFAULT false").replace("DEFAULT 1", "DEFAULT true")
    return ddl


def ensure_added_columns(target_engine: Engine | None = None) -> None:
    """Add post-v1 columns to existing tables without touching their data.

    Runs on both SQLite (dev) and Postgres (prod). Alembic now owns the schema,
    but this stays for one job: levelling a database built by the old
    `create_all` path up to the initial migration before it is stamped. See
    app/migrate.py. New schema changes belong in a migration, not in
    `_ADDED_COLUMNS`.

    `target_engine` exists so the bootstrap can be exercised against a
    throwaway database in tests; it defaults to the application engine."""
    target_engine = target_engine or engine
    dialect = target_engine.dialect.name
    inspector = inspect(target_engine)
    existing_tables = set(inspector.get_table_names())
    with target_engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all will build it fresh with all columns
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN {name} "
                            f"{_dialect_ddl(ddl, dialect)}"
                        )
                    )
