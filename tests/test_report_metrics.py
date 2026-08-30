"""The figures a report is built from.

Two of these pin down claims the old report got wrong: that a fresh upload made
every school look like it had gone backwards, and that fifty lines of ordinary
logins were a security section.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import ChatMessage, Lesson, Progress, School, SecurityLog, User
from app.models.enums import (
    LessonStatus,
    Role,
    SecurityEvent,
    SecurityStatus,
    UserStatus,
    WatchdogStatus,
)
from app.services.report_metrics import (
    QUIET_AFTER_DAYS,
    movement,
    progress_stats,
    quiet_teachers,
    security_anomalies,
)
from app.utils import new_id

NOW = datetime.now(timezone.utc)


def _school(db) -> School:
    school = School(
        id=new_id("sch"), name="Metrics School", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.commit()
    return school


def _teacher(db, school: School, *, name="teacher", age_days=90) -> User:
    user = User(
        id=new_id("u"),
        name=name,
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G7"],
        created_at=NOW - timedelta(days=age_days),
    )
    db.add(user)
    db.commit()
    return user


def _lesson(db, n: int = 1) -> Lesson:
    lesson = Lesson(
        id=new_id("les"),
        title=f"Lesson {n}",
        grade=7,
        subject="STEAM",
        language="en",
        year=2,
        lesson_no=n,
    )
    db.add(lesson)
    db.commit()
    return lesson


def _progress(
    db,
    teacher: User,
    lesson: Lesson,
    *,
    status=LessonStatus.not_started,
    percent=0,
    completed_days_ago=None,
    opened_days_ago=None,
    watchdog=WatchdogStatus.not_opened,
) -> Progress:
    row = Progress(
        id=new_id("p"),
        teacher_id=teacher.id,
        lesson_id=lesson.id,
        status=status,
        percent_complete=percent,
        watchdog=watchdog,
        completed_at=None if completed_days_ago is None else NOW - timedelta(days=completed_days_ago),
        last_opened_at=None if opened_days_ago is None else NOW - timedelta(days=opened_days_ago),
    )
    db.add(row)
    db.commit()
    return row


# --- Completion is three facts, not one average ---------------------------- #


def test_a_fresh_upload_does_not_make_a_school_look_worse(db):
    """The old metric averaged percent_complete over every assignment. Handing a
    teacher forty untouched lessons would halve it overnight; these numbers hold
    still."""
    school = _school(db)
    teacher = _teacher(db, school)
    done = [_lesson(db, n) for n in range(1, 3)]
    for lesson in done:
        _progress(db, teacher, lesson, status=LessonStatus.completed, percent=100)

    before = progress_stats(list(db.scalars(select_progress(db, teacher))))
    assert (before.assigned, before.completed, before.completion_rate) == (2, 2, 100)
    assert before.avg_of_started == 100

    # Now assign eight more that nobody has opened.
    for n in range(3, 11):
        _progress(db, teacher, _lesson(db, n))

    after = progress_stats(list(db.scalars(select_progress(db, teacher))))
    assert after.assigned == 10
    assert after.completed == 2  # unchanged — no work was undone
    assert after.not_started == 8
    # Progress on the lessons actually begun is still 100%.
    assert after.avg_of_started == 100
    # And the rate honestly reflects the bigger pile, without pretending the
    # teacher regressed.
    assert after.completion_rate == 20


def select_progress(db, teacher: User):
    from sqlalchemy import select

    return select(Progress).where(Progress.teacher_id == teacher.id)


def test_stats_of_nothing_report_nothing(db):
    stats = progress_stats([])
    assert (stats.assigned, stats.completed, stats.completion_rate) == (0, 0, 0)
    # No percentage is offered for work that was never begun.
    assert stats.avg_of_started is None


# --- What changed ---------------------------------------------------------- #


def test_completions_and_questions_are_counted_week_over_week(db):
    school = _school(db)
    teacher = _teacher(db, school)
    for days in (1, 3, 6):  # this week
        _progress(
            db, teacher, _lesson(db), status=LessonStatus.completed, completed_days_ago=days
        )
    for days in (8, 12):  # the week before
        _progress(
            db, teacher, _lesson(db), status=LessonStatus.completed, completed_days_ago=days
        )
    asked_about = _lesson(db, 99)
    for days, count in ((2, 4), (9, 1)):
        for _ in range(count):
            db.add(
                ChatMessage(
                    id=new_id("cm"),
                    teacher_id=teacher.id,
                    lesson_id=asked_about.id,
                    role="user",
                    content="how?",
                    created_at=NOW - timedelta(days=days),
                )
            )
    # The assistant's own replies are not questions.
    db.add(
        ChatMessage(
            id=new_id("cm"),
            teacher_id=teacher.id,
            lesson_id=asked_about.id,
            role="assistant",
            content="like this",
            created_at=NOW - timedelta(days=2),
        )
    )
    db.commit()

    moved = movement(db, [teacher.id], school_id=school.id)
    assert (moved.completed.this_week, moved.completed.prior_week) == (3, 2)
    assert (moved.questions.this_week, moved.questions.prior_week) == (4, 1)
    assert "up from 2" in moved.completed.describe()


def test_a_first_week_is_not_described_as_growth(db):
    school = _school(db)
    teacher = _teacher(db, school)
    _progress(db, teacher, _lesson(db), status=LessonStatus.completed, completed_days_ago=2)

    moved = movement(db, [teacher.id], school_id=school.id)
    assert "nothing the week before" in moved.completed.describe()


def test_activity_is_a_current_count_with_no_misleading_comparison(db):
    """last_opened_at is a "last seen" field, not an event log: a teacher active
    in both weeks only appears in the later one. So it is reported as a plain
    count and never as a week-over-week trend."""
    school = _school(db)
    teacher = _teacher(db, school)
    _progress(db, teacher, _lesson(db), opened_days_ago=2)
    _progress(db, teacher, _lesson(db), opened_days_ago=3)
    _progress(db, teacher, _lesson(db), opened_days_ago=20)

    moved = movement(db, [teacher.id], school_id=school.id)
    assert moved.teachers_active == 1
    assert moved.lessons_touched == 2
    assert not hasattr(moved, "opened_prior_week")


# --- Who needs a nudge ----------------------------------------------------- #


def test_quiet_teachers_are_found_and_ordered_worst_first(db):
    school = _school(db)
    busy = _teacher(db, school, name="Busy")
    slipping = _teacher(db, school, name="Slipping")
    never = _teacher(db, school, name="Never")
    _progress(db, busy, _lesson(db), opened_days_ago=1)
    _progress(db, slipping, _lesson(db), opened_days_ago=QUIET_AFTER_DAYS + 10)
    _progress(db, never, _lesson(db))  # assigned, never opened

    quiet = quiet_teachers(db, [busy, slipping, never])

    assert [q.name for q in quiet] == ["Never", "Slipping"]
    assert quiet[0].days_quiet is None
    assert quiet[1].days_quiet >= QUIET_AFTER_DAYS
    assert "has never opened a lesson" in quiet[0].describe()


def test_a_new_hire_is_not_counted_as_quiet(db):
    """Assigning lessons to someone who joined yesterday must not report them as
    behind."""
    school = _school(db)
    fresh = _teacher(db, school, name="Fresh", age_days=2)
    _progress(db, fresh, _lesson(db))

    assert quiet_teachers(db, [fresh]) == []


# --- Security, grouped ----------------------------------------------------- #


def test_only_anomalies_survive_and_they_are_grouped(db):
    school = _school(db)
    teacher = _teacher(db, school, name="Rita")
    for _ in range(3):
        db.add(
            SecurityLog(
                id=new_id("sl"),
                user_id=teacher.id,
                user_name="Rita",
                role=Role.teacher,
                school_id=school.id,
                event=SecurityEvent.blocked_second_device,
                status=SecurityStatus.blocked,
                timestamp=NOW - timedelta(days=1),
            )
        )
    for _ in range(20):  # ordinary logins used to bury the three above
        db.add(
            SecurityLog(
                id=new_id("sl"),
                user_id=teacher.id,
                user_name="Rita",
                role=Role.teacher,
                school_id=school.id,
                event=SecurityEvent.normal_login,
                status=SecurityStatus.ok,
                timestamp=NOW - timedelta(days=1),
            )
        )
    db.commit()

    anomalies = security_anomalies(db, school_id=school.id)

    assert len(anomalies) == 1
    assert anomalies[0].count == 3
    assert anomalies[0].status == "blocked"
    assert "3 times" in anomalies[0].describe()


def test_old_alerts_fall_out_of_the_window(db):
    school = _school(db)
    teacher = _teacher(db, school, name="Ancient")
    db.add(
        SecurityLog(
            id=new_id("sl"),
            user_id=teacher.id,
            user_name="Ancient",
            role=Role.teacher,
            school_id=school.id,
            event=SecurityEvent.foreign_device,
            status=SecurityStatus.warning,
            timestamp=NOW - timedelta(days=90),
        )
    )
    db.commit()

    assert security_anomalies(db, school_id=school.id, days=30) == []
