"""Whose assignments a lesson discloses.

A curriculum lesson is shared: one Grade 7 lesson carries an assignment for
every Grade 7 teacher on the platform, across every school. Serialising that
list whole handed a school-admin the user ids of other schools' teachers, and
let them count another school's roster lesson by lesson.

Super-admins still see all of it — the assignment list is what they administer.
"""

from __future__ import annotations

from app.models import Lesson, LessonAssignment, School, User
from app.models.enums import Role, UserStatus
from app.routers.lessons import get_lesson, list_lessons
from app.utils import new_id


def _school(db, name: str) -> School:
    school = School(
        id=new_id("sch"), name=name, country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.commit()
    return school


def _user(db, role: Role, school: School | None, grades=("G7",)) -> User:
    user = User(
        id=new_id("u"),
        name=f"{role.value}-{new_id('n')}",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=role,
        status=UserStatus.active,
        school_id=school.id if school else None,
        grades=list(grades),
    )
    db.add(user)
    db.commit()
    return user


def _shared_lesson(db, teachers: list[User]) -> Lesson:
    """A curriculum lesson — no school of its own — taught in several schools."""
    lesson = Lesson(
        id=new_id("les"),
        title=f"Shared Grade 7 lesson {new_id('t')}",
        grade=7,
        subject="STEAM",
        language="en",
        year=2,
        lesson_no=1,
        school_id=None,
    )
    db.add(lesson)
    db.flush()
    for teacher in teachers:
        db.add(
            LessonAssignment(
                id=new_id("la"),
                lesson_id=lesson.id,
                teacher_id=teacher.id,
                source="rule",
            )
        )
    db.commit()
    return lesson


def _row(rows, lesson_id):
    return next(r for r in rows if r.id == lesson_id)


def test_school_admin_sees_only_their_own_schools_teachers(db):
    a, b = _school(db, "A"), _school(db, "B")
    mine = _user(db, Role.teacher, a)
    theirs = _user(db, Role.teacher, b)
    lesson = _shared_lesson(db, [mine, theirs])
    admin = _user(db, Role.school_admin, a, grades=())

    listed = _row(list_lessons(section=None, db=db, current=admin), lesson.id)
    fetched = get_lesson(lesson_id=lesson.id, section=None, db=db, current=admin)

    assert listed.assigned_teacher_ids == [mine.id]
    assert fetched.assigned_teacher_ids == [mine.id]
    # The point of the fix: the other school's teacher is not merely unnamed,
    # their id is absent, so the roster cannot be counted either.
    assert theirs.id not in listed.assigned_teacher_ids
    assert theirs.id not in fetched.assigned_teacher_ids


def test_a_teacher_sees_only_themselves(db):
    a, b = _school(db, "A"), _school(db, "B")
    me = _user(db, Role.teacher, a)
    colleague = _user(db, Role.teacher, a)
    stranger = _user(db, Role.teacher, b)
    lesson = _shared_lesson(db, [me, colleague, stranger])

    listed = _row(list_lessons(section=None, db=db, current=me), lesson.id)

    assert listed.assigned_teacher_ids == [me.id]


def test_a_super_admin_still_sees_every_assignment(db):
    """The scoping must not blind the role that administers assignments."""
    a, b = _school(db, "A"), _school(db, "B")
    one, two = _user(db, Role.teacher, a), _user(db, Role.teacher, b)
    lesson = _shared_lesson(db, [one, two])
    boss = _user(db, Role.super_admin, None, grades=())

    listed = _row(list_lessons(section=None, db=db, current=boss), lesson.id)
    fetched = get_lesson(lesson_id=lesson.id, section=None, db=db, current=boss)

    assert sorted(listed.assigned_teacher_ids) == sorted([one.id, two.id])
    assert sorted(fetched.assigned_teacher_ids) == sorted([one.id, two.id])
