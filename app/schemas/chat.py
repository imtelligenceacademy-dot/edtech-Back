from __future__ import annotations

from datetime import datetime

from app.schemas.base import CamelModel


class ChatMessageOut(CamelModel):
    id: str
    teacher_id: str
    lesson_id: str
    # The class it was said in. "" when the grade has one unnamed class.
    section: str = ""
    role: str
    content: str
    source_ref: str | None = None
    created_at: datetime


class ChatThreadOut(CamelModel):
    """One class's thread about one lesson, for the super-admin's list."""

    lesson_id: str
    lesson_title: str | None = None
    grade: int | None = None
    # Which class. "" when the grade has one unnamed class, and then the list
    # reads exactly as it did before classes existed.
    section: str = ""
    message_count: int
    last_message_at: datetime | None = None
