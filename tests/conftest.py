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

os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
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
    Base.metadata.create_all(engine)
    yield
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
