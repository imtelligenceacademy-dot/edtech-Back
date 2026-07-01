from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.base import CamelModel


class AccessRequestCreate(CamelModel):
    lesson_id: str
    note: str | None = Field(default=None, max_length=500)


class AccessRequestOut(CamelModel):
    id: str
    teacher_id: str
    teacher_name: str
    lesson_id: str
    lesson_title: str
    grade: int
    language: str | None = None
    lesson_no: int | None = None
    status: str  # pending | granted | denied
    note: str | None = None
    created_at: datetime
