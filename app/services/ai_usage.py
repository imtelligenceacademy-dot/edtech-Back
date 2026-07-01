"""AI-usage tracking: record each assistant interaction and aggregate counts
over a rolling 7-day window (with the prior week for a week-over-week delta).

Counts are scoped server-side: super-admins see every school, everyone else
only their own school.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AiUsage, User
from app.models.enums import Role
from app.utils import new_id

from collections.abc import Sequence


class AILimitExceeded(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def enforce_ai_limit(db: Session, user: User, kind: str) -> None:
    """Raise when a user has exhausted their hourly or daily AI quota."""
    if kind == "teacher":
        hourly_limit = settings.ai_teacher_hourly_limit
        daily_limit = settings.ai_teacher_daily_limit
        label = "teacher AI assistant"
    else:
        hourly_limit = settings.ai_admin_hourly_limit
        daily_limit = settings.ai_admin_daily_limit
        label = "school-admin AI assistant"

    now = datetime.now(timezone.utc)
    hour_start = now - timedelta(hours=1)
    day_start = now - timedelta(days=1)

    base = select(func.count(AiUsage.id)).where(
        AiUsage.user_id == user.id,
        AiUsage.kind == kind,
    )
    last_hour = db.scalar(base.where(AiUsage.created_at >= hour_start)) or 0
    if hourly_limit > 0 and last_hour >= hourly_limit:
        raise AILimitExceeded(
            f"You've reached the {label} hourly limit ({hourly_limit}). Please try again later."
        )

    last_day = db.scalar(base.where(AiUsage.created_at >= day_start)) or 0
    if daily_limit > 0 and last_day >= daily_limit:
        raise AILimitExceeded(
            f"You've reached the {label} daily limit ({daily_limit}). Please try again tomorrow."
        )


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
