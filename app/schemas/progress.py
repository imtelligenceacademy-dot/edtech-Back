from __future__ import annotations

from datetime import datetime

from app.models.enums import LessonStatus, WatchdogStatus
from app.schemas.base import CamelModel


class ProgressOut(CamelModel):
    id: str
    teacher_id: str
    lesson_id: str
    # Which class this row tracks. "" when the grade has a single, unnamed
    # section, which is every teacher who takes one class per grade.
    section: str = ""
    status: LessonStatus
    percent_complete: int
    # Where the teacher actually stopped. Null for lessons last saved before
    # slide positions were recorded.
    last_slide: int | None = None
    slide_total: int | None = None
    last_opened_at: datetime | None = None
    watchdog: WatchdogStatus
    watchdog_message: str | None = None


class ProgressUpdate(CamelModel):
    """Teacher self-reports where they stopped, or marks the lesson complete."""

    slide: int | None = None  # 1-based slide they reached
    total: int | None = None  # total slides (from the viewer), if known
    complete: bool = False
    # The class being taught. Omitted by single-class teachers (and by any
    # client predating sections), in which case the server uses their only one.
    section: str | None = None
