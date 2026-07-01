from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.school import School
    from app.models.uploaded_file import UploadedFile
    from app.models.user import User


class Lesson(Base, TimestampMixin):
    __tablename__ = "lessons"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)

    # Curriculum lessons are global (not tied to one school); school_id stays
    # set only for legacy / school-authored lessons.
    school_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Language of the lesson material: "en" | "fr". Null for legacy lessons.
    language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)
    # Parsed lesson number from the source filename (e.g. 4), for dedup.
    lesson_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Number of slides/pages in the lesson PDF — denominator for progress %.
    slide_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    school: Mapped[Optional["School"]] = relationship(back_populates="lessons")
    slides: Mapped[list["Slide"]] = relationship(
        back_populates="lesson",
        cascade="all, delete-orphan",
        order_by="Slide.index",
    )
    assignments: Mapped[list["LessonAssignment"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    # PDFs linked to this lesson (most recent first). View-only — the owning
    # side is UploadedFile.linked_lesson_id.
    uploaded_files: Mapped[list["UploadedFile"]] = relationship(
        "UploadedFile",
        primaryjoin="Lesson.id == UploadedFile.linked_lesson_id",
        foreign_keys="UploadedFile.linked_lesson_id",
        order_by="UploadedFile.created_at.desc()",
        viewonly=True,
    )


class Slide(Base):
    __tablename__ = "slides"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    lesson_id: Mapped[str] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    lesson: Mapped["Lesson"] = relationship(back_populates="slides")


class LessonAssignment(Base, TimestampMixin):
    """Join table linking a lesson to a teacher (the 'Connection DB' edge)."""

    __tablename__ = "lesson_assignments"
    __table_args__ = (UniqueConstraint("lesson_id", "teacher_id", name="uq_lesson_teacher"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    lesson_id: Mapped[str] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # How the assignment was made: "rule" (auto grade+language) or "manual"
    # (Access Control override). Only rule-based ones are pruned automatically.
    source: Mapped[str] = mapped_column(String(8), nullable=False, default="rule")

    lesson: Mapped["Lesson"] = relationship(back_populates="assignments")
    teacher: Mapped["User"] = relationship()
