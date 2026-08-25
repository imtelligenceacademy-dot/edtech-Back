from __future__ import annotations

from datetime import datetime

from app.schemas.base import CamelModel


class ChatMessageOut(CamelModel):
    id: str
    teacher_id: str
    lesson_id: str
    role: str
    content: str
    source_ref: str | None = None
    created_at: datetime


class ChatThreadOut(CamelModel):
    """One lesson's thread, for the super-admin's list."""

    lesson_id: str
    lesson_title: str | None = None
    grade: int | None = None
    message_count: int
    last_message_at: datetime | None = None
