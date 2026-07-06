from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class FairProject(Base, TimestampMixin):
    """An ICT Fair project PDF, shown to teachers who have `ict_fair_access`.

    Deliberately outside the lesson/sequencing/auto-assign pipeline: fair
    projects have no grade track, no unlock sequence, and no completion — they
    are simply viewable resources gated by the teacher's account flag.
    """

    __tablename__ = "fair_projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    # The stored PDF backing this project.
    file_id: Mapped[str | None] = mapped_column(
        ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    uploaded_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
