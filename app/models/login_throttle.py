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

    # When the current run of cycles began. `cycle_count` is the only route to a
    # network ban, so something has to let it decay; without this it could only
    # ever be cleared by a successful sign-in, which is what made the ban both
    # unreachable and, in the other direction, permanent once reached.
    cycle_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The last time anybody signed in successfully from this address. An address
    # that is signing teachers in is a school, not an attack, however many
    # failures are mixed in with it — and a school must not be banned for a day.
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
