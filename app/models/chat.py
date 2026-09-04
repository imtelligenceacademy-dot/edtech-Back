from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow


class ChatMessage(Base):
    """One turn of a teacher's conversation with the lesson assistant.

    Threads are keyed by (teacher, lesson, section): a teacher opens one or two
    lessons a week per class, so the lesson is the unit they think in, and Grade
    7 Lesson 2 can never bleed into Grade 8 Lesson 2 — nor 6A's conversation
    about a lesson into 6B's. The same lesson taught to four classes is four
    conversations, because what was asked in one room is not what the teacher
    wants back when they walk into the next.

    Only real exchanges live here — the question and the answer. The app's own
    chatter ("Opening ...", lock explanations) is navigation feedback that would
    only bury what the teacher came back for.

    Deliberately excluded from the database backup: a year of these would push
    the emailed dump past the provider's attachment limit. There is a separate
    on-demand export instead.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_thread", "teacher_id", "lesson_id", "section", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[str] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The class this was said in. Empty string, not NULL, to match the rest of
    # the schema: a grade with a single unnamed class carries "" everywhere.
    section: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default=""
    )

    # "user" or "assistant" — the same two roles the chat UI renders.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # The lesson label the assistant answered against, shown as a chip in the UI.
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
