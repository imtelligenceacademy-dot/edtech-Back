"""Kindergarten: KG1-KG3 teachers, MTiny lessons, and no assistant.

Kindergarten has no grade number, and every lesson in the product is filed
under one. The vocabulary in ``services/grades`` gives KG a reserved integer
below zero and converts at the edges; these tests hold that conversion to its
promises, because a wrong answer anywhere in it is a teacher who receives no
lessons at all and no error saying why.

The assistant is switched off for these teachers. It has never read an MTiny
lesson, so every answer it gave would be invented — and the rule is per
account, not per role, so the tests below also pin the case it must not catch:
a teacher who takes KG2 *and* Grade 1 still teaches the curriculum the
assistant is grounded in, and keeps it.

That exception is why one gate is not enough. The account-level one lets her
through, and she was still getting invented answers whenever the lesson she had
open was an MTiny one — so there is a second gate on the lesson itself, and both
are tested here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Lesson, LessonAssignment, Progress, School, User
from app.models.enums import Role, UserStatus
from app.permissions import teaches_only_kindergarten, user_can
from app.routers import ai
from app.schemas.ai import AIChatRequest
from app.schemas.user import VALID_GRADES
from app.security import hash_password
from app.services.auto_assign import parse_lesson_filename, sync_teacher_assignments
from app.services.grades import (
    ALL_GRADE_TOKENS,
    grade_label,
    grade_number,
    grade_token,
    is_kindergarten,
)
from app.services.lesson_access import is_lesson_available
from app.services.sections import rename_section
from app.utils import new_id


# --------------------------------------------------------------------------- #
# The vocabulary itself
# --------------------------------------------------------------------------- #

def test_kindergarten_sorts_ahead_of_grade_one():
    """The whole reason KG is stored below zero: ordering falls out of it."""
    stored = [grade_number(t) for t in ALL_GRADE_TOKENS]
    assert stored == sorted(stored)
    assert stored[:3] == [grade_number("KG1"), grade_number("KG2"), grade_number("KG3")]
    assert all(n is not None and n < 1 for n in stored[:3])


@pytest.mark.parametrize("token", ALL_GRADE_TOKENS)
def test_token_survives_a_round_trip(token):
    number = grade_number(token)
    assert number is not None
    assert grade_token(number) == token


@pytest.mark.parametrize(
    "text", ["G13", "G0", "KG4", "KG0", "grade", "", "  ", "-3", "0"]
)
def test_things_that_are_not_grades_are_refused(text):
    assert grade_number(text) is None


def test_kindergarten_is_not_called_grade_kg1():
    """Nobody says "Grade KG1", and report headings are read by humans."""
    assert grade_label(grade_number("KG1")) == "KG1"
    assert grade_label(grade_number("G7")) == "Grade 7"


def test_kg3_is_a_grade_a_teacher_can_be_given():
    assert ("KG1", "KG2", "KG3") == VALID_GRADES[:3]
    assert all(is_kindergarten(t) for t in VALID_GRADES[:3])
    assert not any(is_kindergarten(t) for t in VALID_GRADES[3:])


# --------------------------------------------------------------------------- #
# Uploaded MTiny files
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "filename,token,course,lesson_no",
    [
        ("KG1 MTiny lesson 03 Colors.pdf", "KG1", "mtiny", 3),
        ("KG2 mtiny lesson 1 Shapes.pdf", "KG2", "mtiny", 1),
        ("KG3 M Tiny Lesson 12 Patterns.pdf", "KG3", "mtiny", 12),
        ("kg1 lesson 2 Counting.pdf", "KG1", None, 2),
    ],
)
def test_mtiny_filenames_parse(filename, token, course, lesson_no):
    parsed = parse_lesson_filename(filename)
    assert parsed is not None
    assert (parsed.grade_token, parsed.course, parsed.lesson_no) == (
        token,
        course,
        lesson_no,
    )
    assert parsed.title == filename[:-4]


@pytest.mark.parametrize(
    "filename,token,course,lesson_no",
    [
        ("Grade 7 python lesson 04 Variables.pdf", "G7", "python", 4),
        ("Grade 7 micro:bit lesson 04 Buzzer.pdf", "G7", "microbit", 4),
        ("Grade 7 Lesson 04 Light Sensor.pdf", "G7", None, 4),
    ],
)
def test_the_names_that_already_worked_still_do(filename, token, course, lesson_no):
    """Widening the pattern must not move anything it already matched."""
    parsed = parse_lesson_filename(filename)
    assert parsed is not None
    assert (parsed.grade_token, parsed.course, parsed.lesson_no) == (
        token,
        course,
        lesson_no,
    )


@pytest.mark.parametrize(
    "filename",
    ["KG4 MTiny lesson 1 Nope.pdf", "Grade 13 lesson 1 Nope.pdf", "Colors.pdf"],
)
def test_names_outside_the_convention_are_not_lessons(filename):
    assert parse_lesson_filename(filename) is None


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #

@pytest.fixture()
def school(db):
    row = School(
        id=new_id("sch"), name="Kindergarten Test", country="LB", city="Beirut",
        program_year=2,
    )
    db.add(row)
    db.flush()
    return row


def _teacher(db, school, grades, name="KG Teacher"):
    row = User(
        id=new_id("usr"),
        name=name,
        email=f"{new_id('kg')}@example.com",
        password_hash=hash_password("not-a-real-password"),
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=grades,
        language="en",
    )
    db.add(row)
    db.flush()
    return row


def _lesson(db, token, lesson_no, course="mtiny", year=2):
    row = Lesson(
        id=new_id("les"),
        title=f"{token} MTiny lesson {lesson_no:02d} Something",
        grade=grade_number(token),
        subject="STEAM",
        language="en",
        year=year,
        course=course,
        lesson_no=lesson_no,
    )
    db.add(row)
    db.flush()
    return row


def test_a_kindergarten_teacher_receives_mtiny_lessons(db, school):
    """The bug this whole change exists to fix: the rule compared "G{grade}"
    against the teacher's tokens, so "KG1" could never match anything."""
    lesson = _lesson(db, "KG1", 3)
    teacher = _teacher(db, school, ["KG1"])

    assert sync_teacher_assignments(db, teacher) == 1
    db.flush()
    assigned = {
        a.lesson_id
        for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
        )
    }
    assert assigned == {lesson.id}


def test_a_kindergarten_teacher_does_not_receive_another_grades_lessons(db, school):
    _lesson(db, "KG2", 1)
    _lesson(db, "G7", 1, course="python")
    teacher = _teacher(db, school, ["KG1"])

    assert sync_teacher_assignments(db, teacher) == 0


def test_renaming_a_kindergarten_class_moves_its_progress(db, school):
    """Renaming a class moves everything recorded under the old label.

    On a kindergarten grade this used to raise instead: the token has no
    leading "G" to strip, so the lookup went through ``int("KG1")``. The rename
    failed before it started, and the class's progress stayed where it was.
    """
    lesson = _lesson(db, "KG1", 1)
    teacher = _teacher(db, school, ["KG1"])
    teacher.sections = {"KG1": ["Red", "Blue"]}
    db.flush()
    sync_teacher_assignments(db, teacher)
    db.flush()

    before = _progress_sections(db, teacher, lesson)
    assert before == {"Red", "Blue"}

    assert rename_section(db, teacher, "KG1", "Red", "Rose") == 1
    db.flush()
    assert _progress_sections(db, teacher, lesson) == {"Rose", "Blue"}


def _progress_sections(db, teacher, lesson) -> set[str]:
    return {
        p.section
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == teacher.id, Progress.lesson_id == lesson.id
            )
        )
    }


# --------------------------------------------------------------------------- #
# The assistant
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(db):
    """The real app, with the session and the signed-in user supplied.

    Request it before any fixture that writes: starting the app runs the
    migrations, and that DDL deadlocks against an open write transaction.
    """
    holder: dict[str, User] = {}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    try:
        with TestClient(app) as c:
            yield c, holder
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "grades,only_kg",
    [
        (["KG1"], True),
        (["KG1", "KG2", "KG3"], True),
        (["KG2", "G1"], False),
        (["G1"], False),
        ([], False),
    ],
)
def test_who_counts_as_a_kindergarten_teacher(db, school, grades, only_kg):
    teacher = _teacher(db, school, grades)
    assert teaches_only_kindergarten(teacher) is only_kg
    assert user_can(teacher, "use-ai-assistant") is not only_kg


def test_a_teacher_with_no_grades_keeps_the_assistant(db, school):
    """An account mid-setup is not a kindergarten teacher. Reading "no grades"
    as "only kindergarten grades" would quietly disable the assistant for every
    teacher an admin had not finished configuring."""
    assert user_can(_teacher(db, school, []), "use-ai-assistant") is True


@pytest.mark.parametrize("path", ["/api/ai/chat", "/api/ai/chat/stream"])
def test_the_assistant_refuses_a_kindergarten_teacher(client, db, school, path):
    c, holder = client
    holder["user"] = _teacher(db, school, ["KG1", "KG2"])

    response = c.post(path, json={"message": "How do I teach colours?"})
    assert response.status_code == 403
    assert "use-ai-assistant" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/api/ai/chat", "/api/ai/chat/stream"])
def test_the_assistant_still_answers_a_teacher_of_both(client, db, school, path):
    """The rule must not catch a teacher who takes KG2 alongside Grade 1."""
    c, holder = client
    holder["user"] = _teacher(db, school, ["KG2", "G1"])

    response = c.post(path, json={"message": "How do I teach loops?"})
    assert response.status_code != 403


@pytest.mark.parametrize("path", ["/api/ai/chat", "/api/ai/chat/stream"])
def test_the_assistant_refuses_a_kindergarten_lesson(client, db, school, path):
    """The half that was missing. A teacher of KG2 *and* Grade 1 keeps the
    capability, so the account-level gate lets her through — and she was getting
    invented answers about an MTiny lesson the model has never seen. The gate has
    to look at the lesson that is open, not only at who is asking."""
    c, holder = client
    teacher = _teacher(db, school, ["KG2", "G1"])
    holder["user"] = teacher
    lesson = _lesson(db, "KG2", 1)
    sync_teacher_assignments(db, teacher)
    db.flush()

    response = c.post(path, json={"message": "How do I teach colours?", "lessonId": lesson.id})

    # A 200 is not the marker: a model answer returns 200 too. Nor is "MTiny" --
    # `_lesson` puts it in every title, so it comes back either way, and this test
    # stayed green with the gate taken out. Only the canned refusal says this.
    assert response.status_code == 200
    assert "haven't been taught" in response.text


def test_a_grade_one_lesson_still_reaches_the_model(db, school):
    """The other side of the same gate. Tested at ``_build_prompt`` because
    letting this one through means reaching a provider, and what matters here is
    that the kindergarten refusal is not what comes back."""
    teacher = _teacher(db, school, ["KG2", "G1"])
    lesson = _lesson(db, "G1", 1, course="microbit")
    sync_teacher_assignments(db, teacher)
    db.flush()
    # Without this the lesson would be refused as "not open to you" and the test
    # would pass for the wrong reason, still green with the fix taken out.
    assert is_lesson_available(db, teacher, lesson.id), "the lesson must be open to her"

    bundle = ai._build_prompt(
        db, teacher, AIChatRequest(message="How do I teach loops?", lessonId=lesson.id)
    )

    assert bundle.refusal is None
