from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class AccessRequest(Base, TimestampMixin):
    """A teacher's request for the super-admin to unlock a locked lesson (e.g.
    one still inside its waiting period). Resolving a pending request as
    'granted' sets the matching ``Progress.unlocked_override``.
    """

    __tablename__ = "access_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    teacher_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[str] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # pending | granted | denied
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    resolved_by: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
