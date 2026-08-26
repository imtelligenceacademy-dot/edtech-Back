from __future__ import annotations

from datetime import datetime

from app.schemas.access_request import AccessRequestOut
from app.schemas.base import CamelModel
from app.schemas.file import UploadedFileOut
from app.schemas.security import SecurityLogOut
from app.schemas.user import UserOut


class StalledTeacher(CamelModel):
    """A teacher who hasn't opened a lesson in a while, or ever."""

    teacher_id: str
    name: str
    email: str
    school_id: str | None = None
    last_activity_at: datetime | None = None
    completed_count: int = 0


class SuperAdminOverview(CamelModel):
    """Everything the super-admin dashboard renders, computed server-side."""

    school_count: int
    teacher_count: int
    lesson_count: int
    pending_count: int
    pending_approvals: list[UserOut]
    security_alerts: list[SecurityLogOut]

    # --- Needs attention --------------------------------------------------- #
    # Each list is capped; the count beside it is the true total, so the UI can
    # say "8 of 23" rather than quietly under-reporting a backlog.
    access_requests: list[AccessRequestOut] = []
    access_request_count: int = 0
    stalled_teachers: list[StalledTeacher] = []
    stalled_teacher_count: int = 0
    unsorted_uploads: list[UploadedFileOut] = []
    unsorted_upload_count: int = 0
    stalled_after_days: int = 14
