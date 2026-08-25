"""Teacher chat history: who can read it, and what it is scoped to.

The access rules are the point of these tests. A school admin monitors progress,
never conversations — and this codebase has already shipped one scoping bug of
that shape (`school_id == None` rendering as IS NULL and failing open), so the
refusal is asserted rather than assumed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from fastapi import HTTPException

from app.models import ChatMessage, Lesson, User
from app.models.enums import Role, UserStatus
from app.routers.chat import clear_messages, export_chats, list_messages, list_threads
from app.services.backup import _json_snapshot_bytes
from app.services.chat_history import purge_expired, save_exchange
from app.utils import new_id


def _user(db, role: Role, name: str) -> User:
    user = User(
        id=new_id("u"),
        name=name,
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=role,
        status=UserStatus.active,
        grades=[],
    )
    db.add(user)
    db.commit()
    return user


def _lesson(db, title: str, grade: int = 7) -> Lesson:
    lesson = Lesson(id=new_id("les"), title=title, grade=grade, subject="STEAM")
    db.add(lesson)
    db.commit()
    return lesson


def _drain(response) -> str:
    """Collect a StreamingResponse's body. Its iterator is async even when the
    generator behind it is not, so run it on a loop."""

    async def collect() -> str:
        chunks = [chunk async for chunk in response.body_iterator]
        return "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in chunks
        )

    return anyio.run(collect)


def _say(db, teacher: User, lesson: Lesson, text: str, *, age_days: int = 0) -> None:
    db.add(
        ChatMessage(
            id=new_id("msg"),
            teacher_id=teacher.id,
            lesson_id=lesson.id,
            role="user",
            content=text,
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )
    )
    db.commit()


def test_school_admin_is_refused_everywhere(db):
    teacher = _user(db, Role.teacher, "teacher")
    lesson = _lesson(db, "Grade 7 micro:bit lesson 01")
    admin = _user(db, Role.school_admin, "principal")
    _say(db, teacher, lesson, "how do I wire the buzzer?")

    for call in (
        lambda: list_messages(lesson_id=lesson.id, teacher_id=teacher.id, limit=50, db=db, current=admin),
        lambda: list_messages(lesson_id=lesson.id, teacher_id=None, limit=50, db=db, current=admin),
        lambda: list_threads(teacher_id=teacher.id, db=db, current=admin),
        lambda: clear_messages(lesson_id=lesson.id, db=db, current=admin),
        lambda: export_chats(current=admin),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403


def test_teacher_reads_only_their_own(db):
    mine = _user(db, Role.teacher, "mine")
    theirs = _user(db, Role.teacher, "theirs")
    lesson = _lesson(db, "Grade 8 python lesson 02")
    _say(db, mine, lesson, "mine")
    _say(db, theirs, lesson, "theirs")

    got = list_messages(lesson_id=lesson.id, teacher_id=None, limit=50, db=db, current=mine)
    assert [m.content for m in got] == ["mine"]

    # Asking for someone else's is refused, not silently ignored.
    with pytest.raises(HTTPException) as exc:
        list_messages(lesson_id=lesson.id, teacher_id=theirs.id, limit=50, db=db, current=mine)
    assert exc.value.status_code == 403


def test_threads_are_scoped_per_lesson(db):
    teacher = _user(db, Role.teacher, "teacher")
    seven = _lesson(db, "Grade 7 micro:bit lesson 02", grade=7)
    eight = _lesson(db, "Grade 8 micro:bit lesson 02", grade=8)
    _say(db, teacher, seven, "about grade 7")
    _say(db, teacher, eight, "about grade 8")

    got = list_messages(lesson_id=seven.id, teacher_id=None, limit=50, db=db, current=teacher)
    assert [m.content for m in got] == ["about grade 7"]

    threads = list_threads(teacher_id=None, db=db, current=teacher)
    assert {t.lesson_id for t in threads} == {seven.id, eight.id}
    assert all(t.message_count == 1 for t in threads)


def test_super_admin_can_read_a_teachers_thread(db):
    teacher = _user(db, Role.teacher, "teacher")
    boss = _user(db, Role.super_admin, "owner")
    lesson = _lesson(db, "Grade 9 micro:bit lesson 03")
    _say(db, teacher, lesson, "a question")

    got = list_messages(lesson_id=lesson.id, teacher_id=teacher.id, limit=50, db=db, current=boss)
    assert [m.content for m in got] == ["a question"]

    assert any(
        json.loads(line)["content"] == "a question"
        for line in _drain(export_chats(current=boss)).splitlines()
    )


def test_saving_an_exchange_keeps_question_and_answer_in_order(db):
    teacher = _user(db, Role.teacher, "teacher")
    lesson = _lesson(db, "Grade 7 micro:bit lesson 04")

    save_exchange(
        teacher_id=teacher.id,
        lesson_id=lesson.id,
        question="what is a pull-up resistor?",
        answer="It holds the pin high until the button pulls it low.",
        source_ref=lesson.title,
    )
    got = list_messages(lesson_id=lesson.id, teacher_id=None, limit=50, db=db, current=teacher)
    assert [m.role for m in got] == ["user", "assistant"]
    assert got[1].source_ref == lesson.title

    # A question with no lesson in context is not worth keeping: the assistant
    # refuses it, so there is nothing to come back to.
    before = db.query(ChatMessage).count()
    save_exchange(
        teacher_id=teacher.id, lesson_id=None, question="hello?", answer="", source_ref=None
    )
    assert db.query(ChatMessage).count() == before


def test_clearing_a_thread_leaves_other_lessons_alone(db):
    teacher = _user(db, Role.teacher, "teacher")
    one = _lesson(db, "Grade 7 micro:bit lesson 05")
    two = _lesson(db, "Grade 7 micro:bit lesson 06")
    _say(db, teacher, one, "first")
    _say(db, teacher, two, "second")

    clear_messages(lesson_id=one.id, db=db, current=teacher)

    assert list_messages(lesson_id=one.id, teacher_id=None, limit=50, db=db, current=teacher) == []
    assert len(list_messages(lesson_id=two.id, teacher_id=None, limit=50, db=db, current=teacher)) == 1


def test_retention_purge_drops_only_what_is_past_the_window(db):
    teacher = _user(db, Role.teacher, "teacher")
    lesson = _lesson(db, "Grade 7 micro:bit lesson 07")
    _say(db, teacher, lesson, "old", age_days=400)
    _say(db, teacher, lesson, "recent", age_days=10)

    deleted = purge_expired(db, retention_days=365)
    assert deleted == 1
    remaining = list_messages(lesson_id=lesson.id, teacher_id=None, limit=50, db=db, current=teacher)
    assert [m.content for m in remaining] == ["recent"]

    # 0 disables the purge entirely.
    assert purge_expired(db, retention_days=0) == 0


def test_chat_is_not_in_the_database_backup(db):
    teacher = _user(db, Role.teacher, "teacher")
    lesson = _lesson(db, "Grade 7 micro:bit lesson 08")
    _say(db, teacher, lesson, "should not be in the dump")

    payload = json.loads(_json_snapshot_bytes())
    assert "chat_messages" not in payload["tables"]
    assert "should not be in the dump" not in _json_snapshot_bytes().decode("utf-8")
