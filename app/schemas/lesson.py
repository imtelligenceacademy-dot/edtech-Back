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
    year: int | None = None
    course: str | None = None  # "python" | "microbit" | null
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


class AssignmentSet(CamelModel):
    """Who, within one school, is assigned to a lesson — the whole set at once.

    Scoped to a school because the Access Control page only ever edits one
    school's teachers, and must not disturb another school's assignments.
    """

    school_id: str
    teacher_ids: list[str] = Field(default_factory=list)


class BulkAssignment(CamelModel):
    """The same edit applied across many lessons at once.

    A term's worth of lessons is 40 rows; handing them to a teacher one lesson
    at a time is 40 trips through the page. This carries the whole rectangle —
    these lessons, these teachers, added or removed — as one request.

    Add and remove are named separately rather than derived from a desired set:
    the admin is editing a selection whose teachers already differ lesson to
    lesson, and "give all of them to Claudia" must not quietly strip whoever
    else happened to have one of them.
    """

    school_id: str
    lesson_ids: list[str] = Field(default_factory=list)
    add_teacher_ids: list[str] = Field(default_factory=list)
    remove_teacher_ids: list[str] = Field(default_factory=list)


class BulkAssignmentPreview(CamelModel):
    """What a bulk edit would change, before it is applied."""

    lessons: int = 0
    adds: int = 0
    removes: int = 0
    # Removals that throw away work a teacher has already done. Removing an
    # assignment deletes that teacher's progress row for the lesson.
    progress_lost: int = 0
    teachers_losing_progress: list[str] = Field(default_factory=list)


class BulkAssignmentResult(CamelModel):
    lessons_touched: int = 0
    assignments_added: int = 0
    assignments_removed: int = 0
    # The affected lessons, so the page can refresh without refetching all 400.
    lessons: list[LessonOut] = Field(default_factory=list)


# --- Super-admin lesson-access management --------------------------------- #
class TeacherLessonAccessRow(CamelModel):
    """One lesson in a teacher's track, with its gating state + override flag."""

    lesson_id: str
    title: str
    grade: int
    language: str | None = None
    course: str | None = None
    lesson_no: int | None = None
    status: str  # available | completed | waiting | locked
    available_at: datetime | None = None
    percent_complete: int = 0
    completed_at: datetime | None = None
    unlocked_override: bool = False


class TeacherAccessTrack(CamelModel):
    grade: int
    language: str | None = None
    year: int | None = None
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
