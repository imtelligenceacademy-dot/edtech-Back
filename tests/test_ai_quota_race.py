"""Spending the same question twice.

`enforce_ai_limit` counted, and `record_ai_usage` inserted, with nothing between
them. A teacher one question below the cap who clicks Send six times — which a
browser will do, and an impatient teacher mid-lesson will cause — sent six
requests that all read the same count, all passed, and all inserted.

Cost only; no access is widened. But the counting is exactly the shape the login
counters had, and that one was bypassable by anybody who wanted to be.

The real assertion lives in the Postgres test at the bottom: SQLite serialises
writes database-wide, so the race this fixes cannot be reproduced on it at all.
"""

from __future__ import annotations

import os
import threading

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import AiUsage, User
from app.models.enums import Role, UserStatus
from app.services.ai_usage import AILimitExceeded, enforce_ai_limit, record_ai_usage
from app.utils import new_id


def _teacher(db) -> User:
    user = User(
        id=new_id("u"),
        name="Racing teacher",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        grades=["G7"],
    )
    db.add(user)
    db.commit()
    return user


def _count(db, user: User) -> int:
    return (
        db.query(AiUsage)
        .filter(AiUsage.user_id == user.id, AiUsage.kind == "teacher")
        .count()
    )


def test_asking_in_series_still_stops_at_the_cap(db):
    """The ordinary path, unchanged: the lock must not cost anyone a question."""
    teacher = _teacher(db)
    limit = settings.ai_teacher_hourly_limit

    for _ in range(limit):
        enforce_ai_limit(db, teacher, "teacher")
        record_ai_usage(db, teacher, "teacher")

    assert _count(db, teacher) == limit
    with pytest.raises(AILimitExceeded):
        enforce_ai_limit(db, teacher, "teacher")


def test_the_check_holds_the_account_row(db):
    """Whatever the dialect does with it, the lock has to be asked for.

    On SQLite `FOR UPDATE` is ignored, so this is the only place the request
    itself can be observed — and the request is what Postgres acts on.
    """
    teacher = _teacher(db)
    seen: list[dict] = []
    original = db.get

    def spy(entity, ident, **kw):
        seen.append({"entity": entity, "ident": ident, **kw})
        return original(entity, ident, **kw)

    db.get = spy  # type: ignore[method-assign]
    try:
        enforce_ai_limit(db, teacher, "teacher")
    finally:
        db.get = original  # type: ignore[method-assign]

    assert any(
        call["entity"] is User
        and call["ident"] == teacher.id
        and call.get("with_for_update")
        and call.get("populate_existing")
        for call in seen
    ), "the account row must be taken FOR UPDATE, and re-read once it is held"


# --------------------------------------------------------------------------- #
# Against the database the race actually happens on
# --------------------------------------------------------------------------- #
ON_POSTGRES = "postgresql" in os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not ON_POSTGRES, reason="needs TEST_DATABASE_URL on Postgres")
def test_parallel_questions_cannot_outrun_the_cap(db):
    """Six Sends at once, from one question below the cap.

    Before the lock this inserted six rows against one remaining question. It
    cannot be written on SQLite, whose database-wide write lock serialises the
    transactions for free and hides the defect completely — the same blind spot
    that let the login-counter version of this ship.
    """
    teacher = _teacher(db)
    limit = settings.ai_teacher_hourly_limit
    for _ in range(limit - 1):
        enforce_ai_limit(db, teacher, "teacher")
        record_ai_usage(db, teacher, "teacher")
    assert _count(db, teacher) == limit - 1

    start = threading.Barrier(6)
    refused = []

    def ask() -> None:
        session = SessionLocal()
        try:
            user = session.get(User, teacher.id)
            start.wait(timeout=10)
            try:
                enforce_ai_limit(session, user, "teacher")
            except AILimitExceeded:
                refused.append(1)
                return
            record_ai_usage(session, user, "teacher")
        finally:
            session.close()

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    db.expire_all()
    assert _count(db, teacher) == limit, "the cap is the cap, however they arrive"
    assert len(refused) == 5
