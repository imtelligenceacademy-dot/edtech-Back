"""Aggregated dashboard endpoints. Counts and recent items are computed in the
database and scoped server-side, so the browser never pulls full tables just to
derive a few numbers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_roles
from app.models import Lesson, School, SecurityLog, User
from app.models.enums import Role, SecurityStatus, UserStatus
from app.schemas.dashboard import SuperAdminOverview

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

ALERT_LIMIT = 5
PENDING_LIMIT = 20


@router.get("/super-admin", response_model=SuperAdminOverview)
def super_admin_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(Role.super_admin)),
) -> SuperAdminOverview:
    school_count = db.scalar(select(func.count(School.id))) or 0
    teacher_count = (
        db.scalar(select(func.count(User.id)).where(User.role == Role.teacher)) or 0
    )
    lesson_count = db.scalar(select(func.count(Lesson.id))) or 0
    pending_count = (
        db.scalar(select(func.count(User.id)).where(User.status == UserStatus.pending)) or 0
    )

    pending = list(
        db.scalars(
            select(User)
            .where(User.status == UserStatus.pending)
            .order_by(User.created_at.desc())
            .limit(PENDING_LIMIT)
        )
    )

    alerts = list(
        db.scalars(
            select(SecurityLog)
            .where(SecurityLog.status != SecurityStatus.ok)
            .order_by(SecurityLog.timestamp.desc())
            .limit(ALERT_LIMIT)
        )
    )

    return SuperAdminOverview(
        school_count=school_count,
        teacher_count=teacher_count,
        lesson_count=lesson_count,
        pending_count=pending_count,
        pending_approvals=pending,
        security_alerts=alerts,
    )
