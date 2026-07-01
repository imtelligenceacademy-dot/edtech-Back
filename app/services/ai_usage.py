"""AI-usage tracking: record each assistant interaction and aggregate counts
over a rolling 7-day window (with the prior week for a week-over-week delta).

Counts are scoped server-side: super-admins see every school, everyone else
only their own school.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AiUsage, User
from app.models.enums import Role
from app.utils import new_id

from collections.abc import Sequence


def record_ai_usage(db: Session, user: User, kind: str) -> None:
    """Log one AI interaction. Commits immediately so the row survives even when
    the caller returns a streaming response (whose generator runs later)."""
    db.add(
        AiUsage(
            id=new_id("aiu"),
            user_id=user.id,
            school_id=user.school_id,
            role=user.role,
            kind=kind,
        )
    )
    db.commit()


def usage_stats(db: Session, user: User) -> dict[str, int | None]:
    """Interaction counts for the last 7 days and the 7 days before that,
    plus a percentage delta (None when there is no prior-week baseline)."""
    now = datetime.now(timezone.utc)
    start_7 = now - timedelta(days=7)
    start_14 = now - timedelta(days=14)

    base = select(func.count(AiUsage.id))
    if user.role != Role.super_admin:
        base = base.where(AiUsage.school_id == user.school_id)

    last7 = db.scalar(base.where(AiUsage.created_at >= start_7)) or 0
    prev7 = (
        db.scalar(
            base.where(AiUsage.created_at >= start_14, AiUsage.created_at < start_7)
        )
        or 0
    )

    delta_pct: int | None
    if prev7 > 0:
        delta_pct = round((last7 - prev7) / prev7 * 100)
    elif last7 > 0:
        delta_pct = 100
    else:
        delta_pct = None

    return {"last7": last7, "prev7": prev7, "delta_pct": delta_pct}


def usage_by_user(
    db: Session, user_ids: Sequence[str]
) -> dict[str, dict[str, int]]:
    """Per-user interaction counts: {user_id: {"total": n, "last7": n}}.
    Users with no activity are included with zeroes."""
    if not user_ids:
        return {}
    start_7 = datetime.now(timezone.utc) - timedelta(days=7)

    totals = dict(
        db.execute(
            select(AiUsage.user_id, func.count(AiUsage.id))
            .where(AiUsage.user_id.in_(user_ids))
            .group_by(AiUsage.user_id)
        ).all()
    )
    recent = dict(
        db.execute(
            select(AiUsage.user_id, func.count(AiUsage.id))
            .where(AiUsage.user_id.in_(user_ids), AiUsage.created_at >= start_7)
            .group_by(AiUsage.user_id)
        ).all()
    )
    return {
        uid: {"total": int(totals.get(uid, 0)), "last7": int(recent.get(uid, 0))}
        for uid in user_ids
    }


def usage_total_for_school(db: Session, school_id: str | None) -> int:
    """All AI interactions attributed to a school (teachers + its admin)."""
    if not school_id:
        return 0
    return db.scalar(
        select(func.count(AiUsage.id)).where(AiUsage.school_id == school_id)
    ) or 0


def usage_breakdown_for_school(db: Session, school_id: str | None) -> dict[str, int]:
    """School AI interactions split by assistant: teacher lesson-assistant vs.
    school-admin operations-assistant. Returns {teacher, admin, total}."""
    if not school_id:
        return {"teacher": 0, "admin": 0, "total": 0}
    by_kind = dict(
        db.execute(
            select(AiUsage.kind, func.count(AiUsage.id))
            .where(AiUsage.school_id == school_id)
            .group_by(AiUsage.kind)
        ).all()
    )
    teacher = int(by_kind.get("teacher", 0))
    admin = int(by_kind.get("admin", 0))
    return {"teacher": teacher, "admin": admin, "total": teacher + admin}
