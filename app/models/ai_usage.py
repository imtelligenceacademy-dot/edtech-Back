from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow
from app.models.enums import Role


class AiUsage(Base):
    """One row per AI assistant interaction. Powers the "AI usage" dashboard
    metric (counts over a rolling window), scoped per school.
    """

    __tablename__ = "ai_usage"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    school_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("schools.id", ondelete="SET NULL"), nullable=True, index=True
    )
    role: Mapped[Optional[Role]] = mapped_column(
        Enum(Role, native_enum=False), nullable=True
    )
    # "teacher" (lesson assistant) or "admin" (school-operations assistant).
    kind: Mapped[str] = mapped_column(String, nullable=False, default="teacher")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
