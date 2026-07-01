from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin
from app.models.enums import Role, UserStatus

if TYPE_CHECKING:
    from app.models.school import School
    from app.models.token import RefreshToken


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    # Argon2id hash — never the plaintext.
    password_hash: Mapped[str] = mapped_column(String, nullable=False)

    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False), nullable=False, default=UserStatus.pending
    )

    school_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("schools.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Grades a teacher is assigned to teach, e.g. ["KG1","G1","G2"]. Empty for
    # non-teacher roles.
    grades: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Language of instruction for teachers: "en" | "fr" | "both". Null otherwise.
    language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Account-lockout bookkeeping.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    school: Mapped[Optional["School"]] = relationship(back_populates="users")
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
