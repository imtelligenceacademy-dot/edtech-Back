"""Builds a text snapshot of a school's live monitoring data for the
school-admin AI assistant to ground its answers in.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Lesson,
    Progress,
    Report,
    School,
    User,
)
from app.models.enums import Role, UserStatus
from app.services.report_metrics import (
    QUIET_AFTER_DAYS,
    movement,
    progress_stats,
    quiet_teachers,
    security_anomalies,
)


def build_school_context(db: Session, admin: User) -> tuple[str, str]:
    """Return (context_text, school_name) scoped to the admin's school."""
    school = db.get(School, admin.school_id) if admin.school_id else None
    school_name = school.name if school else "your school"

    teachers = list(
        db.scalars(
            select(User).where(
                User.school_id == admin.school_id, User.role == Role.teacher
            )
        )
    )
    tids = [t.id for t in teachers]
    name_by_id = {t.id: t.name for t in teachers}

    progress = (
        list(db.scalars(select(Progress).where(Progress.teacher_id.in_(tids))))
        if tids
        else []
    )
    lesson_ids = {p.lesson_id for p in progress}
    lessons = (
        {l.id: l for l in db.scalars(select(Lesson).where(Lesson.id.in_(lesson_ids)))}
        if lesson_ids
        else {}
    )
    reports = list(
        db.scalars(
            select(Report)
            .where(Report.school_id == admin.school_id)
            .order_by(Report.created_at.desc())
            .limit(10)
        )
    )

    active = [t for t in teachers if t.status == UserStatus.active]
    pending = [t for t in teachers if t.status == UserStatus.pending]

    stats = progress_stats(progress)
    moved = movement(db, tids, school_id=admin.school_id)
    quiet = quiet_teachers(db, teachers)

    lines: list[str] = [
        f"SCHOOL: {school_name}",
        "",
        f"SUMMARY: {len(active)} active teachers, {len(pending)} pending. "
        f"{stats.assigned} lessons assigned, {stats.started} started, "
        f"{stats.completed} completed ({stats.completion_rate}%), "
        f"{stats.not_started} never opened, {stats.late} late."
        + (
            f" Average progress across the lessons actually begun: "
            f"{stats.avg_of_started}%."
            if stats.avg_of_started is not None
            else ""
        ),
        "",
        # The comparison is the point of a report: a figure with nothing beside
        # it cannot say whether things are getting better.
        "THIS WEEK vs LAST WEEK:",
    ]
    lines += [f"- {line}" for line in moved.lines()]

    lines += ["", f"QUIET TEACHERS (nothing opened in {QUIET_AFTER_DAYS}+ days):"]
    lines += [f"- {q.describe()}" for q in quiet] or ["- (none — everyone is active)"]

    lines += ["", "TEACHERS:"]
    for t in teachers:
        lines.append(
            f"- {t.name} ({t.status.value}) | grades {t.grades or []} | "
            f"language {t.language or 'n/a'}"
        )

    lines += ["", "PROGRESS (teacher | lesson | status | percent | watchdog):"]
    if progress:
        for p in progress:
            title = lessons[p.lesson_id].title if p.lesson_id in lessons else p.lesson_id
            lines.append(
                f"- {name_by_id.get(p.teacher_id, p.teacher_id)} | {title} | "
                f"{p.status.value} | {p.percent_complete}% | {p.watchdog.value}"
                + (f" | {p.watchdog_message}" if p.watchdog_message else "")
            )
    else:
        lines.append("- (no progress records yet)")

    lines += ["", "SECURITY ALERTS (grouped, last 30 days):"]
    anomalies = security_anomalies(db, school_id=admin.school_id)
    lines += [f"- {a.describe()}" for a in anomalies] or [
        "- (no warnings or blocks recently)"
    ]

    lines += ["", "REPORTS:"]
    if reports:
        for r in reports:
            lines.append(f"- {r.title} | scope {r.scope.value} | status {r.status.value}")
    else:
        lines.append("- (no reports requested yet)")

    return "\n".join(lines)[: settings.ai_max_context_chars], school_name
