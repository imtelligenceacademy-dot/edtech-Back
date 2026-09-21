"""The three lows, and the controls that say the fixes did not overshoot.

- A teacher over her quota still had the slide rendered and, for a slide not
  already transcribed, a billed vision call made and a row committed — before
  being told no and charged nothing.
- The source chip claimed a slide had been consulted whenever one had been
  *rendered*, which is not the same as read. When the seeing provider dropped
  out and the reader came back empty, the answer said it could not check the
  slide and the chip beside it still named one.
- A super-admin created through the API kept whatever `schoolId` the payload
  carried, while the edit path has always cleared it.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException, Request

from app.config import settings
from app.models import AiUsage, Lesson, LessonAssignment, School, User
from app.models.enums import Role, UserStatus
from app.routers import ai as ai_module
from app.routers.ai import PromptBundle, _build_prompt, _without_image, chat_stream
from app.routers.users import create_user
from app.schemas.ai import AIChatRequest
from app.schemas.user import UserCreate
from app.utils import new_id


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/whatever",
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.9", 51000),
        }
    )


def _frames(response) -> list[dict]:
    import asyncio

    async def drain():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(drain())
    text = b"".join(c if isinstance(c, bytes) else c.encode("utf-8") for c in chunks).decode()
    return [json.loads(line[6:]) for line in text.split("\n\n") if line.startswith("data: ")]


def _school(db) -> School:
    school = School(
        id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.commit()
    return school


def _teacher_with_a_lesson(db) -> tuple[User, Lesson]:
    school = _school(db)
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
    lesson = Lesson(
        id=new_id("les"),
        title="Grade 7 lesson 01",
        grade=7,
        subject="STEAM",
        language="en",
        year=2,
        lesson_no=1,
    )
    db.add(lesson)
    db.flush()
    db.add(
        LessonAssignment(
            id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
        )
    )
    db.commit()
    return teacher, lesson


def _spend_the_hour(db, user: User) -> None:
    for _ in range(settings.ai_teacher_hourly_limit):
        db.add(
            AiUsage(
                id=new_id("aiu"),
                user_id=user.id,
                school_id=user.school_id,
                role=user.role,
                kind="teacher",
            )
        )
    db.commit()


# --- 1. work that is not done for a question that will be refused ---------- #


def test_a_teacher_over_quota_does_not_have_the_slide_rendered(db, monkeypatch):
    teacher, lesson = _teacher_with_a_lesson(db)
    _spend_the_hour(db, teacher)
    rendered: list[int] = []
    monkeypatch.setattr(
        ai_module,
        "_slide_image",
        lambda *a, **k: (rendered.append(1), (None, False))[1],
    )

    response = chat_stream(
        payload=AIChatRequest(
            message="what is on this slide?", lesson_id=lesson.id, current_slide=4
        ),
        db=db,
        current=teacher,
    )
    events = _frames(response)

    assert rendered == [], "nothing may be rendered for a question that is refused"
    error = next(e for e in events if "error" in e)
    assert "reached" in error["error"]
    assert error["retryable"] is False


def test_a_teacher_with_headroom_still_gets_the_slide_rendered(db, monkeypatch):
    """The control: the check must not stop the work for everybody."""
    teacher, lesson = _teacher_with_a_lesson(db)
    rendered: list[int] = []
    monkeypatch.setattr(
        ai_module,
        "_slide_image",
        lambda *a, **k: (rendered.append(1), (None, False))[1],
    )

    _build_prompt(
        db,
        teacher,
        AIChatRequest(message="hello", lesson_id=lesson.id, current_slide=4),
        before_work=lambda: None,
    )

    assert rendered == [1]


def test_nothing_open_is_still_refused_for_free(db, monkeypatch):
    """The control that matters most: a refusal must not be charged.

    `before_work` is where the quota is spent, and it has to stay on the far
    side of the refusal — telling a teacher to open a lesson should never cost
    her a question.
    """
    teacher, _lesson = _teacher_with_a_lesson(db)
    charged: list[int] = []

    bundle = _build_prompt(
        db,
        teacher,
        AIChatRequest(message="hello"),
        before_work=lambda: charged.append(1),
    )

    assert bundle.refusal is not None
    assert charged == [], "a refusal reaches no provider, so it costs nothing"


# --- 2. a chip that only claims what was consulted ------------------------- #


def test_the_slide_label_is_corrected_when_the_slide_was_never_read():
    bundle = PromptBundle(
        system="SYSTEM WITH IMAGE ATTACHED",
        messages=[],
        source_ref="Lesson 3 - slide 12",
        image_data_url="data:image/png;base64,AAAA",
        grounded=True,
        # The rebuild found no reading: the model is told the visual check
        # failed, so the answer cannot honestly be attributed to slide 12.
        text_fallback=lambda: ("SYSTEM WITH NO SLIDE", "Lesson 3"),
    )

    system = _without_image(bundle)

    assert system == "SYSTEM WITH NO SLIDE"
    assert bundle.source_ref == "Lesson 3"


def test_the_slide_label_stands_when_the_reader_did_supply_one():
    """The control: falling back is not the same as not seeing the slide."""
    bundle = PromptBundle(
        system="SYSTEM WITH IMAGE ATTACHED",
        messages=[],
        source_ref="Lesson 3 - slide 12",
        image_data_url="data:image/png;base64,AAAA",
        grounded=True,
        text_fallback=lambda: ("SYSTEM WITH SLIDE READING", "Lesson 3 - slide 12"),
    )

    _without_image(bundle)

    assert bundle.source_ref == "Lesson 3 - slide 12"


def test_a_failed_rebuild_leaves_the_reference_alone():
    """A rebuild that raises must not also lose the attribution."""

    def boom():
        raise RuntimeError("reader is down")

    bundle = PromptBundle(
        system="ORIGINAL",
        messages=[],
        source_ref="Lesson 3 - slide 12",
        image_data_url="data:image/png;base64,AAAA",
        grounded=True,
        text_fallback=boom,
    )

    assert _without_image(bundle) == "ORIGINAL"
    assert bundle.source_ref == "Lesson 3 - slide 12"


# --- 3. one rule about which accounts have a school ------------------------ #


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


def test_a_super_admin_created_with_a_school_does_not_keep_one(db):
    school = _school(db)

    created = create_user(
        payload=UserCreate(
            name="Platform Admin",
            email=f"{new_id('e')}@example.com",
            password="a-long-enough-password",
            role=Role.super_admin,
            school_id=school.id,
        ),
        db=db,
        _=_boss(db),
    )

    # Otherwise record_event stamps that school on every sign-in row, and its
    # admin reads the platform administrator's addresses in Security Logs.
    assert created.school_id is None


def test_a_teacher_still_gets_the_school_they_were_created_with(db):
    """The control: only a super-admin's school is discarded."""
    school = _school(db)

    created = create_user(
        payload=UserCreate(
            name="Teacher",
            email=f"{new_id('e')}@example.com",
            password="a-long-enough-password",
            role=Role.teacher,
            school_id=school.id,
            grades=["G7"],
        ),
        db=db,
        _=_boss(db),
    )

    assert created.school_id == school.id


def test_a_teacher_without_a_school_is_still_refused(db):
    with pytest.raises(HTTPException) as err:
        create_user(
            payload=UserCreate(
                name="Teacher",
                email=f"{new_id('e')}@example.com",
                password="a-long-enough-password",
                role=Role.teacher,
                grades=["G7"],
            ),
            db=db,
            _=_boss(db),
        )
    assert err.value.status_code == 400


def test_a_school_that_does_not_exist_is_a_400_not_a_500(db):
    """It reached the database as an FK violation before, which is a 500."""
    with pytest.raises(HTTPException) as err:
        create_user(
            payload=UserCreate(
                name="Teacher",
                email=f"{new_id('e')}@example.com",
                password="a-long-enough-password",
                role=Role.teacher,
                school_id="sch_does_not_exist",
                grades=["G7"],
            ),
            db=db,
            _=_boss(db),
        )
    assert err.value.status_code == 400
