"""Whether the client is told that asking again could help.

`_ERROR_TEXT` already draws the line in prose — "try again in a moment" against
"tell your administrator" — but only the words crossed the wire. The chat offered
a retry button for every failure it caught, including the ones whose own message
says a retry cannot work: a spent allowance, a missing key, a refused request.

These assert the flag on the frame, not the wording, because the wording is what
the client was reduced to guessing from.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from app.config import settings
from app.models import AiUsage, Lesson, LessonAssignment, School, User
from app.models.enums import Role, UserStatus
from app.routers.ai import _error_frame, _provider_error_frame, chat_stream
from app.schemas.ai import AIChatRequest
from app.services.llm import LLMError
from app.utils import new_id


def _frames(response) -> list[dict]:
    """Drain a StreamingResponse into the parsed events it actually sent."""

    async def drain() -> list[bytes]:
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(drain())
    text = b"".join(
        c if isinstance(c, bytes) else c.encode("utf-8") for c in chunks
    ).decode("utf-8")
    return [
        json.loads(line[6:])
        for line in text.split("\n\n")
        if line.startswith("data: ")
    ]


def _teacher_with_a_lesson_open(db) -> tuple[User, Lesson]:
    """A teacher mid-lesson.

    The lesson matters: with none open the assistant refuses before it ever
    reaches the quota check — deliberately, so a teacher who has not opened
    anything is not charged — and the branch under test is never entered.
    """
    school = School(
        id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.flush()
    user = User(
        id=new_id("u"),
        name="Retry teacher",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        grades=["G7"],
        school_id=school.id,
    )
    db.add(user)
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
            id=new_id("la"), lesson_id=lesson.id, teacher_id=user.id, source="rule"
        )
    )
    db.commit()
    return user, lesson


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


def test_a_spent_allowance_is_not_offered_a_retry(db):
    """The one the teacher actually meets: the hourly cap, mid-lesson."""
    teacher, lesson = _teacher_with_a_lesson_open(db)
    _spend_the_hour(db, teacher)

    response = chat_stream(
        payload=AIChatRequest(
            message="why is my LED not lighting?", lesson_id=lesson.id
        ),
        db=db,
        current=teacher,
    )
    events = _frames(response)

    error = next(e for e in events if "error" in e)
    assert "reached" in error["error"]
    assert error["retryable"] is False
    assert events[-1] == {"done": True}


def test_a_busy_provider_is_worth_asking_again():
    frame = json.loads(_provider_error_frame(LLMError("rate_limit"))[6:])
    assert frame["retryable"] is True

    frame = json.loads(_provider_error_frame(LLMError("timeout"))[6:])
    assert frame["retryable"] is True


def test_the_failures_whose_own_message_says_not_to_are_not_retryable():
    for kind in ("auth", "quota", "bad_request"):
        frame = json.loads(_provider_error_frame(LLMError(kind))[6:])
        assert frame["retryable"] is False, kind
        # The flag has to agree with the sentence beside it, or the button and
        # the text tell the teacher opposite things.
        assert "try again" not in frame["error"].lower(), kind


def test_an_unrecognised_failure_stays_retryable():
    """An exception with no kind is an unknown, not a refusal — the old
    behaviour, kept, because assuming otherwise hides a transient fault."""
    frame = json.loads(_provider_error_frame(RuntimeError("something odd"))[6:])
    assert frame["retryable"] is True


def test_the_frame_is_still_valid_sse():
    raw = _error_frame("nope", retryable=False)
    assert raw.startswith("data: ") and raw.endswith("\n\n")
    assert json.loads(raw[6:]) == {"error": "nope", "retryable": False}
