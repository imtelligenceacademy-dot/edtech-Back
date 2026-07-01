from __future__ import annotations

from app.schemas.base import CamelModel
from app.schemas.security import SecurityLogOut
from app.schemas.user import UserOut


class SuperAdminOverview(CamelModel):
    """Everything the super-admin dashboard renders, computed server-side."""

    school_count: int
    teacher_count: int
    lesson_count: int
    pending_count: int
    pending_approvals: list[UserOut]
    security_alerts: list[SecurityLogOut]
