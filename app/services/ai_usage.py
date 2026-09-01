"""AI-usage tracking: record each assistant interaction and aggregate counts
over a rolling 7-day window (with the prior week for a week-over-week delta).

Counts are scoped server-side: super-admins see every school, everyone else
only their own school.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AiUsage, School, User
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


# --- Per-teacher usage report ------------------------------------------------ #
# The super-admin's "AI Usage" screen. Deliberately concrete: every number here
# is a count over a window whose boundaries are handed to the UI alongside it,
# so the screen can name the window instead of saying "recent". Nothing on this
# report is a rating, a score, or a band — a teacher who asked four questions is
# shown as four questions, not as "low usage".

# Days of raw rows pulled into Python. Everything except the all-time totals is
# computed from this window; 30 days keeps the payload small while still
# covering the 14 days the week-over-week comparison needs.
USAGE_WINDOW_DAYS = 30
# Width of the per-day strip the UI draws.
USAGE_DAILY_DAYS = 14

# Counts are bucketed into days for the people reading them, who are in Beirut,
# not into UTC days. Falls back to UTC where the zone database is missing rather
# than failing the request — the response says which one was used.
REPORT_TZ_NAME = "Asia/Beirut"


def _report_tz() -> tuple[tzinfo, str]:
    try:
        return ZoneInfo(REPORT_TZ_NAME), REPORT_TZ_NAME
    except Exception:  # pragma: no cover - only when tzdata is absent
        return timezone.utc, "UTC"


def teacher_usage_report(db: Session) -> dict:
    """Every teacher's AI-assistant usage, with the windows spelled out.

    Teachers who have never asked anything are included with zeroes; leaving
    them out would make the screen a list of the active only, which is the one
    reading it cannot distinguish from "no teachers".
    """
    tz, tz_name = _report_tz()
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(tz)

    today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = today_start_local.astimezone(timezone.utc)
    hour_start = now - timedelta(hours=1)
    day_start = now - timedelta(days=1)
    start_7 = now - timedelta(days=7)
    start_14 = now - timedelta(days=14)
    start_30 = now - timedelta(days=USAGE_WINDOW_DAYS)

    teachers = list(
        db.scalars(
            select(User).where(User.role == Role.teacher).order_by(User.name)
        )
    )
    school_names = dict(db.execute(select(School.id, School.name)).all())
    teacher_ids = [t.id for t in teachers]

    # All-time figures stay in SQL: the row count grows without bound, and only
    # three numbers per teacher are needed from it.
    lifetime = {
        uid: (int(count), first, last)
        for uid, count, first, last in db.execute(
            select(
                AiUsage.user_id,
                func.count(AiUsage.id),
                func.min(AiUsage.created_at),
                func.max(AiUsage.created_at),
            )
            .where(AiUsage.user_id.in_(teacher_ids))
            .group_by(AiUsage.user_id)
        ).all()
    } if teacher_ids else {}

    # The recent window comes back as raw timestamps and is bucketed in Python.
    # Day bucketing in SQL is not portable between SQLite and Postgres, and
    # neither dialect would bucket into Beirut days without more work than this.
    recent: dict[str, list[datetime]] = {uid: [] for uid in teacher_ids}
    if teacher_ids:
        for uid, created in db.execute(
            select(AiUsage.user_id, AiUsage.created_at).where(
                AiUsage.user_id.in_(teacher_ids), AiUsage.created_at >= start_30
            )
        ).all():
            if uid in recent:
                recent[uid].append(_as_utc(created))

    # The days the strip covers, oldest first, so a teacher with no activity
    # still gets a full row of zeroes rather than an empty strip.
    day_keys = [
        (today_start_local - timedelta(days=offset)).date().isoformat()
        for offset in range(USAGE_DAILY_DAYS - 1, -1, -1)
    ]

    rows = []
    for teacher in teachers:
        stamps = recent.get(teacher.id, [])
        total, first_used, last_used = lifetime.get(teacher.id, (0, None, None))

        per_day: dict[str, int] = {key: 0 for key in day_keys}
        active_days = set()
        for stamp in stamps:
            key = stamp.astimezone(tz).date().isoformat()
            active_days.add(key)
            if key in per_day:
                per_day[key] += 1

        last_hour = sum(1 for s in stamps if s >= hour_start)
        last_24h = sum(1 for s in stamps if s >= day_start)
        last_7 = sum(1 for s in stamps if s >= start_7)
        prev_7 = sum(1 for s in stamps if start_14 <= s < start_7)

        rows.append(
            {
                "teacher_id": teacher.id,
                "name": teacher.name,
                "email": teacher.email,
                "status": teacher.status.value,
                "school_id": teacher.school_id,
                "school_name": school_names.get(teacher.school_id),
                "grades": list(teacher.grades or []),
                "total": total,
                "today": sum(1 for s in stamps if s >= today_start),
                "last_hour": last_hour,
                "last24h": last_24h,
                "last7": last_7,
                "prev7": prev_7,
                "last30": len(stamps),
                "active_days30": len(active_days),
                "first_used_at": _as_utc(first_used) if first_used else None,
                "last_used_at": _as_utc(last_used) if last_used else None,
                # Quota headroom, as counts rather than a percentage — "12 of 40
                # used" is actionable in a way that "30%" is not.
                "hourly_used": last_hour,
                "daily_used": last_24h,
                "daily": [{"date": key, "count": per_day[key]} for key in day_keys],
            }
        )

    return {
        "generated_at": now,
        "timezone": tz_name,
        # Handed over so the screen can label every column with the moment it
        # counts from, instead of leaving the reader to guess what "last 7 days"
        # was measured against.
        "today_start": today_start,
        "hour_start": hour_start,
        "day_start": day_start,
        "week_start": start_7,
        "prev_week_start": start_14,
        "window_start": start_30,
        "daily_days": USAGE_DAILY_DAYS,
        "hourly_limit": settings.ai_teacher_hourly_limit,
        "daily_limit": settings.ai_teacher_daily_limit,
        "teachers": rows,
    }


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat those as the UTC they were
    stored as, so comparisons against timezone-aware bounds don't explode."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
