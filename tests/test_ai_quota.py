"""What a teacher has left, and when it frees up.

The limits existed before this and were invisible until they bit — a teacher's
first notice was "you've reached the hourly limit", mid-lesson. These assert the
numbers reported ahead of time match the ones actually enforced, because a
remaining count that disagrees with the enforcement is worse than none.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models import AiUsage, User
from app.models.enums import Role, UserStatus
from app.services.ai_usage import AILimitExceeded, enforce_ai_limit, quota_for
from app.utils import new_id


def _teacher(db) -> User:
    user = User(
        id=new_id("u"),
        name="Quota teacher",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        grades=["G7"],
    )
    db.add(user)
    db.commit()
    return user


def _use(db, user: User, ago: timedelta, kind: str = "teacher") -> None:
    db.add(
        AiUsage(
            id=new_id("aiu"),
            user_id=user.id,
            school_id=user.school_id,
            role=user.role,
            kind=kind,
            created_at=datetime.now(timezone.utc) - ago,
        )
    )
    db.commit()


def test_untouched_quota_reports_the_full_allowance(db):
    q = quota_for(db, _teacher(db))

    assert q["hourly_used"] == 0
    assert q["hourly_remaining"] == settings.ai_teacher_hourly_limit
    assert q["daily_remaining"] == settings.ai_teacher_daily_limit
    # Nothing to wait for while there is headroom.
    assert q["hourly_resets_at"] is None
    assert q["daily_resets_at"] is None


def test_each_window_counts_only_what_falls_inside_it(db):
    teacher = _teacher(db)
    _use(db, teacher, timedelta(minutes=10))   # this hour and this day
    _use(db, teacher, timedelta(minutes=50))   # this hour and this day
    _use(db, teacher, timedelta(hours=5))      # this day only
    _use(db, teacher, timedelta(days=3))       # neither

    q = quota_for(db, teacher)

    assert q["hourly_used"] == 2
    assert q["daily_used"] == 3


def test_reported_remaining_matches_what_enforcement_actually_allows(db):
    """The number shown and the number enforced come from the same counting."""
    teacher = _teacher(db)
    for _ in range(settings.ai_teacher_hourly_limit - 1):
        _use(db, teacher, timedelta(minutes=5))

    assert quota_for(db, teacher)["hourly_remaining"] == 1
    enforce_ai_limit(db, teacher, "teacher")  # the last one is allowed

    _use(db, teacher, timedelta(seconds=1))

    assert quota_for(db, teacher)["hourly_remaining"] == 0
    with pytest.raises(AILimitExceeded):
        enforce_ai_limit(db, teacher, "teacher")


def test_reset_time_is_when_the_oldest_request_ages_out(db):
    """The window rolls, so nothing resets on the hour. The next slot opens
    exactly one hour after the oldest request still inside it."""
    teacher = _teacher(db)
    oldest_ago = timedelta(minutes=59)
    _use(db, teacher, oldest_ago)
    for _ in range(settings.ai_teacher_hourly_limit - 1):
        _use(db, teacher, timedelta(minutes=1))

    q = quota_for(db, teacher)

    assert q["hourly_remaining"] == 0
    expected = datetime.now(timezone.utc) - oldest_ago + timedelta(hours=1)
    assert abs((q["hourly_resets_at"] - expected).total_seconds()) < 2


def test_admin_assistant_is_counted_separately_from_the_teacher_one(db):
    teacher = _teacher(db)
    _use(db, teacher, timedelta(minutes=5), kind="admin")

    assert quota_for(db, teacher, "teacher")["hourly_used"] == 0
    assert quota_for(db, teacher, "admin")["hourly_used"] == 1


def test_a_limit_of_zero_reports_no_limit_rather_than_none_left(db, monkeypatch):
    """0 disables the limit in `enforce_ai_limit`. Reporting it as "0 remaining"
    would tell a teacher they are blocked when they are not."""
    teacher = _teacher(db)
    monkeypatch.setattr(settings, "ai_teacher_hourly_limit", 0)
    _use(db, teacher, timedelta(minutes=5))

    q = quota_for(db, teacher)

    assert q["hourly_remaining"] is None
    assert q["hourly_used"] == 1
    enforce_ai_limit(db, teacher, "teacher")  # and it really is not enforced
