"""Schema migrations: that they match the models, and that the startup
bootstrap handles every database it will actually meet.

The drift test is the one that earns its keep day to day — it fails the moment
somebody edits a model without writing a migration, which is otherwise a
mistake you discover on deploy. The bootstrap tests cover the three states this
app has live databases in, including the one that predates Alembic entirely.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import Base
from app.migrate import VERSION_TABLE, alembic_config, current_revision, run_migrations
import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.models.enums import LessonStatus, Role, WatchdogStatus
from app.models.lesson import Lesson
from app.models.progress import Progress
from app.models.school import School
from app.models.user import User

# Spelled out rather than imported from app.migrate: these tests exist to say
# what the right revision is, so they must not follow the source if it changes.
INITIAL_REVISION = "ecdcb1245c53"
BEFORE_RETIRE_LATE = "c47f0a6e21b8"
RETIRE_LATE = "d8b21c60fa73"


@pytest.fixture()
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / f'mig_{uuid.uuid4().hex}.db').as_posix()}"


def _engine(url: str):
    return create_engine(url, future=True)


def _upgrade(engine) -> None:
    _upgrade_to(engine, "head")


def _upgrade_to(engine, revision: str) -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), revision)


def _head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _pre_alembic(engine, revision: str = INITIAL_REVISION) -> None:
    """A database in the state case 2 describes.

    Built by the old `create_all` path, so it carries *that era's* schema and no
    `alembic_version` table — not today's schema, which is what makes the
    difference between stamping the levelled revision and stamping head visible
    at all.
    """
    _upgrade_to(engine, revision)
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE {VERSION_TABLE}"))


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _seed_one_progress_row(engine) -> None:
    """The smallest graph a `progress` row needs to exist."""
    with Session(engine) as session:
        session.add(School(id="sch_1", name="Test School"))
        session.add(
            User(
                id="usr_1",
                name="A Teacher",
                email="teacher@example.com",
                password_hash="not-a-real-hash",
                role=Role.teacher,
            )
        )
        session.add(Lesson(id="les_1", title="Loops", grade=7, subject="python"))
        session.flush()
        session.add(Progress(id="prg_1", teacher_id="usr_1", lesson_id="les_1"))
        session.commit()


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
    _pre_alembic(engine)
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


def test_pre_alembic_database_still_receives_the_later_migrations(sqlite_url):
    """Case 2 has to finish at head, not at the revision it was levelled to.

    Levelling only ever reaches the initial schema, so a stamp at head there
    records four migrations as applied without running them and their columns
    never arrive. Nothing fails at the time: the app boots, every later startup
    is a no-op upgrade, and the break surfaces on the first query against a
    column that was supposed to have been added.
    """
    engine = _engine(sqlite_url)
    _pre_alembic(engine)

    assert "fair_sections" not in _tables(engine), "precondition: pre-sections schema"
    assert "section" not in _columns(engine, "progress"), "precondition"

    run_migrations(engine)

    assert current_revision(engine) == _head_revision(), (
        "a levelled database must be carried all the way to head"
    )
    assert "fair_sections" in _tables(engine)
    for table, column in (
        ("progress", "section"),
        ("users", "sections"),
        ("chat_messages", "section"),
        ("access_requests", "section"),
        ("fair_projects", "section_id"),
    ):
        assert column in _columns(engine, table), (
            f"{table}.{column} is added by a migration after {INITIAL_REVISION};"
            " levelling cannot produce it, so the upgrade has to run"
        )


def test_older_database_missing_columns_is_levelled_before_stamping(sqlite_url):
    """The bootstrap's one genuinely dangerous case.

    A database old enough to be missing post-v1 columns must have them added
    *before* it is stamped. Stamping first would record "already at head" over
    a schema that is not, and the missing columns would never arrive — a
    failure that surfaces later, as a query against a column that isn't there.
    """
    engine = _engine(sqlite_url)
    _pre_alembic(engine)

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


def test_bootstrap_leaves_the_application_loggers_alone(sqlite_url):
    """Running migrations must not switch off the logging around them.

    `fileConfig` disables every logger its file does not name, and `alembic.ini`
    names only root, alembic and sqlalchemy.engine. The bootstrap runs inside
    the app's startup, by which point uvicorn's loggers and the app's own
    already exist — so the default left the process with no request log, no
    uvicorn error log, and silence from every `logger.exception` in the
    codebase. The deliberately-swallowed failures were the ones it hid best:
    the nightly backup, the chat-retention purge, a chat write that failed.
    """
    engine = _engine(sqlite_url)
    names = ["uvicorn", "uvicorn.error", "uvicorn.access", "app", "app.migrate"]
    for name in names:
        logging.getLogger(name).disabled = False

    delivered: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            delivered.append(record.getMessage())

    app_logger = logging.getLogger("app")
    handler = _Capture()
    app_logger.addHandler(handler)
    try:
        run_migrations(engine)
        # What app/main.py does when the nightly backup raises.
        app_logger.error("Daily backup email failed")
    finally:
        app_logger.removeHandler(handler)

    assert [n for n in names if logging.getLogger(n).disabled] == [], (
        "the bootstrap disabled loggers it does not own"
    )
    # The flag is the mechanism; this is the thing that actually matters, since
    # a disabled logger drops the record before any handler sees it.
    assert delivered == ["Daily backup email failed"]


def test_create_all_database_is_stamped_where_it_actually_stands(sqlite_url):
    """Unstamped does not mean old.

    A database built by `create_all` against current models has no version
    table either, but it is already at head — the test suite makes one on every
    run, and so does anyone who has ever started the app before Alembic owned
    the schema. Stamping it at the initial revision sends the upgrade back over
    migrations it was already built with, and the first CREATE TABLE fails, so
    the app does not start at all.
    """
    engine = _engine(sqlite_url)
    Base.metadata.create_all(bind=engine)
    assert current_revision(engine) is None, "precondition: not yet stamped"

    run_migrations(engine)

    assert current_revision(engine) == _head_revision()
    assert "fair_sections" in _tables(engine)


def test_retiring_late_writes_enum_names_not_values(sqlite_url):
    """A data migration has to write what the ORM reads.

    `Enum(..., native_enum=False)` persists the member's *name*, so writing its
    value instead leaves a row that cannot be loaded at all — `LookupError`, and
    with it every screen built on `Progress`. Reading the row back through the
    model is the only assertion that actually proves the string is right.
    """
    engine = _engine(sqlite_url)
    _upgrade_to(engine, BEFORE_RETIRE_LATE)
    _seed_one_progress_row(engine)

    # The state the migration exists to clear, spelled as the old code spelled it.
    with engine.begin() as conn:
        conn.execute(text("UPDATE progress SET status = 'late', watchdog = 'late'"))

    _upgrade(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT status, watchdog FROM progress")).one() == (
            "in_progress",
            "on_track",
        )
    with Session(engine) as session:
        row = session.get(Progress, "prg_1")
        assert row.status is LessonStatus.in_progress
        assert row.watchdog is WatchdogStatus.on_track


def test_rows_corrupted_by_the_first_retire_late_are_repaired(sqlite_url):
    """The databases that already ran the broken version.

    Alembic will not re-run a revision it has recorded, so fixing that migration
    does nothing for a database that has been through it. A hyphenated string in
    either column can only have come from there — every application write goes
    through the ORM — so it is safe to convert on sight.
    """
    engine = _engine(sqlite_url)
    _upgrade_to(engine, RETIRE_LATE)
    _seed_one_progress_row(engine)

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE progress SET status = 'in-progress', watchdog = 'on-track'")
        )

    _upgrade(engine)

    with Session(engine) as session:
        row = session.get(Progress, "prg_1")
        assert row.status is LessonStatus.in_progress
        assert row.watchdog is WatchdogStatus.on_track
