from __future__ import annotations

from datetime import datetime

from app.schemas.base import CamelModel


class UploadedFileOut(CamelModel):
    id: str
    filename: str
    content_type: str
    size_bytes: int
    uploaded_by: str | None = None
    linked_lesson_id: str | None = None
    created_at: datetime


class UploadResult(CamelModel):
    """Returned after an upload: the stored file plus what was auto-assigned."""

    file: UploadedFileOut
    lesson_id: str | None = None
    lesson_title: str | None = None
    grade: str | None = None
    language: str | None = None
    assigned_count: int = 0
    teacher_names: list[str] = []
    note: str | None = None
