"""Pending lesson-access requests, shaped for whoever needs to act on them.

Kept out of the router so the dashboard can show the same inbox the Lesson
Unlock page shows: a teacher blocked on a request is the most time-sensitive
thing on the platform, and it should not depend on someone opening one
particular page to notice.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccessRequest, Lesson, User
from app.schemas.access_request import AccessRequestOut

PENDING = "pending"


def to_out(req: AccessRequest, lesson: Lesson | None, teacher: User | None) -> AccessRequestOut:
    return AccessRequestOut(
        id=req.id,
        teacher_id=req.teacher_id,
        teacher_name=teacher.name if teacher else req.teacher_id,
        lesson_id=req.lesson_id,
        lesson_title=lesson.title if lesson else req.lesson_id,
        grade=lesson.grade if lesson else 0,
        language=lesson.language if lesson else None,
        lesson_no=lesson.lesson_no if lesson else None,
        status=req.status,
        note=req.note,
        created_at=req.created_at,
    )


def list_pending(db: Session, limit: int | None = None) -> list[AccessRequestOut]:
    """Oldest waiting first would be fairer, but newest first matches the rest
    of the admin UI; the count beside the list is what conveys the backlog."""
    stmt = (
        select(AccessRequest)
        .where(AccessRequest.status == PENDING)
        .order_by(AccessRequest.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    reqs = list(db.scalars(stmt))
    if not reqs:
        return []

    lessons = {
        l.id: l
        for l in db.scalars(select(Lesson).where(Lesson.id.in_([r.lesson_id for r in reqs])))
    }
    teachers = {
        u.id: u
        for u in db.scalars(select(User).where(User.id.in_([r.teacher_id for r in reqs])))
    }
    return [to_out(r, lessons.get(r.lesson_id), teachers.get(r.teacher_id)) for r in reqs]
