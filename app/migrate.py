"""Bring the database up to the current schema at startup.

Three states have to be handled, because this app shipped and ran before
Alembic existed and there are live databases in each of them:

1. **Empty** — a fresh deployment. Run every migration; the initial one builds
   the whole schema.
2. **Populated, no `alembic_version`** — a database built by the old
   `create_all` + `ensure_added_columns` path. Its schema already matches the
   initial migration, so *running* that migration would fail on tables that
   already exist. It is brought level by `ensure_added_columns` (which back-
   fills anything a very old deploy is missing) and then stamped, which records
   "you are already at this revision" without touching a table.
3. **Populated and stamped** — the normal path from here on. Run whatever is
   newer than the recorded revision.

Getting case 2 wrong is what makes a first Alembic deployment dangerous: a
stamp on a database that is *not* actually at the initial schema silently skips
the columns it is missing. So `ensure_added_columns` runs first and stays
around for exactly that reason, even though new work should now go in a
migration rather than in that dict.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.database import engine, ensure_added_columns

logger = logging.getLogger("app.migrate")

# Resolved from this file, not the working directory: the Procfile starts
# uvicorn from the repo root but nothing guarantees that everywhere.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"
_MIGRATIONS_DIR = _BACKEND_ROOT / "migrations"

VERSION_TABLE = "alembic_version"


def alembic_config(connection=None) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    if connection is not None:
        # env.py picks this up and reuses the connection instead of opening a
        # second one, so the whole bootstrap runs on one transaction.
        cfg.attributes["connection"] = connection
    return cfg


def database_state(target_engine: Engine | None = None) -> tuple[bool, bool]:
    """(has_tables, is_stamped) for the given database."""
    target_engine = target_engine or engine
    tables = set(inspect(target_engine).get_table_names())
    return bool(tables - {VERSION_TABLE}), VERSION_TABLE in tables


def run_migrations(target_engine: Engine | None = None) -> None:
    """Bring the database to head. Safe to call on every startup.

    `target_engine` defaults to the application engine; it is a parameter so
    the tests can drive this exact function against a throwaway database
    rather than reimplementing its branches and testing the copy.
    """
    target_engine = target_engine or engine
    has_tables, is_stamped = database_state(target_engine)

    with target_engine.begin() as connection:
        cfg = alembic_config(connection)

        if has_tables and not is_stamped:
            # Case 2. Level the schema first, then record where it stands.
            logger.info(
                "Database predates Alembic — levelling schema and stamping head."
            )
            ensure_added_columns(target_engine)
            command.stamp(cfg, "head")
            return

        if not has_tables:
            logger.info("Empty database — creating schema from migrations.")

        command.upgrade(cfg, "head")


def current_revision(target_engine: Engine | None = None) -> str | None:
    """The revision the database is stamped at, or None if it has never been."""
    from alembic.runtime.migration import MigrationContext

    with (target_engine or engine).connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()
