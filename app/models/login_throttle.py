from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class LoginThrottle(Base, TimestampMixin):
    __tablename__ = "login_throttles"

    ip: Mapped[str] = mapped_column(String, primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cycle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")
