"""Every question reaches OpenAI by the route that answers, and a failure says so.

A teacher with a slide on screen got an answer. A teacher clicking a starter
prompt on the lesson list, or on a phone, had no slide to attach, so the question
went to `/chat/completions` instead of `/responses` and came back refused: she saw
"Try a shorter question" after typing "hi", the backups were never asked because a
400 stopped the chain, the provider's reason was thrown away, and the owner's page
showed the question alone, as if nobody had replied.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import ChatMessage, Lesson, School, Slide, User
from app.models.enums import Role, UserStatus
from app.routers import ai
from app.services import llm
from app.services.auto_assign import sync_teacher_assignments
from app.services.chat_history import FAILURE_ROLE
from app.services.llm import LLMError
from app.utils import new_id


class _Stream:
    def __init__(self, lines=(), status_code=200, body=None):
        self.status_code = status_code
        self._lines = list(lines)
        self._body = body or {}

    def read(self):
        return b""

    def json(self):
        return self._body

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _delta(text: str) -> str:
    return "data: " + json.dumps({"type": "response.output_text.delta", "delta": text})


@pytest.fixture()
def openai_provider(monkeypatch):
    """The real provider, built the way production builds it."""
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    return llm._build("openai")


def test_a_question_with_no_slide_streams_through_responses(openai_provider, monkeypatch):
    sent = {}

    def stream(method, url, **kwargs):
        sent.update(url=url, **kwargs)
        return _Stream([_delta("An "), _delta("answer")])

    monkeypatch.setattr(llm.httpx, "stream", stream)

    reply = "".join(openai_provider.chat_stream("SYSTEM", [{"role": "user", "content": "hi"}]))

    assert reply == "An answer"
    assert sent["url"].endswith("/responses")
    assert sent["json"]["stream"] is True
    # The setting only the failing route sent, and nothing about a slide.
    assert "temperature" not in sent["json"]
    assert sent["json"]["input"][-1] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }


def test_a_whole_answer_comes_from_responses_too(openai_provider, monkeypatch):
    """Reports and the quiz resolver use this path. The model's reasoning arrives
    in the same list and must not end up in the answer."""
    sent = {}

    def post(url, **kwargs):
        sent.update(url=url, **kwargs)
        return _Stream(
            body={
                "output": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "Thinking aloud. "}],
                    },
                    {"type": "message", "content": [{"type": "output_text", "text": "An answer"}]},
                ]
            }
        )

    monkeypatch.setattr(llm.httpx, "post", post)

    assert openai_provider.chat("SYSTEM", [{"role": "user", "content": "hi"}]) == "An answer"
    assert sent["url"].endswith("/responses")
    assert sent["json"]["stream"] is False


def test_an_exam_keeps_its_longer_wait_on_this_route(openai_provider, monkeypatch):
    sent = {}

    def stream(method, url, **kwargs):
        sent.update(kwargs)
        return _Stream([_delta("An exam")])

    monkeypatch.setattr(llm.httpx, "stream", stream)

    list(openai_provider.chat_stream("S", [{"role": "user", "content": "x"}], long_answer=True))

    assert sent["timeout"] == settings.ai_assessment_timeout_seconds


def test_the_other_providers_stay_on_chat_completions(monkeypatch):
    """`/responses` is what OpenAI answers on; Groq and Grok are unchanged."""
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    sent = {}

    def stream(method, url, **kwargs):
        sent["url"] = url
        return _Stream(['data: {"choices": [{"delta": {"content": "hi"}}]}'])

    monkeypatch.setattr(llm.httpx, "stream", stream)

    assert "".join(llm._build("groq").chat_stream("S", [])) == "hi"
    assert sent["url"].endswith("/chat/completions")


def test_a_refusal_writes_the_providers_reason_to_the_log(openai_provider, monkeypatch, caplog):
    """Only the status used to survive, which is why this went undiagnosed. A
    rejected key's message quotes part of the key, so that part must not."""
    message = "Unsupported value for temperature. Your key sk-abc123XYZ was used."
    monkeypatch.setattr(
        llm.httpx,
        "stream",
        lambda *a, **k: _Stream(status_code=400, body={"error": {"message": message}}),
    )

    with caplog.at_level(logging.WARNING, logger="app.llm"):
        with pytest.raises(LLMError):
            list(openai_provider.chat_stream("SECRET-SYSTEM", [{"role": "user", "content": "QUESTION"}]))

    assert "Unsupported value for temperature" in caplog.text
    assert "returned 400" in caplog.text
    assert "sk-abc123XYZ" not in caplog.text
    assert "QUESTION" not in caplog.text
    assert "SECRET-SYSTEM" not in caplog.text


# --------------------------------------------------------------------------- #
# What each person sees when it fails
# --------------------------------------------------------------------------- #

class _Refuses:
    name = "openai"
    model = "m"
    supports_vision = False

    def chat_stream(self, system, messages, **kwargs):
        raise LLMError("bad_request", "provider returned 400")
        yield  # pragma: no cover - makes this a generator


class _DiesPartWay(_Refuses):
    def chat_stream(self, system, messages, **kwargs):
        yield "The first half"
        raise LLMError("unavailable", "provider dropped")


@pytest.fixture()
def client(db):
    holder: dict[str, User] = {}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    try:
        with TestClient(app) as c:
            yield c, holder
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def people(db):
    school = School(id=new_id("sch"), name="S", country="LB", city="Beirut", program_year=2)
    db.add(school)
    teacher = User(
        id=new_id("usr"), name="Jamal", email=f"{new_id('t')}@example.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id, grades=["G8"], language="en",
    )
    owner = User(
        id=new_id("usr"), name="Owner", email=f"{new_id('o')}@example.com", password_hash="x",
        role=Role.super_admin, status=UserStatus.active, grades=[],
    )
    lesson = Lesson(
        id=new_id("les"), title="Grade 8 Python Lesson 01 Hello Python", grade=8, subject="STEAM",
        language="en", year=2, course="python", lesson_no=1,
    )
    db.add_all([teacher, owner, lesson])
    db.flush()
    db.add(Slide(id=new_id("sld"), lesson_id=lesson.id, index=1, title="Hello", body="print()"))
    db.flush()
    sync_teacher_assignments(db, teacher)
    db.flush()
    return teacher, owner, lesson


def _ask(c, holder, teacher, lesson, monkeypatch, provider):
    monkeypatch.setattr(ai, "get_provider", lambda: provider)
    holder["user"] = teacher
    return c.post("/api/ai/chat/stream", json={"message": "hi", "lessonId": lesson.id})


def test_a_refusal_no_longer_blames_the_length_of_the_question(client, people, monkeypatch):
    c, holder = client
    teacher, _, lesson = people

    reply = _ask(c, holder, teacher, lesson, monkeypatch, _Refuses())

    assert "couldn't answer that" in reply.text
    assert "shorter" not in reply.text


def test_the_owner_sees_the_failure_and_the_teacher_does_not_get_it_back(
    db, client, people, monkeypatch
):
    c, holder = client
    teacher, owner, lesson = people
    _ask(c, holder, teacher, lesson, monkeypatch, _Refuses())

    holder["user"] = owner
    seen = c.get(
        "/api/chat/messages", params={"lessonId": lesson.id, "teacherId": teacher.id}
    ).json()
    assert [m["role"] for m in seen] == ["user", FAILURE_ROLE]
    assert "couldn't answer that" in seen[1]["content"]

    # Her reloaded thread becomes the history sent to the model, so an error
    # sentence must not be in it.
    holder["user"] = teacher
    hers = c.get("/api/chat/messages", params={"lessonId": lesson.id}).json()
    assert [m["role"] for m in hers] == ["user"]

    # And the count on the owner's list is still of things that were said.
    holder["user"] = owner
    threads = c.get("/api/chat/threads", params={"teacherId": teacher.id}).json()
    assert threads[0]["messageCount"] == 1


def test_an_answer_cut_off_partway_records_both(db, client, people, monkeypatch):
    c, holder = client
    teacher, _, lesson = people

    _ask(c, holder, teacher, lesson, monkeypatch, _DiesPartWay())

    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.teacher_id == teacher.id)
        .order_by(ChatMessage.created_at)
    ).all()
    assert [r.role for r in rows] == ["user", "assistant", FAILURE_ROLE]
    assert rows[1].content == "The first half"
