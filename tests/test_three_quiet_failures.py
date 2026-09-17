"""Three places that failed without saying so.

- A correct password on a suspended account was refused and never written to the
  security log, so an admin who had suspended an account *because* its
  credentials leaked saw an empty screen while the leaked password was in use.
- A provider error arriving mid-stream was skipped along with every other
  non-delta line, so a half-written answer was shown and stored as a whole one.
- `my-classes` grouped by grade, class and language but not year, so a school
  promoted between curriculum years reported one merged track.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException, Request, Response

from app.models import (
    Lesson,
    LessonAssignment,
    School,
    SecurityLog,
    User,
)
from app.models.enums import Role, SecurityStatus, UserStatus
from app.routers.auth import login
from app.routers.lessons import my_classes
from app.schemas.auth import LoginRequest
from app.security import hash_password
from app.services.llm import ChatMessage, LLMError, _raise_for_stream_error
from app.utils import new_id

PASSWORD = "a-long-enough-password"


def _request(ip: str = "203.0.113.55") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "query_string": b"",
            "headers": [(b"user-agent", b"pytest")],
            "client": (ip, 51000),
        }
    )


def _user(db, status: UserStatus) -> User:
    user = User(
        id=new_id("u"),
        name="Teacher",
        email=f"{new_id('e')}@example.com",
        password_hash=hash_password(PASSWORD),
        role=Role.teacher,
        status=status,
        grades=["G7"],
    )
    db.add(user)
    db.commit()
    return user


def _logs_for(db, user: User) -> list[SecurityLog]:
    return list(
        db.query(SecurityLog).filter(SecurityLog.user_id == user.id).all()
    )


# --- 1. the sign-in that was refused in silence ---------------------------- #


@pytest.mark.parametrize(
    "account_status", [UserStatus.suspended, UserStatus.rejected, UserStatus.pending]
)
def test_the_right_password_on_a_blocked_account_is_recorded(db, account_status):
    user = _user(db, account_status)

    with pytest.raises(HTTPException) as err:
        login(
            payload=LoginRequest(email=user.email, password=PASSWORD),
            request=_request(),
            response=Response(),
            db=db,
        )

    assert err.value.status_code == 403
    rows = _logs_for(db, user)
    assert len(rows) == 1, "an admin asking whether the leak is in use needs a row"
    assert rows[0].status == SecurityStatus.blocked


def test_a_wrong_password_on_a_blocked_account_is_not_double_counted(db):
    """The control: this branch must not start logging attempts twice."""
    user = _user(db, UserStatus.suspended)

    with pytest.raises(HTTPException):
        login(
            payload=LoginRequest(email=user.email, password="wrong-password-entirely"),
            request=_request(),
            response=Response(),
            db=db,
        )

    assert len(_logs_for(db, user)) == 1


def test_an_active_account_still_signs_in(db):
    """The control that matters most: this sits on the path to every login."""
    user = _user(db, UserStatus.active)

    session = login(
        payload=LoginRequest(email=user.email, password=PASSWORD),
        request=_request(),
        response=Response(),
        db=db,
    )

    assert session.user_id == user.id


# --- 2. the answer that stopped halfway ------------------------------------ #


def test_an_openai_style_error_frame_is_raised_not_skipped():
    with pytest.raises(LLMError) as err:
        _raise_for_stream_error({"error": {"message": "backend overloaded", "type": "server_error"}})
    assert err.value.kind == "unavailable"
    assert "overloaded" in str(err.value)


def test_an_anthropic_style_error_frame_is_raised_not_skipped():
    with pytest.raises(LLMError) as err:
        _raise_for_stream_error({"type": "error", "error": {"type": "overloaded_error"}})
    assert err.value.kind == "unavailable"


def test_ordinary_frames_pass_straight_through():
    """The control: everything that is not an error must stay silent, or the
    first content delta of every answer would raise."""
    for frame in (
        {"choices": [{"delta": {"content": "hello"}}]},
        {"type": "content_block_delta", "delta": {"text": "hello"}},
        {"type": "message_start"},
        {"choices": [{"delta": {}}]},
        {},
        "not a dict at all",
        None,
    ):
        _raise_for_stream_error(frame)


def test_a_mid_stream_error_stops_the_generator_rather_than_ending_it(monkeypatch):
    """End to end through the real parser: eight deltas, then a failure.

    Before, the loop skipped the error frame, hit the end of the stream and
    returned normally — so the caller saw a complete answer.
    """
    from app.services import llm as llm_module

    lines = [
        'data: {"choices":[{"delta":{"content":"step one "}}]}',
        'data: {"choices":[{"delta":{"content":"step two "}}]}',
        'data: {"error":{"message":"overloaded","type":"server_error"}}',
        'data: {"choices":[{"delta":{"content":"never reached"}}]}',
        "data: [DONE]",
    ]

    class _Resp:
        status_code = 200

        def iter_lines(self):
            return iter(lines)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(llm_module.httpx, "stream", lambda *a, **k: _Resp())
    provider = llm_module.OpenAICompatProvider(
        name="test", base_url="https://example.invalid", api_key="k", model="m"
    )

    got: list[str] = []
    with pytest.raises(LLMError):
        for chunk in provider.chat_stream("system", [ChatMessage(role="user", content="hi")]):
            got.append(chunk)

    assert got == ["step one ", "step two "], "what arrived before the failure is kept"


# --- 3. two curricula reported as one -------------------------------------- #


def _teacher_with_two_years(db) -> User:
    school = School(
        id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.flush()
    teacher = User(
        id=new_id("u"),
        name="Promoted",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G7"],
    )
    db.add(teacher)
    db.flush()
    for year, count in ((1, 2), (2, 3)):
        for n in range(1, count + 1):
            lesson = Lesson(
                id=new_id("les"),
                title=f"Year {year} lesson {n:02d}",
                grade=7,
                subject="STEAM",
                language="en",
                year=year,
                lesson_no=n,
                course="python" if year == 2 else None,
            )
            db.add(lesson)
            db.flush()
            db.add(
                LessonAssignment(
                    id=new_id("la"),
                    lesson_id=lesson.id,
                    teacher_id=teacher.id,
                    source="rule",
                )
            )
    db.commit()
    return teacher


def test_two_curriculum_years_are_two_tracks_not_one(db):
    teacher = _teacher_with_two_years(db)

    rows = my_classes(db=db, current=teacher)

    # Two rows for the one class: the retained Year-1 lessons and the Year-2
    # curriculum are sequenced separately, so they must be counted separately.
    assert len(rows) == 2
    assert sorted(r.total for r in rows) == [2, 3]
    assert {r.grade for r in rows} == {7}


def test_a_single_year_class_is_still_one_row(db):
    """The control: the extra key must not split an ordinary class in two."""
    school = School(
        id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.flush()
    teacher = User(
        id=new_id("u"),
        name="Ordinary",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G7"],
    )
    db.add(teacher)
    db.flush()
    for n in (1, 2, 3):
        lesson = Lesson(
            id=new_id("les"),
            title=f"Lesson {n:02d}",
            grade=7,
            subject="STEAM",
            language="en",
            year=2,
            lesson_no=n,
            course="python",
        )
        db.add(lesson)
        db.flush()
        db.add(
            LessonAssignment(
                id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
            )
        )
    db.commit()

    rows = my_classes(db=db, current=teacher)

    assert len(rows) == 1
    assert rows[0].total == 3
