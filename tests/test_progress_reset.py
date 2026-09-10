"""A super-admin putting a teacher's progress back to never-opened.

The case this exists for: a school trains its teachers on real lessons before
term, every one of those is recorded as completed, and on the first day of
teaching the whole curriculum reads as already done. The unlock override cannot
fix that — it reopens a finished lesson while still recording it as finished.

What is asserted here is mostly what a reset must NOT touch. It is destructive
and cannot be undone, so the blast radius is the thing worth pinning down.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.models import ChatMessage, Lesson, LessonAssignment, Progress, School, User
from app.models.enums import LessonStatus, Role, SecurityEvent, UserStatus, WatchdogStatus
from app.models import SecurityLog
from app.routers import lessons as lessons_router
from app.schemas.lesson import ProgressResetRequest
from app.services.lesson_access import compute_access
from app.services.sections import ensure_progress_for_lessons
from app.utils import new_id
from sqlalchemy import select


class _Request:
    """FastAPI's Request, as much of it as the audit line reads."""

    client = None
    headers: dict[str, str] = {}


def _admin(db) -> User:
    user = User(
        id=new_id("u"), name="Owner", email=f"{new_id('a')}@x.com", password_hash="x",
        role=Role.super_admin, status=UserStatus.active, grades=[],
    )
    db.add(user)
    db.commit()
    return user


def _teacher(db, school, grades, sections=None) -> User:
    user = User(
        id=new_id("u"), name="Teacher", email=f"{new_id('t')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=grades, sections=sections or {}, language="en",
    )
    db.add(user)
    db.flush()
    return user


def _lessons(db, teacher, grade, count) -> list[Lesson]:
    out = []
    for n in range(1, count + 1):
        lesson = Lesson(
            id=new_id("les"), title=f"Grade {grade} python lesson 0{n}", grade=grade,
            subject="STEAM", language="en", year=2, course="python", lesson_no=n,
        )
        db.add(lesson)
        db.flush()
        db.add(LessonAssignment(
            id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
        ))
        out.append(lesson)
    return out


@pytest.fixture()
def trained(db):
    """A teacher who walked the whole curriculum during training: everything
    marked complete, in both of her classes."""
    school = School(id=new_id("sch"), name="Trained", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G6"], {"G6": ["A", "B"]})
    lessons = _lessons(db, teacher, 6, 3)
    ensure_progress_for_lessons(db, teacher, lessons)
    db.flush()

    for row in db.scalars(select(Progress).where(Progress.teacher_id == teacher.id)):
        row.status = LessonStatus.completed
        row.percent_complete = 100
        row.last_slide = 11
        row.slide_total = 11
        row.completed_at = datetime.now(timezone.utc)
        row.last_opened_at = datetime.now(timezone.utc)
    db.commit()
    return {"admin": _admin(db), "teacher": teacher, "lessons": lessons}


def _reset(db, world, **kwargs):
    return lessons_router.reset_teacher_progress(
        world["teacher"].id,
        ProgressResetRequest(**kwargs),
        _Request(),
        db=db,
        admin=world["admin"],
    )


def _rows(db, teacher):
    return list(db.scalars(select(Progress).where(Progress.teacher_id == teacher.id)))


# --------------------------------------------------------------------------- #
# The case it was built for
# --------------------------------------------------------------------------- #
def test_resetting_everything_returns_the_whole_curriculum_to_untouched(db, trained):
    result = _reset(db, trained)

    # Three lessons across two classes.
    assert result.lessons == 6
    assert result.completed_cleared == 6
    assert result.classes == 2

    for row in _rows(db, trained["teacher"]):
        assert row.status == LessonStatus.not_started
        assert row.percent_complete == 0
        assert row.last_slide is None
        assert row.completed_at is None
        assert row.last_opened_at is None
        assert row.watchdog == WatchdogStatus.not_opened


def test_the_sequence_starts_again_from_the_first_lesson(db, trained):
    """The point of the reset. Before it the curriculum is finished and locked;
    after it the teacher is back at lesson one with the rest ahead of her."""
    teacher, (l1, l2, l3) = trained["teacher"], trained["lessons"]
    _reset(db, trained)

    access = compute_access(db, teacher)
    for section in ("A", "B"):
        assert access[(l1.id, section)].status == "available"
        assert access[(l2.id, section)].status == "locked"
        assert access[(l3.id, section)].status == "locked"


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def test_one_lesson_in_one_class_leaves_everything_else_alone(db, trained):
    """The mistake case: a teacher marks one lesson complete in one room."""
    teacher, (l1, _, _) = trained["teacher"], trained["lessons"]

    result = _reset(db, trained, lessonId=l1.id, section="A")

    assert result.lessons == 1
    rows = {(r.lesson_id, r.section): r for r in _rows(db, teacher)}
    assert rows[(l1.id, "A")].status == LessonStatus.not_started
    assert rows[(l1.id, "B")].status == LessonStatus.completed, "6B was not in the room"


def test_one_lesson_across_every_class(db, trained):
    teacher, (l1, l2, _) = trained["teacher"], trained["lessons"]

    result = _reset(db, trained, lessonId=l1.id)

    assert result.lessons == 2
    rows = {(r.lesson_id, r.section): r for r in _rows(db, teacher)}
    assert rows[(l1.id, "A")].status == LessonStatus.not_started
    assert rows[(l1.id, "B")].status == LessonStatus.not_started
    assert rows[(l2.id, "A")].status == LessonStatus.completed


def test_one_class_across_every_lesson(db, trained):
    teacher = trained["teacher"]

    result = _reset(db, trained, section="B")

    assert result.lessons == 3
    by_section = {}
    for row in _rows(db, teacher):
        by_section.setdefault(row.section, set()).add(row.status)
    assert by_section["B"] == {LessonStatus.not_started}
    assert by_section["A"] == {LessonStatus.completed}


# --------------------------------------------------------------------------- #
# What it must not touch
# --------------------------------------------------------------------------- #
def test_the_teacher_keeps_the_lessons_she_had(db, trained):
    """Resetting progress is not unassigning. She should still have them all,
    back at the beginning."""
    teacher = trained["teacher"]
    before = db.scalars(
        select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
    ).all()

    _reset(db, trained)

    after = db.scalars(
        select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
    ).all()
    assert len(after) == len(before) == 3
    assert len(_rows(db, teacher)) == 6, "the progress rows stay, they are just cleared"


def test_her_conversations_survive(db, trained):
    """Her questions about a lesson are her own notes on the material, not a
    record of progress, and she may well want them when she teaches it for real."""
    teacher, (l1, _, _) = trained["teacher"], trained["lessons"]
    db.add(ChatMessage(
        id=new_id("msg"), teacher_id=teacher.id, lesson_id=l1.id, section="A",
        role="user", content="what usually confuses students here?",
    ))
    db.commit()

    _reset(db, trained)

    kept = db.scalars(
        select(ChatMessage).where(ChatMessage.teacher_id == teacher.id)
    ).all()
    assert len(kept) == 1


def test_an_unlock_override_goes_with_the_progress(db, trained):
    """It exists to reopen a finished lesson. Once nothing is finished there is
    nothing to reopen, and leaving it set would exempt that lesson from the
    sequence for good."""
    teacher, (l1, _, _) = trained["teacher"], trained["lessons"]
    for row in _rows(db, teacher):
        if row.lesson_id == l1.id:
            row.unlocked_override = True
    db.commit()

    result = _reset(db, trained)

    assert result.overrides_cleared == 2
    assert all(not r.unlocked_override for r in _rows(db, teacher))


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def test_it_is_written_down(db, trained):
    """Destructive and unreconstructable, so the log line is the only remaining
    account of what the teacher had."""
    _reset(db, trained)

    # Scoped to this test's admin: the log is shared across the suite, and the
    # question is what THIS reset wrote.
    logged = db.scalars(
        select(SecurityLog).where(
            SecurityLog.event == SecurityEvent.progress_reset,
            SecurityLog.user_id == trained["admin"].id,
        )
    ).all()
    assert len(logged) == 1
    assert "Teacher" in logged[0].detail, "names who lost the progress"
    assert "6 progress record" in logged[0].detail, "and how much of it"


def test_running_it_twice_is_harmless(db, trained):
    first = _reset(db, trained)
    second = _reset(db, trained)

    assert first.completed_cleared == 6
    assert second.lessons == 6, "the rows are still there"
    assert second.completed_cleared == 0, "and there was nothing left to clear"


def test_a_class_that_is_not_this_teacher_s_is_refused(db, trained):
    with pytest.raises(HTTPException) as raised:
        _reset(db, trained, section="Z")
    assert raised.value.status_code == 400


def test_an_unknown_lesson_is_refused(db, trained):
    with pytest.raises(HTTPException) as raised:
        _reset(db, trained, lessonId="les_nope")
    assert raised.value.status_code == 404
