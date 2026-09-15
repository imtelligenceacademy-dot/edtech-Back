"""The one piece of hand-written DDL left, and whether both databases accept it.

`_ADDED_COLUMNS` is raw SQL, not SQLAlchemy, because it runs before Alembic owns
the schema — it is what levels a pre-Alembic database up to the initial revision
so it can be stamped. Raw SQL means nothing checks it against the dialect it is
about to meet, and one entry had been wrong since it was written: Postgres has no
DATETIME type, so `ALTER TABLE progress ADD COLUMN completed_at DATETIME` does
not add a badly-typed column, it raises `type "datetime" does not exist` — out of
`ensure_added_columns`, out of `run_migrations`, out of the startup lifespan, and
the app does not boot.

Nothing caught it because the failure is Postgres-only and every migration test
builds SQLite. The CI job that runs against Postgres builds SQLite here too.

So this asserts the emitted type against what the models themselves compile to on
each dialect. That is the property that matters — the levelled column has to be
the column the migrations and models expect — and it holds whatever anyone
spells in that dict later.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import DateTime, inspect, text
from sqlalchemy.dialects import postgresql, sqlite

from app.database import _ADDED_COLUMNS, _dialect_ddl
import app.models  # noqa: F401  (registers the tables)
from app.database import Base

DIALECTS = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}


def _model_column(table: str, column: str):
    return Base.metadata.tables[table].columns[column]


def _emitted_type(ddl: str, dialect: str) -> str:
    """The type at the front of a column's DDL, without the constraints."""
    spelled = _dialect_ddl(ddl, dialect)
    for suffix in (" NOT NULL", " DEFAULT"):
        cut = spelled.find(suffix)
        if cut != -1:
            spelled = spelled[:cut]
    return spelled.strip()


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_every_added_column_is_the_type_the_model_says_it_is(dialect_name):
    dialect = DIALECTS[dialect_name]

    for table, columns in _ADDED_COLUMNS.items():
        for name, ddl in columns.items():
            expected = _model_column(table, name).type.compile(dialect)
            assert _emitted_type(ddl, dialect_name) == expected, (
                f"{table}.{name} is levelled as "
                f"{_emitted_type(ddl, dialect_name)!r} on {dialect_name}, but the "
                f"model compiles to {expected!r} there"
            )


def test_postgres_is_never_handed_a_datetime():
    """The specific one, said out loud, because it cost a boot rather than a
    column: `DATETIME` is not a type Postgres has."""
    for table, columns in _ADDED_COLUMNS.items():
        for name, ddl in columns.items():
            spelled = _dialect_ddl(ddl, "postgresql")
            assert "DATETIME" not in spelled.upper(), f"{table}.{name}: {spelled}"


def test_a_timestamp_levelled_into_postgres_keeps_its_time_zone():
    """Every timestamp in the models is `DateTime(timezone=True)`. A bare
    TIMESTAMP would be accepted by Postgres and quietly disagree with them —
    a worse outcome than the crash, because nothing would report it."""
    spelled = _dialect_ddl(_ADDED_COLUMNS["progress"]["completed_at"], "postgresql")

    assert spelled.upper().startswith("TIMESTAMP WITH TIME ZONE")


def test_sqlite_is_left_alone():
    """The dict is spelled the way SQLite renders these, which is what the old
    `create_all` path produced on the databases it exists to level."""
    for table, columns in _ADDED_COLUMNS.items():
        for name, ddl in columns.items():
            assert _dialect_ddl(ddl, "sqlite") == ddl


def test_a_boolean_default_is_translated_for_postgres_only():
    boolean = _ADDED_COLUMNS["users"]["ict_fair_access"]

    assert "DEFAULT false" in _dialect_ddl(boolean, "postgresql")
    assert "DEFAULT 0" in _dialect_ddl(boolean, "sqlite")


# --------------------------------------------------------------------------- #
# Against the database this actually breaks on
# --------------------------------------------------------------------------- #
ON_POSTGRES = "postgresql" in os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not ON_POSTGRES, reason="needs TEST_DATABASE_URL on Postgres")
def test_levelling_a_postgres_database_actually_works():
    """The test the suite did not have, which is why this shipped.

    Everything above reasons about strings. This runs the DDL at a real Postgres
    and is the only thing here that would have caught the original: `DATETIME`
    is not a type it has, so the statement raised rather than adding a column,
    and the exception came up through `run_migrations` and the startup lifespan
    until the app refused to boot.

    Every other migration test builds SQLite, including in the CI job whose
    entire purpose is Postgres. This one is skipped there and nowhere else.
    """
    from app.database import engine, ensure_added_columns

    def has_column() -> bool:
        return "completed_at" in {c["name"] for c in inspect(engine).get_columns("progress")}

    assert has_column(), "precondition: the schema fixture built it"

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE progress DROP COLUMN completed_at"))
    assert not has_column(), "precondition: dropped"

    try:
        ensure_added_columns(engine)

        assert has_column(), "levelling has to put the column back"
        restored = next(
            c for c in inspect(engine).get_columns("progress") if c["name"] == "completed_at"
        )
        assert isinstance(restored["type"], DateTime)
        assert restored["type"].timezone, (
            "the models are DateTime(timezone=True); a bare TIMESTAMP here would "
            "disagree with them silently"
        )
    finally:
        # Leave the schema as it was even if the assertions above did not hold,
        # so one failure does not take the rest of the run with it.
        if not has_column():
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE progress ADD COLUMN completed_at "
                        "TIMESTAMP WITH TIME ZONE"
                    )
                )


def test_a_sqlite_engine_gets_its_pragmas_whatever_is_configured(tmp_path):
    """Foreign keys are off by default in SQLite, and this app depends on them.

    The listener that turns them on is registered on `Engine` itself, so it sees
    every engine in the process — but it was keyed on the *configured* database
    rather than the connection being opened. With Postgres configured, the
    throwaway SQLite databases these tests build ran without foreign keys, so
    the cascades they check were not being enforced in the one CI job that runs
    against Postgres. Asked of the connection, it is right either way.
    """
    from sqlalchemy import create_engine

    throwaway = create_engine(
        f"sqlite:///{(tmp_path / 'pragma.db').as_posix()}", future=True
    )
    with throwaway.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
