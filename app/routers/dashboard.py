"""Aggregated dashboard endpoints. Counts and recent items are computed in the
database and scoped server-side, so the browser never pulls full tables just to
derive a few numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_roles
from app.models import AccessRequest, Lesson, Progress, School, SecurityLog, UploadedFile, User
from app.models.enums import LessonStatus, Role, SecurityStatus, UserStatus
from app.schemas.dashboard import StalledTeacher, SuperAdminOverview
from app.services.access_requests import PENDING, list_pending

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

ALERT_LIMIT = 5
PENDING_LIMIT = 20
# How much of each attention list is returned; the counts carry the rest.
ATTENTION_LIMIT = 8
# A teacher who hasn't opened a lesson in this long is worth a look. Teachers
# created inside the same window are left alone — a new hire isn't behind.
STALLED_AFTER_DAYS = 14


def stalled_teachers(db: Session) -> list[StalledTeacher]:
    """Active teachers who have not opened a lesson recently, or ever.

    Activity is `Progress.last_opened_at`, not the row's updated_at: assigning a
    lesson creates progress rows, so updated_at would make a teacher who has
    never opened anything look busy.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALLED_AFTER_DAYS)

    teachers = list(
        db.scalars(
            select(User).where(
                User.role == Role.teacher,
                User.status == UserStatus.active,
                User.created_at < cutoff,
            )
        )
    )
    if not teachers:
        return []

    ids = [t.id for t in teachers]
    last_seen = {
        teacher_id: last
        for teacher_id, last in db.execute(
            select(Progress.teacher_id, func.max(Progress.last_opened_at))
            .where(Progress.teacher_id.in_(ids))
            .group_by(Progress.teacher_id)
        ).all()
    }
    completed = {
        teacher_id: count
        for teacher_id, count in db.execute(
            select(Progress.teacher_id, func.count(Progress.id))
            .where(Progress.teacher_id.in_(ids), Progress.status == LessonStatus.completed)
            .group_by(Progress.teacher_id)
        ).all()
    }

    stalled: list[StalledTeacher] = []
    for teacher in teachers:
        last = last_seen.get(teacher.id)
        if last is not None:
            # SQLite hands back naive datetimes; compare like with like.
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last >= cutoff:
                continue
        stalled.append(
            StalledTeacher(
                teacher_id=teacher.id,
                name=teacher.name,
                email=teacher.email,
                school_id=teacher.school_id,
                last_activity_at=last,
                completed_count=completed.get(teacher.id, 0),
            )
        )

    # Quietest first: never-opened, then longest since.
    stalled.sort(key=lambda s: (s.last_activity_at is not None, s.last_activity_at or cutoff))
    return stalled


@router.get("/super-admin", response_model=SuperAdminOverview)
def super_admin_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(Role.super_admin)),
) -> SuperAdminOverview:
    school_count = db.scalar(select(func.count(School.id))) or 0
    teacher_count = (
        db.scalar(select(func.count(User.id)).where(User.role == Role.teacher)) or 0
    )
    lesson_rows = db.execute(
        select(Lesson.id, Lesson.grade, Lesson.lesson_no, Lesson.course, Lesson.year)
    ).all()
    lesson_count = len(
        {
            (grade, lesson_no, course, year)
            if lesson_no is not None
            else ("legacy", lesson_id)
            for lesson_id, grade, lesson_no, course, year in lesson_rows
        }
    )
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

    # --- Needs attention ---------------------------------------------------- #
    access_requests = list_pending(db, limit=ATTENTION_LIMIT)
    access_request_count = (
        db.scalar(select(func.count(AccessRequest.id)).where(AccessRequest.status == PENDING)) or 0
    )
    stalled = stalled_teachers(db)
    # PDFs whose filename didn't parse: stored, but assigned to nobody.
    unsorted = list(
        db.scalars(
            select(UploadedFile)
            .where(UploadedFile.linked_lesson_id.is_(None))
            .order_by(UploadedFile.created_at.desc())
            .limit(ATTENTION_LIMIT)
        )
    )
    unsorted_count = (
        db.scalar(
            select(func.count(UploadedFile.id)).where(UploadedFile.linked_lesson_id.is_(None))
        )
        or 0
    )

    return SuperAdminOverview(
        school_count=school_count,
        teacher_count=teacher_count,
        lesson_count=lesson_count,
        pending_count=pending_count,
        pending_approvals=pending,
        security_alerts=alerts,
        access_requests=access_requests,
        access_request_count=access_request_count,
        stalled_teachers=stalled[:ATTENTION_LIMIT],
        stalled_teacher_count=len(stalled),
        unsorted_uploads=unsorted,
        unsorted_upload_count=unsorted_count,
        stalled_after_days=STALLED_AFTER_DAYS,
    )
