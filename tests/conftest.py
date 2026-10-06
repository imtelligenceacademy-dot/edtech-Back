"""Test configuration.

Environment is set before any app module is imported so the settings singleton
picks up a throwaway SQLite database instead of the developer's real one.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / f"imt_test_{uuid.uuid4().hex}.db"

# TEST_DATABASE_URL, not DATABASE_URL, deliberately. The schema fixture creates
# and drops every table, so it must be impossible to point the suite at a real
# database by having DATABASE_URL exported in your shell. Opting in takes a
# variable nothing else sets — CI uses it to run the suite against Postgres,
# which is what production runs and SQLite is not.
_EXTERNAL_DB = os.environ.get("TEST_DATABASE_URL", "").strip()
_USING_SQLITE = not _EXTERNAL_DB

os.environ["DATABASE_URL"] = _EXTERNAL_DB or f"sqlite:///{_TMP_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-not-used-in-production"
os.environ["ENVIRONMENT"] = "development"
os.environ["AI_PROVIDER"] = "mock"          # never call a paid API from tests
os.environ["OPENAI_API_KEY"] = ""
os.environ["AI_TEACHER_VISION_ENABLED"] = "true"

import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: F401,E402  (registers tables)


if _USING_SQLITE:
    # Python's sqlite3 driver manages transactions itself by default: it issues
    # its own BEGIN when it thinks one is needed and commits behind SQLAlchemy's
    # back. A SAVEPOINT cannot be relied on under that, which is why the first
    # attempt at isolating tests (below) let committed rows survive the rollback
    # meant to remove them. Handing the driver's transaction control to
    # SQLAlchemy is the documented recipe; Postgres needs none of it.
    @event.listens_for(engine, "connect")
    def _driver_leaves_transactions_alone(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_when_sqlalchemy_says(conn):
        conn.exec_driver_sql("BEGIN")


@pytest.fixture(scope="session", autouse=True)
def _schema():
    # An external database may be left over from a previous run, so start clean.
    if not _USING_SQLITE:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    if not _USING_SQLITE:
        Base.metadata.drop_all(engine)
    engine.dispose()
    _TMP_DB.unlink(missing_ok=True)


@pytest.fixture()
def db(request):
    """A session whose writes are undone at the end, including committed ones.

    The suite shares one database and never truncates it, so isolation used to
    rest on nothing calling ``commit`` — and much does: uploads, progress
    resets, and every lesson PDF served, which writes a line to the security
    log. The app is handed this same session, so its commit made permanent the
    lessons and teachers the test had only flushed, and they then broke tests
    in other modules, which is where the failures surfaced.

    So the session is bound to a transaction this fixture owns. With
    ``create_savepoint`` the session's own ``commit`` releases a SAVEPOINT
    inside it instead of ending it, and rolling the outer transaction back at
    the end takes every write with it, committed or not.

    Every *other* session opened during the test is pointed at the same
    connection too. The app opens its own in a few places — saving a chat
    exchange, refunding a failed question, building a file archive — and on a
    separate connection those could not see anything the test had set up, so
    they failed or, worse, saved nothing and passed. ``SessionLocal`` is one
    object shared by every module that imported it, so configuring it here
    reaches all of them, and it is put back exactly as it was afterwards.

    A test that needs its commits seen from other connections — threads racing
    each other on Postgres — marks itself ``real_commits`` and gets a plain
    session, and cleans up after itself.
    """
    if request.node.get_closest_marker("real_commits"):
        session = SessionLocal()
        try:
            yield session
        finally:
            session.rollback()
            session.close()
        return

    connection = engine.connect()
    outer = connection.begin()
    configured = dict(SessionLocal.kw)
    SessionLocal.configure(bind=connection, join_transaction_mode="create_savepoint")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        SessionLocal.kw.clear()
        SessionLocal.kw.update(configured)
        if outer.is_active:
            outer.rollback()
        connection.close()
