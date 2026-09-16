"""Restoring a backup must not destroy what the backup deliberately left out.

`chat_messages` is excluded from a backup on purpose — a year of conversations
would push the emailed dump past the provider's attachment limit, and none of it
is needed to bring the platform back. The restore then deleted every table and
refilled only the ones the payload carried, so the excluded table was emptied
and never refilled: an admin restoring Monday's backup lost every teacher's
assistant history, for every school, and was told "Database restored (N tables)".

Leaving `chat_messages` out of the delete sweep would not have been enough.
`teacher_id` is ON DELETE CASCADE, so emptying `users` takes the conversations
anyway, through the database.

These run against a throwaway file database rather than the suite's own, because
a restore truncates every table and the suite's is shared.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select

from app.database import Base
from app.models import ChatMessage, Lesson, School, User
from app.models.enums import Role, UserStatus
from app.services import backup as backup_service
from app.utils import new_id


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A real database of our own, with the module pointed at it.

    The FK pragma listener in app.database is registered on Engine itself, so
    this engine enforces ON DELETE CASCADE exactly as production does — which is
    the whole point: without the cascade the defect cannot be reproduced.
    """
    url = f"sqlite:///{(tmp_path / 'restore.db').as_posix()}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(backup_service, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed(engine, *, chats_on_extra_lesson: bool = False):
    """A school, a teacher, a lesson, and conversations about it."""
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        school = School(
            id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
        )
        session.add(school)
        session.flush()
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
        session.add(teacher)
        lesson = Lesson(
            id=new_id("les"),
            title="Grade 7 lesson 01",
            grade=7,
            subject="STEAM",
            language="en",
            year=2,
            lesson_no=1,
        )
        session.add(lesson)
        session.flush()

        for i in range(4):
            session.add(
                ChatMessage(
                    id=new_id("cm"),
                    teacher_id=teacher.id,
                    lesson_id=lesson.id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"turn {i}",
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.commit()
        return teacher.id, lesson.id


def _chat_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(ChatMessage.__table__)).scalar_one()


def test_a_restore_keeps_the_conversations_the_backup_left_out(sandbox):
    _seed(sandbox)
    assert _chat_count(sandbox) == 4, "precondition: there is history to lose"

    snapshot = backup_service._json_snapshot_bytes()
    # Precondition, and the reason this bug exists: the payload has no chats.
    assert b"chat_messages" not in snapshot

    backup_service._restore_json_database(snapshot)

    assert _chat_count(sandbox) == 4


def test_a_conversation_whose_teacher_is_not_in_the_backup_is_dropped_not_forced(sandbox):
    """A teacher hired after the backup was taken does not exist in it.

    Their conversations cannot come back without violating the foreign key that
    removed them, so they are dropped — the restore must not fail on them, and
    must not resurrect a row pointing at nobody.
    """
    from sqlalchemy.orm import Session

    _seed(sandbox)
    snapshot = backup_service._json_snapshot_bytes()

    # Hired after the snapshot, with a conversation of their own.
    with Session(sandbox) as session:
        lesson_id = session.scalars(select(Lesson.id)).first()
        newcomer = User(
            id=new_id("u"),
            name="Hired later",
            email=f"{new_id('e')}@example.com",
            password_hash="x",
            role=Role.teacher,
            status=UserStatus.active,
            school_id=session.scalars(select(School.id)).first(),
            grades=["G7"],
        )
        session.add(newcomer)
        session.flush()
        session.add(
            ChatMessage(
                id=new_id("cm"),
                teacher_id=newcomer.id,
                lesson_id=lesson_id,
                role="user",
                content="mine alone",
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    assert _chat_count(sandbox) == 5

    backup_service._restore_json_database(snapshot)

    # The four that still have a teacher survive; the newcomer's does not.
    assert _chat_count(sandbox) == 4
    with sandbox.connect() as conn:
        remaining = conn.execute(select(ChatMessage.__table__.c.content)).scalars().all()
    assert "mine alone" not in remaining


def test_the_restore_still_replaces_everything_it_is_supposed_to(sandbox):
    """The guard must not turn the restore into a no-op for the other tables."""
    from sqlalchemy.orm import Session

    _seed(sandbox)
    snapshot = backup_service._json_snapshot_bytes()

    with Session(sandbox) as session:
        session.add(
            School(
                id=new_id("sch"),
                name="Added after the backup",
                country="Lebanon",
                city="Tripoli",
                program_year=1,
            )
        )
        session.commit()

    backup_service._restore_json_database(snapshot)

    with Session(sandbox) as session:
        names = session.scalars(select(School.name)).all()
    assert "Added after the backup" not in names, "the restore has to roll the platform back"
    assert names == ["S"]
