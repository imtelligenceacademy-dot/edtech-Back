"""Assigning a term's worth of lessons in one edit instead of forty.

Access Control used to answer for exactly one lesson at a time. These cover the
rectangle version: these lessons, these teachers, added or removed together.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models import Lesson, LessonAssignment, Progress, School, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.routers.lessons import bulk_assignments, preview_bulk_assignments
from app.schemas.lesson import BulkAssignment
from app.utils import new_id


def _school(db, name: str = "Bulk School") -> School:
    school = School(
        id=new_id("sch"), name=name, country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.commit()
    return school


def _teacher(db, school: School, *, name: str = "teacher", active: bool = True) -> User:
    user = User(
        id=new_id("u"),
        name=name,
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active if active else UserStatus.suspended,
        school_id=school.id,
        grades=["G7"],
    )
    db.add(user)
    db.commit()
    return user


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
    db.commit()
    return user


def _lessons(db, count: int) -> list[Lesson]:
    made = []
    for n in range(1, count + 1):
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
        made.append(lesson)
    db.commit()
    return made


def _assign(db, lesson: Lesson, teacher: User, *, status_: LessonStatus | None = None) -> None:
    db.add(
        LessonAssignment(
            id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
        )
    )
    db.add(
        Progress(
            id=new_id("p"),
            teacher_id=teacher.id,
            lesson_id=lesson.id,
            status=status_ or LessonStatus.not_started,
        )
    )
    db.commit()


# --- Applying one edit across many lessons --------------------------------- #


def test_one_call_hands_every_selected_lesson_to_the_teachers(db):
    school = _school(db)
    claudia = _teacher(db, school, name="Claudia")
    maroun = _teacher(db, school, name="Maroun")
    lessons = _lessons(db, 12)

    result = bulk_assignments(
        payload=BulkAssignment(
            school_id=school.id,
            lesson_ids=[l.id for l in lessons],
            add_teacher_ids=[claudia.id, maroun.id],
        ),
        db=db,
        _=_boss(db),
    )

    assert result.lessons_touched == 12
    assert result.assignments_added == 24
    assert result.assignments_removed == 0
    assert (
        db.query(LessonAssignment)
        .filter(LessonAssignment.teacher_id == claudia.id)
        .count()
        == 12
    )
    # Every new assignment gets its progress row seeded, as the single-lesson
    # path does.
    assert db.query(Progress).filter(Progress.teacher_id == maroun.id).count() == 12
    # The caller gets the affected lessons back, so the page needn't refetch 400.
    assert len(result.lessons) == 12
    assert all(claudia.id in l.assigned_teacher_ids for l in result.lessons)


def test_adding_leaves_other_teachers_on_those_lessons_alone(db):
    """The edit is add/remove, not "here is the new set" — nobody is stripped
    just because they weren't named."""
    school = _school(db)
    incumbent = _teacher(db, school, name="Incumbent")
    newcomer = _teacher(db, school, name="Newcomer")
    lessons = _lessons(db, 3)
    for lesson in lessons:
        _assign(db, lesson, incumbent)

    bulk_assignments(
        payload=BulkAssignment(
            school_id=school.id,
            lesson_ids=[l.id for l in lessons],
            add_teacher_ids=[newcomer.id],
        ),
        db=db,
        _=_boss(db),
    )

    assert (
        db.query(LessonAssignment)
        .filter(LessonAssignment.teacher_id == incumbent.id)
        .count()
        == 3
    )


def test_already_assigned_lessons_are_not_counted_or_duplicated(db):
    school = _school(db)
    teacher = _teacher(db, school)
    lessons = _lessons(db, 4)
    _assign(db, lessons[0], teacher)

    result = bulk_assignments(
        payload=BulkAssignment(
            school_id=school.id,
            lesson_ids=[l.id for l in lessons],
            add_teacher_ids=[teacher.id],
        ),
        db=db,
        _=_boss(db),
    )

    assert result.assignments_added == 3
    assert result.lessons_touched == 3
    assert (
        db.query(LessonAssignment)
        .filter(
            LessonAssignment.teacher_id == teacher.id,
            LessonAssignment.lesson_id == lessons[0].id,
        )
        .count()
        == 1
    )


def test_removing_takes_the_assignment_and_its_progress(db):
    school = _school(db)
    teacher = _teacher(db, school)
    lessons = _lessons(db, 3)
    for lesson in lessons:
        _assign(db, lesson, teacher)

    result = bulk_assignments(
        payload=BulkAssignment(
            school_id=school.id,
            lesson_ids=[l.id for l in lessons],
            remove_teacher_ids=[teacher.id],
        ),
        db=db,
        _=_boss(db),
    )

    assert result.assignments_removed == 3
    assert db.query(LessonAssignment).filter(LessonAssignment.teacher_id == teacher.id).count() == 0
    assert db.query(Progress).filter(Progress.teacher_id == teacher.id).count() == 0


# --- Weighing the edit first ----------------------------------------------- #


def test_preview_counts_the_work_a_removal_would_destroy(db):
    school = _school(db)
    teacher = _teacher(db, school, name="Rita")
    lessons = _lessons(db, 4)
    _assign(db, lessons[0], teacher, status_=LessonStatus.completed)
    _assign(db, lessons[1], teacher, status_=LessonStatus.in_progress)
    _assign(db, lessons[2], teacher)  # not started — nothing of value lost

    preview = preview_bulk_assignments(
        payload=BulkAssignment(
            school_id=school.id,
            lesson_ids=[l.id for l in lessons],
            remove_teacher_ids=[teacher.id],
        ),
        db=db,
        _=_boss(db),
    )

    assert preview.removes == 3
    assert preview.progress_lost == 2
    assert preview.teachers_losing_progress == ["Rita"]
    # Previewing changes nothing.
    assert db.query(LessonAssignment).filter(LessonAssignment.teacher_id == teacher.id).count() == 3


def test_preview_of_a_pure_addition_loses_nothing(db):
    school = _school(db)
    teacher = _teacher(db, school)
    lessons = _lessons(db, 5)

    preview = preview_bulk_assignments(
        payload=BulkAssignment(
            school_id=school.id,
            lesson_ids=[l.id for l in lessons],
            add_teacher_ids=[teacher.id],
        ),
        db=db,
        _=_boss(db),
    )

    assert (preview.adds, preview.removes, preview.progress_lost) == (5, 0, 0)
    assert preview.lessons == 5


# --- Refusals -------------------------------------------------------------- #


def test_a_teacher_cannot_be_added_and_removed_at_once(db):
    school = _school(db)
    teacher = _teacher(db, school)
    lessons = _lessons(db, 2)

    with pytest.raises(HTTPException) as err:
        bulk_assignments(
            payload=BulkAssignment(
                school_id=school.id,
                lesson_ids=[l.id for l in lessons],
                add_teacher_ids=[teacher.id],
                remove_teacher_ids=[teacher.id],
            ),
            db=db,
            _=_boss(db),
        )
    assert err.value.status_code == 400


def test_teachers_outside_the_chosen_school_are_refused(db):
    school = _school(db)
    other = _school(db, "Elsewhere")
    outsider = _teacher(db, other, name="Outsider")
    lessons = _lessons(db, 2)

    with pytest.raises(HTTPException) as err:
        bulk_assignments(
            payload=BulkAssignment(
                school_id=school.id,
                lesson_ids=[l.id for l in lessons],
                add_teacher_ids=[outsider.id],
            ),
            db=db,
            _=_boss(db),
        )
    assert err.value.status_code == 400
    assert db.query(LessonAssignment).filter(LessonAssignment.teacher_id == outsider.id).count() == 0


def test_an_empty_edit_is_refused_rather_than_silently_doing_nothing(db):
    school = _school(db)
    teacher = _teacher(db, school)
    lessons = _lessons(db, 2)

    with pytest.raises(HTTPException):  # no lessons
        bulk_assignments(
            payload=BulkAssignment(
                school_id=school.id, lesson_ids=[], add_teacher_ids=[teacher.id]
            ),
            db=db,
            _=_boss(db),
        )
    with pytest.raises(HTTPException):  # no teachers
        bulk_assignments(
            payload=BulkAssignment(school_id=school.id, lesson_ids=[l.id for l in lessons]),
            db=db,
            _=_boss(db),
        )
