"""Nobody is charged a question for an answer they did not get.

The allowance is small — five an hour for an admin — so a question spent on
nothing is not an accounting detail, it is a fifth of the hour. And the way it
went wrong was self-reinforcing: with the provider chain down, every attempt
failed, every failure still cost a question, and the fifth attempt was refused
for an hour on the strength of four answers that were never given.

The teacher chat already had this. The admin chat and both report builders did
not, and the admin chat sits directly beside the teacher one with a comment
above the teacher's refund explaining why it is there.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models import AiUsage, School, User
from app.models.enums import Role, UserStatus
from app.routers import ai as ai_router
from app.schemas.ai import AdminChatRequest
from app.services.llm import LLMError
from app.utils import new_id


class _Dead:
    """Every provider in the chain is down. The shape of a bad afternoon."""

    name = "openai"
    model = "m"
    supports_vision = False

    def chat(self, system, messages):
        raise LLMError("unavailable", "no provider answered")

    def chat_stream(self, system, messages):
        raise LLMError("unavailable", "no provider answered")
        yield ""  # pragma: no cover - generator marker


class _Answering:
    name = "openai"
    model = "m"
    supports_vision = False

    def chat(self, system, messages):
        return "## What needs attention\nNothing this week."

    def chat_stream(self, system, messages):
        yield "here is your answer"


@pytest.fixture()
def admin(db) -> User:
    school = School(id=new_id("sch"), name="Refund School", program_year=2)
    db.add(school)
    db.flush()
    user = User(
        id=new_id("u"),
        name="Head",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.school_admin,
        status=UserStatus.active,
        school_id=school.id,
        grades=[],
    )
    db.add(user)
    db.commit()
    return user


def _charges(db, user: User) -> int:
    """Questions standing against this account. A refund deletes the row."""
    db.expire_all()
    return db.query(AiUsage).filter(AiUsage.user_id == user.id).count()


def _drain(response) -> str:
    """A StreamingResponse's body. The iterator is async even when we are not."""

    async def _collect():
        # Starlette yields str here and bytes elsewhere; accept either rather
        # than depending on which.
        return "".join(
            [
                chunk.decode() if isinstance(chunk, bytes) else chunk
                async for chunk in response.body_iterator
            ]
        )

    return asyncio.run(_collect())


# --------------------------------------------------------------------------- #
# The school-admin assistant
# --------------------------------------------------------------------------- #
def test_an_admin_question_that_returns_nothing_is_not_charged(db, admin, monkeypatch):
    monkeypatch.setattr(ai_router, "get_provider", lambda: _Dead())
    before = _charges(db, admin)

    body = _drain(
        ai_router.admin_chat_stream(
            payload=AdminChatRequest(message="How are we doing?"), db=db, current=admin
        )
    )

    assert "error" in body, "precondition: the attempt failed"
    assert _charges(db, admin) == before, (
        "the admin was told nothing and must not have spent a question on it"
    )


def test_an_admin_question_that_is_answered_is_charged(db, admin, monkeypatch):
    """The other half. A refund that fires on success is a free assistant."""
    monkeypatch.setattr(ai_router, "get_provider", lambda: _Answering())
    before = _charges(db, admin)

    body = _drain(
        ai_router.admin_chat_stream(
            payload=AdminChatRequest(message="How are we doing?"), db=db, current=admin
        )
    )

    assert "here is your answer" in body
    assert _charges(db, admin) == before + 1


def test_a_failed_admin_question_says_which_failure_it_was(db, admin, monkeypatch):
    """"Busy, try in a moment" and "not configured, tell your administrator" ask
    for different things. A flat "AI assistant unavailable" asked for neither,
    and the person reading it here is often the administrator."""
    class _Busy(_Dead):
        def chat_stream(self, system, messages):
            raise LLMError("rate_limit", "slow down")
            yield ""  # pragma: no cover

    monkeypatch.setattr(ai_router, "get_provider", lambda: _Busy())

    body = _drain(
        ai_router.admin_chat_stream(
            payload=AdminChatRequest(message="How are we doing?"), db=db, current=admin
        )
    )

    assert "busy" in body.lower()


# --------------------------------------------------------------------------- #
# The reports
# --------------------------------------------------------------------------- #
def test_a_report_with_no_narrative_is_not_charged(db, admin, monkeypatch):
    """The report still builds — the tables under the narrative are the point of
    it — but the narrative is then a hard-coded apology, and charging a question
    for a constant string is how three attempts at a report cost three of five."""
    monkeypatch.setattr(ai_router, "get_provider", lambda: _Dead())
    before = _charges(db, admin)

    ai_router.admin_report(db=db, current=admin)

    assert _charges(db, admin) == before


def test_a_report_with_a_narrative_is_charged(db, admin, monkeypatch):
    monkeypatch.setattr(ai_router, "get_provider", lambda: _Answering())
    before = _charges(db, admin)

    ai_router.admin_report(db=db, current=admin)

    assert _charges(db, admin) == before + 1
