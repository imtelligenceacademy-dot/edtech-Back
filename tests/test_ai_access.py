"""A locked, unassigned or unrelated lesson must never reach the provider, and
must never have its slide rendered.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Lesson,
    LessonAssignment,
    Progress,
    School,
    UploadedFile,
    User,
)
from app.models.enums import LessonStatus, Role, UserStatus
from app.routers import ai
from app.schemas.ai import AIChatRequest
from app.services.ai_usage import quota_for
from app.utils import new_id


@pytest.fixture()
def world(db):
    """A school, a teacher, and two sequential lessons (1 open, 2 locked)."""
    school = School(id=new_id("sch"), name="S", program_year=2)
    db.add(school)
    teacher = User(
        id=new_id("u"), name="T", email=f"{new_id('t')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=["G7"], language="en",
    )
    other = User(
        id=new_id("u"), name="O", email=f"{new_id('o')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=["G7"], language="en",
    )
    db.add_all([teacher, other])
    db.flush()

    lessons = []
    for n in (1, 2):
        les = Lesson(
            id=new_id("les"), title=f"Grade 7 python lesson 0{n}", grade=7,
            subject="STEAM", language="en", year=2, course="python", lesson_no=n,
            created_by=teacher.id,
        )
        db.add(les)
        db.flush()
        db.add(LessonAssignment(
            id=new_id("la"), lesson_id=les.id, teacher_id=teacher.id, source="rule"
        ))
        lessons.append(les)
    db.commit()
    return {"teacher": teacher, "other": other, "l1": lessons[0], "l2": lessons[1]}


def test_available_lesson_is_grounded(db, world):
    bundle = ai._build_prompt(
        db, world["teacher"], AIChatRequest(message="hi", lessonId=world["l1"].id)
    )
    assert bundle.grounded is True
    assert bundle.source_ref == world["l1"].title


def test_locked_lesson_is_refused_and_never_grounded(db, world):
    # Lesson 2 is locked until lesson 1 is completed.
    bundle = ai._build_prompt(
        db, world["teacher"], AIChatRequest(message="hi", lessonId=world["l2"].id)
    )
    assert bundle.grounded is False
    assert bundle.image_data_url is None
    assert bundle.refusal == ai._LESSON_NOT_OPEN_TO_YOU


def test_lesson_not_assigned_to_this_teacher_is_refused(db, world):
    bundle = ai._build_prompt(
        db, world["other"], AIChatRequest(message="hi", lessonId=world["l1"].id)
    )
    assert bundle.grounded is False
    assert bundle.image_data_url is None


def test_unknown_lesson_id_is_refused(db, world):
    bundle = ai._build_prompt(
        db, world["teacher"], AIChatRequest(message="hi", lessonId="les_does_not_exist")
    )
    assert bundle.grounded is False


def test_no_lesson_open_is_refused(db, world):
    """Nothing named at all: the teacher is told to open one, not that a lesson
    of hers is locked."""
    bundle = ai._build_prompt(db, world["teacher"], AIChatRequest(message="hi"))
    assert bundle.grounded is False
    assert bundle.refusal == ai._NO_LESSON_OPEN


def test_locked_lesson_never_renders_a_slide(db, world, monkeypatch):
    """The renderer must not even be called for an inaccessible lesson."""
    called = []
    monkeypatch.setattr(
        ai, "render_page_data_url", lambda *a, **k: called.append(a) or "data:x"
    )
    ai._build_prompt(
        db, world["teacher"],
        AIChatRequest(message="hi", lessonId=world["l2"].id, currentSlide=1),
    )
    assert called == [], "renderer must not run for a locked lesson"


def test_fair_project_requires_the_access_flag(db, world):
    from app.models import FairProject

    uploaded = UploadedFile(
        id=new_id("file"), filename="p.pdf", content_type="application/pdf",
        size_bytes=1, storage_path="missing.pdf", uploaded_by=world["teacher"].id,
    )
    db.add(uploaded)
    db.flush()
    project = FairProject(id=new_id("fair"), title="Robot arm", file_id=uploaded.id)
    db.add(project)
    db.commit()

    # Teacher without the flag: refused.
    world["teacher"].ict_fair_access = False
    db.commit()
    bundle = ai._build_prompt(
        db, world["teacher"], AIChatRequest(message="hi", fairProjectId=project.id)
    )
    assert bundle.grounded is False

    # With the flag: grounded on the project.
    world["teacher"].ict_fair_access = True
    db.commit()
    bundle = ai._build_prompt(
        db, world["teacher"], AIChatRequest(message="hi", fairProjectId=project.id)
    )
    assert bundle.grounded is True
    assert bundle.source_ref == "Robot arm"


# --------------------------------------------------------------------------- #
# A refusal is an answer, not a failure
# --------------------------------------------------------------------------- #
def test_a_refusal_never_reaches_a_provider(db, world, monkeypatch):
    """It used to. The fixed sentence was asked of the model, so a provider
    having a bad minute turned "open a lesson first" into "the AI assistant is
    unavailable" — which sent teachers looking for a fault that wasn't there."""
    called = []
    monkeypatch.setattr(ai, "get_provider", lambda: called.append(1))

    bundle = ai._build_prompt(db, world["teacher"], AIChatRequest(message="hi"))

    assert bundle.refusal == ai._NO_LESSON_OPEN
    assert called == [], "no provider should be consulted to say this"


def test_a_refusal_costs_no_questions_from_the_allowance(db, world):
    """Fifteen questions an hour is what a teacher gets. Being told to open a
    lesson must not spend one of them."""
    teacher = world["teacher"]
    before = quota_for(db, teacher, "teacher")["hourly_used"]

    response = ai.chat(
        AIChatRequest(message="hi"), db=db, current=teacher
    )

    assert response.content == ai._NO_LESSON_OPEN
    assert quota_for(db, teacher, "teacher")["hourly_used"] == before


def test_naming_a_locked_lesson_says_so_rather_than_blaming_the_assistant(db, world):
    bundle = ai._build_prompt(
        db, world["teacher"], AIChatRequest(message="hi", lessonId=world["l2"].id)
    )
    assert bundle.refusal == ai._LESSON_NOT_OPEN_TO_YOU
    assert "Request access" in bundle.refusal
