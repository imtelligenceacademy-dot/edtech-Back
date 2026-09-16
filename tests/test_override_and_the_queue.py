"""An admin's override is an extra door, not a place in the queue.

It must not move the sequence in either direction, and only one of those halves
was implemented. Closing the gate with an override took access away — reopening
a lesson finished last month locked the lesson the teacher was forty per cent
through — and that was fixed by having an override leave the gate untouched.

Untouched is not neutral. An override on a lesson that is *not* finished then
inherited the previous lesson's open gate, so the lesson after it opened on a
countdown belonging to a lesson two places back, without the overridden one ever
being completed. The sequential unlock is the product's central rule; this let
a class walk straight past it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models import Lesson, LessonAssignment, Progress, School, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.services.lesson_access import compute_access
from app.utils import new_id


@pytest.fixture()
def track(db):
    """One teacher, one class, three lessons in sequence."""
    school = School(
        id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.flush()
    teacher = User(
        id=new_id("u"),
        name="Teacher",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G7"],
    )
    db.add(teacher)
    lessons = []
    for n in (1, 2, 3):
        lesson = Lesson(
            id=new_id("les"),
            title=f"Grade 7 lesson {n:02d}",
            grade=7,
            subject="STEAM",
            language="en",
            year=2,
            lesson_no=n,
        )
        db.add(lesson)
        db.flush()
        db.add(
            LessonAssignment(
                id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
            )
        )
        lessons.append(lesson)
    db.commit()
    return teacher, lessons


def _progress(db, teacher, lesson, *, status, completed_at=None, override=False):
    db.add(
        Progress(
            id=new_id("p"),
            teacher_id=teacher.id,
            lesson_id=lesson.id,
            section="",
            status=status,
            completed_at=completed_at,
            unlocked_override=override,
        )
    )
    db.commit()


def _status(access, lesson):
    return access[(lesson.id, "")].status


def test_an_unlocked_lesson_still_blocks_the_one_after_it(db, track):
    """The hole: L1 finished long ago, L2 unlocked early and never completed.

    L3 must not open on L1's countdown — L2 is unfinished and is what stands
    between them.
    """
    teacher, (l1, l2, l3) = track
    long_ago = datetime.now(timezone.utc) - timedelta(
        days=settings.lesson_unlock_wait_days + 5
    )
    _progress(db, teacher, l1, status=LessonStatus.completed, completed_at=long_ago)
    _progress(db, teacher, l2, status=LessonStatus.in_progress, override=True)

    access = compute_access(db, teacher)

    assert _status(access, l1) == "completed"
    assert _status(access, l2) == "available", "the override is still an open door"
    assert _status(access, l3) != "available"


def test_a_waiting_lesson_is_not_counting_down_a_different_lesson(db, track):
    """The timestamp shown belonged to the lesson two places back."""
    teacher, (l1, l2, l3) = track
    just_now = datetime.now(timezone.utc)
    _progress(db, teacher, l1, status=LessonStatus.completed, completed_at=just_now)
    _progress(db, teacher, l2, status=LessonStatus.in_progress, override=True)

    access = compute_access(db, teacher)
    third = access[(l3.id, "")]

    assert third.status != "available"
    # L1's unlock time must not be presented as what L3 is waiting for; L3 is
    # waiting on L2 being finished, which has no date yet.
    assert third.available_at is None


def test_unlocking_the_very_first_lesson_does_not_open_the_second(db, track):
    """The degenerate case: the gate starts open, so nothing had to be inherited
    for the second lesson to fall through."""
    teacher, (l1, l2, _l3) = track
    _progress(db, teacher, l1, status=LessonStatus.in_progress, override=True)

    access = compute_access(db, teacher)

    assert _status(access, l1) == "available"
    assert _status(access, l2) != "available"


def test_reopening_a_finished_lesson_still_leaves_her_current_one_open(db, track):
    """The half that was already right, and must stay right.

    This is the complaint the override rule was written for: reopening a lesson
    finished last month must not lock the lesson she is part-way through.
    """
    teacher, (l1, l2, l3) = track
    last_month = datetime.now(timezone.utc) - timedelta(days=30)
    _progress(
        db,
        teacher,
        l1,
        status=LessonStatus.completed,
        completed_at=last_month,
        override=True,
    )
    _progress(db, teacher, l2, status=LessonStatus.in_progress)

    access = compute_access(db, teacher)

    assert _status(access, l1) == "available", "reopened, so she can open it again"
    assert _status(access, l2) == "available", "and the lesson she is in stays open"
    assert _status(access, l3) != "available"


def test_an_ordinary_sequence_is_unchanged(db, track):
    """No overrides anywhere: the plain rule still holds."""
    teacher, (l1, l2, l3) = track
    long_ago = datetime.now(timezone.utc) - timedelta(
        days=settings.lesson_unlock_wait_days + 5
    )
    _progress(db, teacher, l1, status=LessonStatus.completed, completed_at=long_ago)

    access = compute_access(db, teacher)

    assert _status(access, l1) == "completed"
    assert _status(access, l2) == "available"
    assert _status(access, l3) != "available"
