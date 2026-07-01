from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin
from app.models.enums import LessonStatus, WatchdogStatus


class Progress(Base, TimestampMixin):
    __tablename__ = "progress"
    __table_args__ = (UniqueConstraint("teacher_id", "lesson_id", name="uq_teacher_lesson"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[str] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[LessonStatus] = mapped_column(
        Enum(LessonStatus, native_enum=False), nullable=False, default=LessonStatus.not_started
    )
    percent_complete: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # When the teacher marked the lesson complete. Drives the per-track
    # "wait a week before the next lesson unlocks" gate.
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Super-admin override: when True, this lesson is always available to the
    # teacher regardless of sequencing/wait (also re-opens a completed lesson).
    unlocked_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    watchdog: Mapped[WatchdogStatus] = mapped_column(
        Enum(WatchdogStatus, native_enum=False), nullable=False, default=WatchdogStatus.not_opened
    )
    watchdog_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    teacher: Mapped["object"] = relationship("User")
    lesson: Mapped["object"] = relationship("Lesson")
