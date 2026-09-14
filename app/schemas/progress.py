from __future__ import annotations

from datetime import datetime

from pydantic import Field

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

    # Bounded. `total` is copied onto the shared curriculum lesson, so an
    # unbounded value from one teacher's client redefined the denominator for
    # every teacher in every school on that lesson — a negative one made every
    # colleague's percentage zero for good.
    # ge=0, not ge=1: the handler already reads 0 as "not known yet", which is
    # what the presenting bar sends before the projector has reported its page
    # count — rejecting it would refuse a legitimate "Mark complete". What is
    # refused is the negative and the absurd.
    slide: int | None = Field(default=None, ge=0, le=5000)
    total: int | None = Field(default=None, ge=0, le=5000)
    complete: bool = False
    # The class being taught. Omitted by single-class teachers (and by any
    # client predating sections), in which case the server uses their only one.
    section: str | None = None
