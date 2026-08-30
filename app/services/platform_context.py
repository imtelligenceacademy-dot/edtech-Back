"""A text snapshot of every school, for the super-admin's report narrative.

Deliberately not the school context repeated N times. The super-admin's
question is comparative — which school is moving, which has stalled, who has
gone quiet — so this carries per-school rollups and the exceptions across all of
them, and never the per-assignment detail that belongs in a single school's
report.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lesson, Progress, School, User
from app.models.enums import Role, UserStatus
from app.services.ai_usage import usage_total_for_school
from app.services.report_metrics import (
    QUIET_AFTER_DAYS,
    movement,
    progress_stats,
    quiet_teachers,
    security_anomalies,
)


def build_platform_context(db: Session) -> str:
    schools = list(db.scalars(select(School).order_by(School.name)))
    teachers = list(db.scalars(select(User).where(User.role == Role.teacher)))
    teacher_ids = [t.id for t in teachers]
    lesson_count = db.scalar(select(func.count(Lesson.id))) or 0

    progress = (
        list(db.scalars(select(Progress).where(Progress.teacher_id.in_(teacher_ids))))
        if teacher_ids
        else []
    )
    stats = progress_stats(progress)
    moved = movement(db, teacher_ids)

    by_school: dict[str | None, list[Progress]] = {s.id: [] for s in schools}
    school_of_teacher = {t.id: t.school_id for t in teachers}
    for p in progress:
        sid = school_of_teacher.get(p.teacher_id)
        if sid in by_school:
            by_school[sid].append(p)

    lines: list[str] = [
        "SCOPE: every school on the platform.",
        "",
        f"PLATFORM SUMMARY: {len(schools)} schools, {len(teachers)} teachers, "
        f"{lesson_count} lessons in the curriculum. {stats.assigned} lessons assigned, "
        f"{stats.started} started, {stats.completed} completed "
        f"({stats.completion_rate}%), {stats.not_started} never opened, "
        f"{stats.late} late.",
        "",
        "THIS WEEK vs LAST WEEK (platform-wide):",
    ]
    lines += [f"- {line}" for line in moved.lines()]

    lines += ["", "PER SCHOOL (assigned | started | completed | rate | late | AI questions):"]
    for school in schools:
        s_stats = progress_stats(by_school.get(school.id, []))
        s_teachers = [t for t in teachers if t.school_id == school.id]
        active = sum(1 for t in s_teachers if t.status == UserStatus.active)
        s_moved = movement(db, [t.id for t in s_teachers], school_id=school.id)
        lines.append(
            f"- {school.name} ({school.city or '—'}, {school.country or '—'}) | "
            f"{active} active teachers | {s_stats.assigned} assigned | "
            f"{s_stats.started} started | {s_stats.completed} completed | "
            f"{s_stats.completion_rate}% | {s_stats.late} late | "
            f"{usage_total_for_school(db, school.id)} AI questions all time | "
            f"{s_moved.completed.this_week} completed this week vs "
            f"{s_moved.completed.prior_week} last week"
        )

    school_name_by_id = {s.id: s.name for s in schools}
    school_by_email = {
        t.email: school_name_by_id.get(t.school_id, "unknown school") for t in teachers
    }
    quiet = quiet_teachers(db, teachers)
    lines += ["", f"QUIET TEACHERS (nothing opened in {QUIET_AFTER_DAYS}+ days):"]
    lines += [
        f"- {school_by_email.get(q.email, '—')}: {q.describe()}" for q in quiet
    ] or ["- (none — every active teacher has opened something recently)"]

    anomalies = security_anomalies(db)
    lines += ["", "SECURITY ALERTS (grouped, last 30 days):"]
    lines += [f"- {a.describe()}" for a in anomalies] or [
        "- (no warnings or blocks recently)"
    ]

    return "\n".join(lines)[: settings.ai_max_context_chars]
