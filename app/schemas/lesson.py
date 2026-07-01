from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from app.schemas.base import CamelModel


class SlideOut(CamelModel):
    id: str
    index: int
    title: str
    body: str
    image_url: str | None = None


class LessonOut(CamelModel):
    id: str
    title: str
    grade: int
    subject: str
    school_id: str | None = None
    language: str | None = None
    lesson_no: int | None = None
    due_date: date | None = None
    created_by: str | None = None
    file_id: str | None = None  # linked PDF, if any
    slides: list[SlideOut] = Field(default_factory=list)
    assigned_teacher_ids: list[str] = Field(default_factory=list)
    # Sequential-unlock state for the requesting teacher (None for admins).
    access_status: str | None = None  # available | completed | waiting | locked
    available_at: datetime | None = None
    access_message: str | None = None


class SlideCreate(CamelModel):
    index: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    image_url: str | None = None


class LessonCreate(CamelModel):
    title: str = Field(min_length=2, max_length=200)
    grade: int = Field(ge=1, le=12)
    subject: str = Field(min_length=1, max_length=80)
    school_id: str
    due_date: date | None = None
    slides: list[SlideCreate] = Field(default_factory=list)


class AssignmentRequest(CamelModel):
    teacher_id: str


# --- Super-admin lesson-access management --------------------------------- #
class TeacherLessonAccessRow(CamelModel):
    """One lesson in a teacher's track, with its gating state + override flag."""

    lesson_id: str
    title: str
    grade: int
    language: str | None = None
    lesson_no: int | None = None
    status: str  # available | completed | waiting | locked
    available_at: datetime | None = None
    percent_complete: int = 0
    completed_at: datetime | None = None
    unlocked_override: bool = False


class TeacherAccessTrack(CamelModel):
    grade: int
    language: str | None = None
    lessons: list[TeacherLessonAccessRow] = Field(default_factory=list)


class TeacherAccessOut(CamelModel):
    teacher_id: str
    teacher_name: str
    email: str
    school_id: str | None = None
    grades: list[str] = Field(default_factory=list)
    language: str | None = None
    tracks: list[TeacherAccessTrack] = Field(default_factory=list)


class OverrideRequest(CamelModel):
    unlocked: bool
