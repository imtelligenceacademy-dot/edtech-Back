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


class FileSelection(CamelModel):
    """A set of uploaded-file ids the admin has selected on the Files page."""

    file_ids: list[str] = []
    # Only used when zipping: names the download after what was selected
    # ("year-2-grade-7-en"). Sanitised server-side.
    label: str | None = None


class DeletionImpact(CamelModel):
    """What a selection would actually destroy.

    Deleting a lesson PDF deletes the whole lesson, and the lesson cascades into
    every teacher's assignment, progress, chat and access request for it. The
    admin used to confirm that blind; these are the counts behind the warning.
    """

    files: int = 0
    lessons: int = 0
    teachers: int = 0
    assignments: int = 0
    progress: int = 0
    chat_messages: int = 0
    access_requests: int = 0
    # Lessons that at least one teacher has already started or finished — the
    # ones worth hesitating over.
    lessons_in_progress: int = 0
    lesson_titles: list[str] = []
    missing: int = 0


class BulkDeleteResult(CamelModel):
    deleted_files: int = 0
    deleted_lessons: int = 0


class UploadPreviewRequest(CamelModel):
    """Filenames only — the preview never uploads anything."""

    filenames: list[str] = []
    language: str = "en"
    year: int = 2


class UploadPreviewRow(CamelModel):
    filename: str
    ok: bool
    note: str | None = None
    lesson_title: str | None = None
    grade: int | None = None
    course: str | None = None
    lesson_no: int | None = None
    existing_lesson: bool = False
    teacher_names: list[str] = []
