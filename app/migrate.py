"""Bring the database up to the current schema at startup.

Three states have to be handled, because this app shipped and ran before
Alembic existed and there are live databases in each of them:

1. **Empty** — a fresh deployment. Run every migration; the initial one builds
   the whole schema.
2. **Populated, no `alembic_version`** — a database built by a `create_all`
   path rather than by migrations. *Running* the migrations it already
   satisfies would fail on tables that already exist, so it is brought level by
   `ensure_added_columns` (which backfills anything a very old deploy is
   missing), stamped at the revision its schema actually reaches, and then
   upgraded like any other database.
3. **Populated and stamped** — the normal path from here on. Run whatever is
   newer than the recorded revision.

Getting case 2 wrong is what makes a first Alembic deployment dangerous, and it
can be got wrong in both directions. Stamp too low and the upgrade re-runs a
CREATE TABLE against a table that is already there, so the app does not boot.
Stamp too high — at head, say — and every later migration is recorded as
applied without running, so the columns it adds never arrive; nothing fails at
the time, and the break surfaces much later as a query against a column that
was supposed to exist.

Neither guess is safe because "unstamped" says nothing about how old a database
is: one built by `create_all` against current models is unstamped and already
at head, while one that predates Alembic is unstamped and sits at the initial
schema. So the revision is read off the schema itself (`levelled_revision`),
and case 2 then ends where case 3 does: at `upgrade`.
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

# Every schema-changing revision, paired with something that exists in the
# database if and only if it has been applied.
#
# A case-2 database has no version table, so its own schema is the only evidence
# of how far it has come, and both ways of guessing are harmful: too high skips
# migrations and the columns never arrive, too low re-runs a CREATE TABLE and
# the app will not boot. An unstamped database is not necessarily an *old* one —
# anything built by `create_all` against current models is unstamped and already
# at head — so the revision has to be read off the schema rather than assumed.
#
# Only schema-changing revisions belong here, in chain order. A data-only
# revision has nothing to detect and is safe to re-run anyway — its WHERE clause
# matches nothing the second time — so it is simply carried by whichever marker
# follows it. Where none follows, it is left past the end of the list and every
# levelled database receives it.
_SCHEMA_MARKERS: tuple[tuple[str, str, str | None], ...] = (
    ("ecdcb1245c53", "users", None),
    ("42958f0e7fe1", "fair_sections", None),
    ("9b3d71c8a4e5", "progress", "section"),
    ("c47f0a6e21b8", "chat_messages", "section"),
    ("f3a91c47b2e5", "users", "failed_login_window_started_at"),
    ("c5d82b1e4f07", "login_throttles", "cycle_started_at"),
)


def levelled_revision(target_engine: Engine) -> str:
    """The newest revision this database's schema already satisfies.

    Walks the markers in order and stops at the first thing that is missing:
    the revision before that gap is the last one fully applied. The initial
    revision is the floor, because `ensure_added_columns` has just guaranteed
    at least that much.
    """
    inspector = inspect(target_engine)
    tables = set(inspector.get_table_names())

    reached = _SCHEMA_MARKERS[0][0]
    for revision, table, column in _SCHEMA_MARKERS:
        if table not in tables:
            break
        if column is not None and column not in {
            c["name"] for c in inspector.get_columns(table)
        }:
            break
        reached = revision
    return reached


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
            # Case 2. Level the schema, record the revision it is *actually* at,
            # and then fall through to the upgrade that carries it the rest of
            # the way. Stamping head here would strand an old database at the
            # initial schema; stamping the initial revision would make a
            # create_all database re-run migrations it has already been built
            # with. Only the schema itself can say which of the two this is.
            ensure_added_columns(target_engine)
            reached = levelled_revision(target_engine)
            logger.info(
                "Database has no version table — levelled schema, stamping %s.",
                reached,
            )
            command.stamp(cfg, reached)
        elif not has_tables:
            logger.info("Empty database — creating schema from migrations.")

        command.upgrade(cfg, "head")


def current_revision(target_engine: Engine | None = None) -> str | None:
    """The revision the database is stamped at, or None if it has never been."""
    from alembic.runtime.migration import MigrationContext

    with (target_engine or engine).connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()
