"""Teacher chat history — one thread per (teacher, lesson).

Access is stated at every endpoint rather than inferred: a teacher is pinned to
their own rows, the super-admin may read anyone's, and every other role is
refused. School admins monitor progress, not conversations.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.deps import get_current_user
from app.models import ChatMessage, Lesson, User
from app.models.enums import Role
from app.schemas.chat import ChatMessageOut, ChatThreadOut
from app.services.chat_history import clear_thread, thread_messages, threads_for_teacher

router = APIRouter(prefix="/api/chat", tags=["chat"])

_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Teacher conversations are private to the teacher.",
)


def _resolve_subject(current: User, teacher_id: str | None) -> str:
    """Whose history is being asked for, or 403.

    A teacher may only ever read their own — passing someone else's id is
    refused rather than quietly ignored, so a bug in the caller can't widen
    what comes back.
    """
    if current.role == Role.teacher:
        if teacher_id and teacher_id != current.id:
            raise _FORBIDDEN
        return current.id
    if current.role == Role.super_admin:
        return teacher_id or current.id
    raise _FORBIDDEN


@router.get("/messages", response_model=list[ChatMessageOut])
def list_messages(
    lesson_id: str = Query(..., alias="lessonId"),
    teacher_id: str | None = Query(None, alias="teacherId"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[ChatMessage]:
    """One lesson's thread, oldest first.

    Deliberately independent of the lesson's unlock state: a lesson a teacher
    has finished is locked again, and their own notes about it should not lock
    with it.
    """
    subject = _resolve_subject(current, teacher_id)
    return thread_messages(db, teacher_id=subject, lesson_id=lesson_id, limit=limit)


@router.delete(
    "/messages", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def clear_messages(
    lesson_id: str = Query(..., alias="lessonId"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """A teacher clears one lesson's thread. Only ever their own."""
    if current.role != Role.teacher:
        raise _FORBIDDEN
    clear_thread(db, teacher_id=current.id, lesson_id=lesson_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/threads", response_model=list[ChatThreadOut])
def list_threads(
    teacher_id: str | None = Query(None, alias="teacherId"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[ChatThreadOut]:
    """Which lessons a teacher has talked about, most recent first."""
    subject = _resolve_subject(current, teacher_id)
    threads = threads_for_teacher(db, teacher_id=subject)
    if not threads:
        return []

    lessons = {
        lesson.id: lesson
        for lesson in db.scalars(
            select(Lesson).where(Lesson.id.in_([t["lesson_id"] for t in threads]))
        )
    }
    return [
        ChatThreadOut(
            lesson_id=t["lesson_id"],
            lesson_title=getattr(lessons.get(t["lesson_id"]), "title", None),
            grade=getattr(lessons.get(t["lesson_id"]), "grade", None),
            message_count=t["message_count"],
            last_message_at=t["last_message_at"],
        )
        for t in threads
    ]


@router.get("/export")
def export_chats(current: User = Depends(get_current_user)) -> StreamingResponse:
    """Every message, as JSON Lines.

    Streamed a row at a time on purpose: chat is kept out of the database
    backup precisely because a year of it does not belong in memory, and this
    export would have the same problem if it built one big document.
    """
    if current.role != Role.super_admin:
        raise _FORBIDDEN

    def rows() -> Iterator[str]:
        with SessionLocal() as db:
            stmt = select(ChatMessage).order_by(
                ChatMessage.teacher_id, ChatMessage.lesson_id, ChatMessage.created_at
            )
            for message in db.scalars(stmt).yield_per(500):
                yield json.dumps(
                    {
                        "teacherId": message.teacher_id,
                        "lessonId": message.lesson_id,
                        "role": message.role,
                        "content": message.content,
                        "createdAt": message.created_at.isoformat()
                        if message.created_at
                        else None,
                    },
                    ensure_ascii=False,
                ) + "\n"

    return StreamingResponse(
        rows(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="im-telligence-chats.jsonl"'
        },
    )
