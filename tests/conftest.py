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

from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: F401,E402  (registers tables)


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
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
