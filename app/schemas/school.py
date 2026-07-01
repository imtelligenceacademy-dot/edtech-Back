from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.base import CamelModel


class SchoolBrief(CamelModel):
    """Minimal, public-safe school info for the signup dropdown."""

    id: str
    name: str


class SchoolOut(CamelModel):
    id: str
    name: str
    country: str
    city: str
    teacher_count: int = 0
    admin_count: int = 0
    created_at: datetime | None = None


class SchoolCreate(CamelModel):
    name: str = Field(min_length=2, max_length=160)
    country: str = Field(default="", max_length=80)
    city: str = Field(default="", max_length=80)


class SchoolUpdate(CamelModel):
    """All fields optional — only provided fields are changed (partial update)."""

    name: str | None = Field(default=None, min_length=2, max_length=160)
    country: str | None = Field(default=None, max_length=80)
    city: str | None = Field(default=None, max_length=80)
