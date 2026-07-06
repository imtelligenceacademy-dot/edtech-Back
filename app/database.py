"""Database engine, session factory, and the declarative Base.

SQLite is configured with WAL journaling and enforced foreign keys — neither is
on by default in SQLite and both matter for correctness/concurrency.
"""

from __future__ import annotations

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

engine = create_engine(
    _engine_url(settings.database_url),
    connect_args=_connect_args,
    echo=False,
    future=True,
    # Verify connections before use — cloud Postgres drops idle connections.
    pool_pre_ping=not IS_SQLITE,
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Enable foreign-key enforcement and WAL mode on every SQLite connection."""
    if IS_SQLITE:
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
    },
    "schools": {
        "program_year": "INTEGER NOT NULL DEFAULT 2",
    },
    "lessons": {
        "year": "INTEGER NOT NULL DEFAULT 2",
        "course": "VARCHAR(16)",
    },
}


def ensure_added_columns() -> None:
    """Add post-v1 columns to existing tables without touching their data.

    Runs on both SQLite (dev) and Postgres (prod) since Alembic isn't set up
    yet. On a fresh DB `create_all` already includes every column, so the
    inspector check below makes this a no-op there; on an existing DB it adds
    only the genuinely missing columns. Each entry's DDL is dialect-neutral."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all will build it fresh with all columns
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
