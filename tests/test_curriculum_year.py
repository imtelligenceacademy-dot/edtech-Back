"""Moving a school from one curriculum year to the next.

A school runs Year 1 in its first year with us and Year 2 after that, and the
assignment rule reads that number: a teacher receives the lessons for their
grade, in their language, *for their school's current year*.

Changing the number used to change nothing else. The rule only ever ran when a
teacher was created or edited, so a school promoted to Year 2 kept the whole of
Year 1 and received none of Year 2 — while the field the super-admin had just
set said "Determines which year's lessons this school's teachers receive". The
repair was to open and re-save every teacher on the account list, one at a time,
and nothing on the screen said so.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import Lesson, LessonAssignment, Progress, School, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.routers import schools as schools_router
from app.schemas.school import SchoolUpdate
from app.services.auto_assign import sync_teacher_assignments
from app.utils import new_id


def _boss(db) -> User:
    user = User(
        id=new_id("u"),
        name="owner",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.super_admin,
        status=UserStatus.active,
        grades=[],
    )
    db.add(user)
    db.flush()
    return user


def _lesson(db, *, year: int, lesson_no: int, grade: int = 7) -> Lesson:
    lesson = Lesson(
        id=new_id("les"),
        title=f"Grade {grade} python lesson {lesson_no:02d} (year {year})",
        grade=grade,
        subject="STEAM",
        language="en",
        year=year,
        course="python",
        lesson_no=lesson_no,
    )
    db.add(lesson)
    db.flush()
    return lesson


@pytest.fixture()
def first_year(db):
    """A school still on Year 1, with one teacher holding the Year 1 lessons.

    Year 2's lessons exist on the platform — they are curriculum-level, shared
    by every school — but this teacher does not hold them yet.
    """
    school = School(id=new_id("sch"), name="Balamand", program_year=1)
    db.add(school)
    db.flush()
    teacher = User(
        id=new_id("u"),
        name="T",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G7"],
        language="en",
    )
    db.add(teacher)
    db.flush()

    year_one = [_lesson(db, year=1, lesson_no=n) for n in (1, 2)]
    year_two = [_lesson(db, year=2, lesson_no=n) for n in (1, 2)]
    sync_teacher_assignments(db, teacher)
    db.commit()

    return {
        "school": school,
        "teacher": teacher,
        "year_one": year_one,
        "year_two": year_two,
        "admin": _boss(db),
    }


def _assigned(db, teacher) -> set[str]:
    return {
        a.lesson_id
        for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
        )
    }


def _promote(db, world, year: int = 2):
    return schools_router.update_school(
        world["school"].id, SchoolUpdate(program_year=year), db, world["admin"]
    )


def test_promoting_a_school_hands_its_teachers_the_new_year(db, first_year):
    teacher = first_year["teacher"]
    year_one = {l.id for l in first_year["year_one"]}
    year_two = {l.id for l in first_year["year_two"]}

    # Lessons are curriculum-level and shared, and other tests commit their own,
    # so these say what happened to *these* lessons rather than counting rows.
    before = _assigned(db, teacher)
    assert year_one <= before and not (year_two & before), "precondition: year 1 only"

    _promote(db, first_year)
    db.commit()

    after = _assigned(db, teacher)
    assert year_two <= after, "year 2 should arrive"
    assert not (year_one & after), "untouched year 1 should go"


def test_a_promotion_keeps_a_lesson_a_class_has_already_started(db, first_year):
    """The smart-strip rule, unchanged: work is never thrown away.

    A teacher part-way through a Year 1 lesson keeps it, so a mid-year promotion
    does not delete a class's progress. Only the untouched remainder goes.
    """
    teacher = first_year["teacher"]
    started, untouched = first_year["year_one"]
    row = db.scalar(
        select(Progress).where(
            Progress.teacher_id == teacher.id, Progress.lesson_id == started.id
        )
    )
    row.status = LessonStatus.in_progress
    row.percent_complete = 40
    row.last_opened_at = datetime.now(timezone.utc)
    db.flush()

    _promote(db, first_year)
    db.commit()

    assigned = _assigned(db, teacher)
    assert started.id in assigned, "a lesson a class has opened is kept"
    assert untouched.id not in assigned, "the untouched rest of year 1 goes"
    assert {l.id for l in first_year["year_two"]} <= assigned


def test_renaming_a_school_leaves_the_curriculum_alone(db, first_year):
    """The guard on the check.

    Re-running the rule on every teacher is not free, and it strips untouched
    assignments — so it must fire on a real change of year and on nothing else.
    """
    teacher = first_year["teacher"]
    before = _assigned(db, teacher)

    schools_router.update_school(
        first_year["school"].id,
        SchoolUpdate(name="Balamand Main Campus", city="Tripoli"),
        db,
        first_year["admin"],
    )
    db.commit()

    assert _assigned(db, teacher) == before
    assert first_year["school"].name == "Balamand Main Campus"


def test_setting_the_same_year_again_changes_nothing(db, first_year):
    teacher = first_year["teacher"]
    before = _assigned(db, teacher)

    _promote(db, first_year, year=1)
    db.commit()

    assert _assigned(db, teacher) == before


def test_promoting_one_school_does_not_touch_another(db, first_year):
    """Lessons are curriculum-level and shared, so the rule has to be applied
    per school or one admin's promotion would re-key somebody else's teachers."""
    other_school = School(id=new_id("sch"), name="Elsewhere", program_year=1)
    db.add(other_school)
    db.flush()
    other_teacher = User(
        id=new_id("u"),
        name="Other",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=other_school.id,
        grades=["G7"],
        language="en",
    )
    db.add(other_teacher)
    db.flush()
    sync_teacher_assignments(db, other_teacher)
    db.flush()
    before = _assigned(db, other_teacher)
    assert {l.id for l in first_year["year_one"]} <= before, "precondition: year 1"

    _promote(db, first_year)
    db.commit()

    assert _assigned(db, other_teacher) == before, (
        "the other school is still on year 1 and keeps year 1"
    )
