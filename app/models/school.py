from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.lesson import Lesson
    from app.models.user import User


class School(Base, TimestampMixin):
    __tablename__ = "schools"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    country: Mapped[str] = mapped_column(String, nullable=False, default="")
    city: Mapped[str] = mapped_column(String, nullable=False, default="")

    # Curriculum year the school is currently on: new schools start on Year 1 and
    # a super-admin promotes them to Year 2 later. Determines which year's lessons
    # its teachers receive. Existing schools were backfilled to 2.
    program_year: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    users: Mapped[list["User"]] = relationship(back_populates="school")
    lessons: Mapped[list["Lesson"]] = relationship(back_populates="school")
