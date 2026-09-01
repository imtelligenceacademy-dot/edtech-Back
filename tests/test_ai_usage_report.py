"""The per-teacher AI usage report behind the super-admin's usage screen.

The screen's whole claim is that its numbers are checkable, so what is asserted
here is that each window counts exactly what its name says: an interaction 8
days old is not in "last 7 days", one 25 hours old is not in "last 24 hours",
and a teacher who has never asked anything still appears with zeroes rather
than being dropped from the list.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models import AiUsage, School, User
from app.models.enums import Role, UserStatus
from app.routers.ai import usage_by_teacher
from app.services.ai_usage import teacher_usage_report
from app.utils import new_id


def _user(db, role: Role, name: str, school_id: str | None = None) -> User:
    user = User(
        id=new_id("u"),
        name=name,
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=role,
        status=UserStatus.active,
        grades=["G7"],
        school_id=school_id,
    )
    db.add(user)
    db.commit()
    return user


def _school(db, name: str) -> School:
    school = School(id=new_id("sch"), name=name, country="LB", city="Beirut")
    db.add(school)
    db.commit()
    return school


def _usage(db, user: User, ago: timedelta, kind: str = "teacher") -> AiUsage:
    row = AiUsage(
        id=new_id("aiu"),
        user_id=user.id,
        school_id=user.school_id,
        role=user.role,
        kind=kind,
        created_at=datetime.now(timezone.utc) - ago,
    )
    db.add(row)
    db.commit()
    return row


def _row_for(report: dict, teacher: User) -> dict:
    return next(r for r in report["teachers"] if r["teacher_id"] == teacher.id)


def test_each_window_counts_only_what_it_names(db):
    school = _school(db, "Rolling windows")
    teacher = _user(db, Role.teacher, "Windowed", school.id)

    # One interaction per window, each placed just inside the boundary it tests
    # and outside the tighter one below it.
    _usage(db, teacher, timedelta(minutes=30))   # this hour, so also 24h/7d/30d
    _usage(db, teacher, timedelta(hours=5))      # last 24h, not last hour
    _usage(db, teacher, timedelta(days=3))       # last 7 days, not last 24h
    _usage(db, teacher, timedelta(days=10))      # the week before last
    _usage(db, teacher, timedelta(days=25))      # last 30 days only
    _usage(db, teacher, timedelta(days=200))     # all-time only

    row = _row_for(teacher_usage_report(db), teacher)

    assert row["last_hour"] == 1
    assert row["last24h"] == 2
    assert row["last7"] == 3
    assert row["prev7"] == 1, "days 7-14 back, not a running total"
    assert row["last30"] == 5
    assert row["total"] == 6, "all time includes the row outside the 30-day window"


def test_teacher_who_never_asked_is_listed_with_zeroes(db):
    teacher = _user(db, Role.teacher, "Never asked")

    report = teacher_usage_report(db)
    row = _row_for(report, teacher)

    assert row["total"] == 0
    assert row["last7"] == 0
    assert row["last_used_at"] is None
    assert row["first_used_at"] is None
    # A silent teacher is the finding, so the strip is still full-width zeroes.
    assert len(row["daily"]) == report["daily_days"]
    assert all(day["count"] == 0 for day in row["daily"])


def test_first_and_last_use_are_the_real_timestamps(db):
    teacher = _user(db, Role.teacher, "Timestamped")
    oldest = _usage(db, teacher, timedelta(days=40))
    newest = _usage(db, teacher, timedelta(hours=2))
    _usage(db, teacher, timedelta(days=3))

    row = _row_for(teacher_usage_report(db), teacher)

    assert row["first_used_at"].timestamp() == pytest.approx(
        oldest.created_at.timestamp(), abs=1
    )
    assert row["last_used_at"].timestamp() == pytest.approx(
        newest.created_at.timestamp(), abs=1
    )


def test_active_days_counts_days_not_interactions(db):
    teacher = _user(db, Role.teacher, "Bursty")
    # Four questions across two days is two active days, not four.
    _usage(db, teacher, timedelta(hours=1))
    _usage(db, teacher, timedelta(hours=2))
    _usage(db, teacher, timedelta(days=2, hours=1))
    _usage(db, teacher, timedelta(days=2, hours=3))

    row = _row_for(teacher_usage_report(db), teacher)

    assert row["last30"] == 4
    assert row["active_days30"] == 2


def test_daily_strip_buckets_by_day_and_sums_to_the_window(db):
    teacher = _user(db, Role.teacher, "Stripey")
    _usage(db, teacher, timedelta(hours=1))
    _usage(db, teacher, timedelta(hours=3))
    _usage(db, teacher, timedelta(days=4))

    report = teacher_usage_report(db)
    row = _row_for(report, teacher)

    assert len(row["daily"]) == report["daily_days"]
    # Ordered oldest-first, so the strip reads left to right as time does.
    dates = [day["date"] for day in row["daily"]]
    assert dates == sorted(dates)
    assert sum(day["count"] for day in row["daily"]) == 3


def test_only_teachers_appear(db):
    school = _school(db, "Mixed roles")
    admin = _user(db, Role.school_admin, "An admin", school.id)
    teacher = _user(db, Role.teacher, "A teacher", school.id)
    _usage(db, admin, timedelta(hours=1), kind="admin")
    _usage(db, teacher, timedelta(hours=1))

    ids = {r["teacher_id"] for r in teacher_usage_report(db)["teachers"]}

    assert teacher.id in ids
    assert admin.id not in ids, "the school-admin assistant is a different screen"


def test_school_name_is_resolved_for_the_row(db):
    school = _school(db, "Antonine Sisters")
    teacher = _user(db, Role.teacher, "Placed", school.id)

    row = _row_for(teacher_usage_report(db), teacher)

    assert row["school_name"] == "Antonine Sisters"


def test_report_states_the_boundaries_it_counted_from(db):
    report = teacher_usage_report(db)

    # The screen labels its columns from these, so they have to be present and
    # ordered; a window whose edge the reader cannot see is not checkable.
    assert report["window_start"] < report["prev_week_start"]
    assert report["prev_week_start"] < report["week_start"]
    assert report["week_start"] < report["day_start"]
    assert report["day_start"] < report["hour_start"]
    assert report["hour_start"] < report["generated_at"]
    assert report["timezone"] in {"Asia/Beirut", "UTC"}
    assert report["daily_limit"] > 0 and report["hourly_limit"] > 0


def test_endpoint_is_super_admin_only(db):
    """Named teachers across every school are the platform owner's view alone.

    The guard is a dependency, so it is exercised directly — the endpoint
    function itself never sees an unauthorised caller.
    """
    school = _school(db, "Scoping")
    guard = _role_guard()

    for role in (Role.teacher, Role.school_admin):
        actor = _user(db, role, f"Not allowed {role.value}", school.id)
        with pytest.raises(HTTPException) as exc:
            guard(user=actor)
        assert exc.value.status_code == 403

    owner = _user(db, Role.super_admin, "Owner")
    assert guard(user=owner) is owner
    assert usage_by_teacher(db=db, current=owner).teachers is not None


def _role_guard():
    """The exact dependency `/api/ai/usage/teachers` is declared with."""
    from app.deps import require_roles

    return require_roles(Role.super_admin)


def test_serialized_keys_are_the_ones_the_frontend_asks_for(db):
    """The wire format, pinned.

    `last24h` is the reason this test exists: the camelCase generator reads the
    trailing "h" as a new word after the digits and emits `last24H`, which the
    frontend reads as undefined. Nothing else catches that — the field name is
    right in Python and right in TypeScript, and only the JSON between them is
    wrong.
    """
    _user(db, Role.teacher, "Serialized")
    payload = usage_by_teacher(
        db=db, current=_user(db, Role.super_admin, "Owner")
    ).model_dump(by_alias=True)

    assert {
        "generatedAt",
        "timezone",
        "todayStart",
        "hourStart",
        "dayStart",
        "weekStart",
        "prevWeekStart",
        "windowStart",
        "dailyDays",
        "hourlyLimit",
        "dailyLimit",
        "teachers",
    } <= set(payload)

    row = payload["teachers"][0]
    for key in (
        "teacherId",
        "schoolName",
        "lastHour",
        "last24h",
        "last7",
        "prev7",
        "last30",
        "activeDays30",
        "firstUsedAt",
        "lastUsedAt",
        "hourlyUsed",
        "dailyUsed",
    ):
        assert key in row, f"{key} is missing from the serialized row"

    assert "last24H" not in row
    assert set(row["daily"][0]) == {"date", "count"}
