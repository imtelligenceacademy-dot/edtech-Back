"""The numbers a report is built from.

One module so the narrative and the tables can never disagree: the assistant is
handed exactly the figures the reader will see underneath it.

Two ideas drive what is in here.

*Honest completion.* "Average completion" used to mean the mean of
``percent_complete`` over every assignment, never-opened ones included. Upload a
grade's PDFs on Monday and forty rows appear at 0%, so every affected school's
average collapses on Tuesday without a single teacher having done anything
wrong. Assigned / started / completed are three separate facts; a percentage is
only offered for work that was actually begun.

*Honest trends.* A report's job is to say what changed, but only some of this
data can carry a comparison. ``completed_at`` and a chat message's
``created_at`` are events — they happened at a moment, and counting them per
week is sound. ``last_opened_at`` is a "last seen" field: a teacher active in
both weeks only appears in the later one, so a week-over-week figure built on it
would always flatter the present. Activity is therefore reported as a plain
current-week count, with no prior week beside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChatMessage, Progress, SecurityLog, User
from app.models.enums import LessonStatus, Role, SecurityStatus, UserStatus

# A teacher silent this long is worth a look. Mirrors the dashboard's
# STALLED_AFTER_DAYS so the report and the "needs attention" panel agree.
QUIET_AFTER_DAYS = 14
WEEK = timedelta(days=7)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; compare like with like."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #
@dataclass
class ProgressStats:
    assigned: int = 0
    started: int = 0          # opened at least once (in-progress, late, or done)
    completed: int = 0
    not_started: int = 0
    late: int = 0
    # Share of everything assigned that is finished.
    completion_rate: int = 0
    # Mean percent across the lessons that were actually begun. Reported only
    # when something has been; otherwise it is a statement about nothing.
    avg_of_started: int | None = None

    @property
    def headline(self) -> str:
        done = f"{self.completed} of {self.assigned} lessons complete ({self.completion_rate}%)"
        if self.not_started:
            done += f", {self.not_started} not yet opened"
        return done


def progress_stats(rows: list[Progress]) -> ProgressStats:
    if not rows:
        return ProgressStats()
    completed = [p for p in rows if p.status == LessonStatus.completed]
    not_started = [p for p in rows if p.status == LessonStatus.not_started]
    started = [p for p in rows if p.status != LessonStatus.not_started]
    late = [p for p in rows if p.status == LessonStatus.late or p.watchdog.value == "late"]

    return ProgressStats(
        assigned=len(rows),
        started=len(started),
        completed=len(completed),
        not_started=len(not_started),
        late=len(late),
        completion_rate=round(100 * len(completed) / len(rows)),
        avg_of_started=(
            round(sum(p.percent_complete for p in started) / len(started)) if started else None
        ),
    )


# --------------------------------------------------------------------------- #
# What changed
# --------------------------------------------------------------------------- #
@dataclass
class Delta:
    """A count this week against the same count last week."""

    label: str
    this_week: int = 0
    prior_week: int = 0

    def describe(self) -> str:
        if self.prior_week == 0:
            return (
                f"{self.this_week} {self.label} this week (nothing the week before)"
                if self.this_week
                else f"no {self.label} this week or last"
            )
        if self.this_week == self.prior_week:
            return f"{self.this_week} {self.label} this week, same as the week before"
        direction = "up" if self.this_week > self.prior_week else "down"
        return (
            f"{self.this_week} {self.label} this week, {direction} from "
            f"{self.prior_week} the week before"
        )


@dataclass
class Movement:
    completed: Delta = field(default_factory=lambda: Delta("lessons completed"))
    questions: Delta = field(default_factory=lambda: Delta("questions asked"))
    # Current week only — see the module docstring for why this one has no
    # prior-week figure beside it.
    teachers_active: int = 0
    lessons_touched: int = 0
    new_alerts: int = 0

    def lines(self) -> list[str]:
        return [
            self.completed.describe(),
            self.questions.describe(),
            f"{self.teachers_active} teacher(s) opened something in the last 7 days, "
            f"across {self.lessons_touched} lesson(s)",
            f"{self.new_alerts} new security alert(s) in the last 7 days",
        ]


def movement(db: Session, teacher_ids: list[str], school_id: str | None = None) -> Movement:
    """Week-over-week for one school's teachers, or the platform when school_id
    is None and teacher_ids covers everyone."""
    out = Movement()
    if not teacher_ids:
        return out

    now = datetime.now(timezone.utc)
    week_start = now - WEEK
    prior_start = now - 2 * WEEK

    def _count(model, when, start, end) -> int:
        stmt = select(func.count(model.id)).where(
            model.teacher_id.in_(teacher_ids), when >= start
        )
        if end is not None:
            stmt = stmt.where(when < end)
        return int(db.scalar(stmt) or 0)

    out.completed.this_week = _count(Progress, Progress.completed_at, week_start, None)
    out.completed.prior_week = _count(Progress, Progress.completed_at, prior_start, week_start)

    # Only the teacher's own turns — the assistant's replies aren't questions.
    def _questions(start, end) -> int:
        stmt = select(func.count(ChatMessage.id)).where(
            ChatMessage.teacher_id.in_(teacher_ids),
            ChatMessage.role == "user",
            ChatMessage.created_at >= start,
        )
        if end is not None:
            stmt = stmt.where(ChatMessage.created_at < end)
        return int(db.scalar(stmt) or 0)

    out.questions.this_week = _questions(week_start, None)
    out.questions.prior_week = _questions(prior_start, week_start)

    active = db.execute(
        select(Progress.teacher_id, func.count(Progress.id))
        .where(
            Progress.teacher_id.in_(teacher_ids),
            Progress.last_opened_at >= week_start,
        )
        .group_by(Progress.teacher_id)
    ).all()
    out.teachers_active = len(active)
    out.lessons_touched = sum(count for _, count in active)

    alert_stmt = select(func.count(SecurityLog.id)).where(
        SecurityLog.status != SecurityStatus.ok, SecurityLog.timestamp >= week_start
    )
    if school_id:
        alert_stmt = alert_stmt.where(SecurityLog.school_id == school_id)
    out.new_alerts = int(db.scalar(alert_stmt) or 0)
    return out


# --------------------------------------------------------------------------- #
# Who needs a nudge
# --------------------------------------------------------------------------- #
@dataclass
class QuietTeacher:
    name: str
    email: str
    days_quiet: int | None  # None = has never opened a lesson
    completed: int

    def describe(self) -> str:
        when = (
            "has never opened a lesson"
            if self.days_quiet is None
            else f"last opened one {self.days_quiet} days ago"
        )
        return f"{self.name} ({self.email}) {when}; {self.completed} completed to date"


def quiet_teachers(db: Session, teachers: list[User]) -> list[QuietTeacher]:
    """Active teachers who have not opened a lesson in QUIET_AFTER_DAYS.

    Activity is `last_opened_at`, never the row's updated_at: assigning a lesson
    writes progress rows, which would make someone who has never opened anything
    look busy. Teachers who joined inside the window are left alone — a new hire
    is not behind.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=QUIET_AFTER_DAYS)
    candidates = [
        t
        for t in teachers
        if t.role == Role.teacher
        and t.status == UserStatus.active
        and (_aware(t.created_at) or now) < cutoff
    ]
    if not candidates:
        return []

    ids = [t.id for t in candidates]
    last_seen = {
        tid: _aware(last)
        for tid, last in db.execute(
            select(Progress.teacher_id, func.max(Progress.last_opened_at))
            .where(Progress.teacher_id.in_(ids))
            .group_by(Progress.teacher_id)
        ).all()
    }
    completed = {
        tid: count
        for tid, count in db.execute(
            select(Progress.teacher_id, func.count(Progress.id))
            .where(Progress.teacher_id.in_(ids), Progress.status == LessonStatus.completed)
            .group_by(Progress.teacher_id)
        ).all()
    }

    quiet: list[QuietTeacher] = []
    for teacher in candidates:
        last = last_seen.get(teacher.id)
        if last is not None and last >= cutoff:
            continue
        quiet.append(
            QuietTeacher(
                name=teacher.name,
                email=teacher.email,
                days_quiet=None if last is None else (now - last).days,
                completed=completed.get(teacher.id, 0),
            )
        )
    # Quietest first: never-opened, then longest since.
    quiet.sort(key=lambda q: (q.days_quiet is not None, -(q.days_quiet or 0)))
    return quiet


# --------------------------------------------------------------------------- #
# Security, grouped
# --------------------------------------------------------------------------- #
@dataclass
class Anomaly:
    user_name: str
    event: str
    status: str
    count: int
    last_seen: datetime | None

    def describe(self) -> str:
        when = self.last_seen.strftime("%Y-%m-%d %H:%M") if self.last_seen else "—"
        times = "once" if self.count == 1 else f"{self.count} times"
        return f"{self.user_name}: {self.event} ({self.status}) {times}, last {when}"


def security_anomalies(db: Session, school_id: str | None = None, days: int = 30) -> list[Anomaly]:
    """Only what went wrong, grouped per person and event.

    The section used to print the last fifty log lines, nearly all of them
    ordinary logins, which buried the three that mattered.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(
            SecurityLog.user_name,
            SecurityLog.event,
            SecurityLog.status,
            func.count(SecurityLog.id),
            func.max(SecurityLog.timestamp),
        )
        .where(SecurityLog.status != SecurityStatus.ok, SecurityLog.timestamp >= since)
        .group_by(SecurityLog.user_name, SecurityLog.event, SecurityLog.status)
    )
    if school_id:
        stmt = stmt.where(SecurityLog.school_id == school_id)

    rows = db.execute(stmt).all()
    anomalies = [
        Anomaly(
            user_name=name,
            event=event.value if hasattr(event, "value") else str(event),
            status=status.value if hasattr(status, "value") else str(status),
            count=int(count),
            last_seen=_aware(last),
        )
        for name, event, status, count, last in rows
    ]
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    anomalies.sort(key=lambda a: (a.count, a.last_seen or oldest), reverse=True)
    return anomalies
