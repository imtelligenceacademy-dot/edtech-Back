from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow


class SlideReading(Base):
    """What a vision model saw on one slide, written down once and kept.

    A slide's picture never changes, so reading it is a fact about the file, not
    about the question being asked. Without this row every teacher question
    about slide 4 would pay for the same image to be read again — the difference
    between one call per slide for the life of the PDF and one call per message.

    Keyed on the file rather than the lesson: re-uploading a PDF creates a new
    file, which is exactly when a fresh reading is wanted.
    """

    __tablename__ = "slide_readings"
    __table_args__ = (UniqueConstraint("file_id", "page", name="uq_slide_reading"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    file_id: Mapped[str] = mapped_column(
        ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page: Mapped[int] = mapped_column(Integer, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Which model produced it, so a reading made by a model you have since
    # replaced is recognisable rather than silently trusted forever.
    model: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
