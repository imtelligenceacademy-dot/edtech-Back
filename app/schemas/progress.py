from __future__ import annotations

from datetime import datetime

from app.models.enums import LessonStatus, WatchdogStatus
from app.schemas.base import CamelModel


class ProgressOut(CamelModel):
    id: str
    teacher_id: str
    lesson_id: str
    status: LessonStatus
    percent_complete: int
    last_opened_at: datetime | None = None
    watchdog: WatchdogStatus
    watchdog_message: str | None = None


class ProgressUpdate(CamelModel):
    """Teacher self-reports where they stopped, or marks the lesson complete."""

    slide: int | None = None  # 1-based slide they reached
    total: int | None = None  # total slides (from the viewer), if known
    complete: bool = False
