from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow
from app.models.enums import Role, SecurityEvent, SecurityStatus


class SecurityLog(Base):
    __tablename__ = "security_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    role: Mapped[Optional[Role]] = mapped_column(Enum(Role, native_enum=False), nullable=True)
    school_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("schools.id", ondelete="SET NULL"), nullable=True, index=True
    )

    ip: Mapped[str] = mapped_column(String, nullable=False, default="")
    device: Mapped[str] = mapped_column(String, nullable=False, default="")
    location_label: Mapped[str] = mapped_column(String, nullable=False, default="")
    location_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    location_lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    event: Mapped[SecurityEvent] = mapped_column(
        Enum(SecurityEvent, native_enum=False), nullable=False
    )
    status: Mapped[SecurityStatus] = mapped_column(
        Enum(SecurityStatus, native_enum=False), nullable=False
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
