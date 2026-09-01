"""Schema migrations: that they match the models, and that the startup
bootstrap handles every database it will actually meet.

The drift test is the one that earns its keep day to day — it fails the moment
somebody edits a model without writing a migration, which is otherwise a
mistake you discover on deploy. The bootstrap tests cover the three states this
app has live databases in, including the one that predates Alembic entirely.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.database import Base
from app.migrate import VERSION_TABLE, alembic_config, current_revision, run_migrations
import app.models  # noqa: F401  (registers every table on Base.metadata)


@pytest.fixture()
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / f'mig_{uuid.uuid4().hex}.db').as_posix()}"


def _engine(url: str):
    return create_engine(url, future=True)


def _upgrade(engine) -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_migrations_match_the_models(sqlite_url):
    """A database built from the migrations equals a database built from the
    models. When this fails, a model was changed and no migration was written."""
    engine = _engine(sqlite_url)
    _upgrade(engine)

    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)

    assert diff == [], (
        "The migrations no longer describe the models. Generate one with:\n"
        "  alembic revision --autogenerate -m 'describe the change'\n"
        f"Differences: {diff}"
    )


def test_upgrade_downgrade_upgrade_round_trip(sqlite_url):
    """Every migration can be undone. A downgrade nobody has run is a downgrade
    that does not work, and it is needed on the day a deploy has to go back."""
    engine = _engine(sqlite_url)
    cfg_tables = None

    _upgrade(engine)
    cfg_tables = _tables(engine)
    assert len(cfg_tables) > 1

    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "base")
    # Only Alembic's own bookkeeping table survives a full downgrade.
    assert _tables(engine) <= {VERSION_TABLE}

    _upgrade(engine)
    assert _tables(engine) == cfg_tables


def test_fresh_database_is_built_from_migrations(sqlite_url):
    engine = _engine(sqlite_url)
    assert _tables(engine) == set()

    run_migrations(engine)

    assert "users" in _tables(engine)
    assert current_revision(engine) is not None


def test_bootstrap_is_idempotent_across_restarts(sqlite_url):
    """Every startup calls this, so running it twice must be a no-op."""
    engine = _engine(sqlite_url)
    run_migrations(engine)
    first = current_revision(engine)
    tables = _tables(engine)

    run_migrations(engine)

    assert current_revision(engine) == first
    assert _tables(engine) == tables


def test_database_predating_alembic_is_stamped_not_rebuilt(sqlite_url):
    """The pre-Alembic case: a schema built by the old `create_all` path, with
    data in it. Running the initial migration against it would fail on tables
    that already exist, so it is stamped instead — and the data has to survive.
    """
    engine = _engine(sqlite_url)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schools (id, name, country, city, program_year,"
                " created_at, updated_at)"
                " VALUES ('sch_1', 'Existing School', 'LB', 'Beirut', 2,"
                " '2026-01-01', '2026-01-01')"
            )
        )

    assert current_revision(engine) is None, "precondition: not yet stamped"

    run_migrations(engine)

    assert current_revision(engine) is not None, "should be stamped after bootstrap"
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM schools")).scalars().all()
    assert rows == ["Existing School"], "existing data must survive the bootstrap"


def test_older_database_missing_columns_is_levelled_before_stamping(sqlite_url):
    """The bootstrap's one genuinely dangerous case.

    A database old enough to be missing post-v1 columns must have them added
    *before* it is stamped. Stamping first would record "already at head" over
    a schema that is not, and the missing columns would never arrive — a
    failure that surfaces later, as a query against a column that isn't there.
    """
    engine = _engine(sqlite_url)
    Base.metadata.create_all(bind=engine)

    dropped = {
        "users": "ict_fair_access",
        "schools": "program_year",
        "security_logs": "detail",
    }
    with engine.begin() as conn:
        for table, column in dropped.items():
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    for table, column in dropped.items():
        assert column not in _columns(engine, table), "precondition: column gone"

    run_migrations(engine)

    for table, column in dropped.items():
        assert column in _columns(engine, table), (
            f"{table}.{column} should have been restored before stamping"
        )
    assert current_revision(engine) is not None
