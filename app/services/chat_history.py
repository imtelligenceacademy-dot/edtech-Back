"""Storage for the teacher assistant's conversations, one thread per lesson.

Writing happens from inside the streaming response, after the request's own DB
session has been closed, so these helpers open their own session and never
raise into the stream: losing a saved message must never cost the teacher the
answer they are reading.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import ChatMessage
from app.utils import new_id

logger = logging.getLogger("app.chat")


def save_exchange(
    *,
    teacher_id: str,
    lesson_id: str | None,
    question: str,
    answer: str,
    source_ref: str | None,
) -> None:
    """Record one question and its reply against the lesson it was asked about.

    A question with no lesson in context is not stored — the assistant refuses
    those, so there is nothing worth coming back to.
    """
    if not lesson_id or not question.strip():
        return

    try:
        with SessionLocal() as db:
            now = datetime.now(timezone.utc)
            db.add(
                ChatMessage(
                    id=new_id("msg"),
                    teacher_id=teacher_id,
                    lesson_id=lesson_id,
                    role="user",
                    content=question,
                    created_at=now,
                )
            )
            if answer.strip():
                db.add(
                    ChatMessage(
                        id=new_id("msg"),
                        teacher_id=teacher_id,
                        lesson_id=lesson_id,
                        role="assistant",
                        content=answer,
                        source_ref=source_ref,
                        # A microsecond later, so the pair always reads in order.
                        created_at=now + timedelta(microseconds=1),
                    )
                )
            db.commit()
    except Exception:
        # The teacher already has their answer on screen; a failed write is a
        # problem for us, not for the lesson in progress.
        logger.exception("could not save chat exchange")


def thread_messages(
    db: Session, *, teacher_id: str, lesson_id: str, limit: int = 50
) -> list[ChatMessage]:
    """The tail of one thread, oldest first — the order the chat renders in."""
    newest = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.teacher_id == teacher_id, ChatMessage.lesson_id == lesson_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    ).all()
    return list(reversed(newest))


def clear_thread(db: Session, *, teacher_id: str, lesson_id: str) -> int:
    result = db.execute(
        delete(ChatMessage).where(
            ChatMessage.teacher_id == teacher_id, ChatMessage.lesson_id == lesson_id
        )
    )
    db.commit()
    return result.rowcount or 0


def threads_for_teacher(db: Session, *, teacher_id: str) -> list[dict]:
    """Which lessons this teacher has chats for, most recent first."""
    rows = db.execute(
        select(
            ChatMessage.lesson_id,
            func.count(ChatMessage.id),
            func.max(ChatMessage.created_at),
        )
        .where(ChatMessage.teacher_id == teacher_id)
        .group_by(ChatMessage.lesson_id)
    ).all()
    threads = [
        {"lesson_id": lesson_id, "message_count": count, "last_message_at": last}
        for lesson_id, count, last in rows
    ]
    threads.sort(key=lambda t: t["last_message_at"] or datetime.min, reverse=True)
    return threads


def purge_expired(db: Session, *, retention_days: int | None = None) -> int:
    """Delete messages older than the retention window. Returns the row count.

    Lowering the window deletes everything newly outside it on the next run,
    and that is not recoverable — the setting is deliberately a single number
    so the consequence is easy to reason about.
    """
    days = settings.chat_retention_days if retention_days is None else retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(delete(ChatMessage).where(ChatMessage.created_at < cutoff))
    db.commit()
    return result.rowcount or 0
