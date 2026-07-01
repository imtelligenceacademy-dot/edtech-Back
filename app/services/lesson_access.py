"""Sequential lesson unlocking for teachers.

A teacher progresses through their lessons one at a time, per
(grade, language) track. Rules:

- The first lesson in each track is available immediately.
- A later lesson only becomes available once the previous lesson in the track
  is completed AND the configured wait period (default 7 days) has elapsed since
  that completion.
- A completed lesson locks (it can't be reopened) — so at any moment a teacher
  has exactly one "current" lesson per track, plus locked past/future ones.
- A super-admin can set ``unlocked_override`` on a teacher's progress row, which
  forces that lesson available regardless of the rules above (this both bypasses
  the wait and reopens a completed lesson).

Everything here is read-only and pure: it computes access from the teacher's
assignments + progress so the lessons API, the progress API, and the AI
grounding all agree on what is open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lesson, LessonAssignment, Progress, User
from app.models.enums import LessonStatus, Role

# Access states surfaced to the frontend.
AVAILABLE = "available"   # the teacher can open this now
COMPLETED = "completed"   # finished and locked (ask admin to reopen)
WAITING = "waiting"       # previous lesson done, counting down to unlock
LOCKED = "locked"         # earlier lessons not finished yet


@dataclass
class LessonAccess:
    status: str
    available_at: datetime | None = None  # for WAITING: when it unlocks
    message: str | None = None


def _wait() -> timedelta:
    return timedelta(days=settings.lesson_unlock_wait_days)


def _track_key(lesson: Lesson) -> tuple[int, str | None]:
    """Lessons are sequenced within a (grade, language) track."""
    return (lesson.grade, lesson.language)


def _order_key(lesson: Lesson) -> tuple[int, str]:
    # Order a track by lesson number, then title as a stable tiebreak.
    return (lesson.lesson_no if lesson.lesson_no is not None else 10_000, lesson.title)


def compute_access(db: Session, teacher: User) -> dict[str, LessonAccess]:
    """Return access info keyed by lesson id for every lesson assigned to the
    teacher. Non-teachers get an empty map (no gating applies to them)."""
    if teacher.role != Role.teacher:
        return {}

    lesson_ids = [
        a.lesson_id
        for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
        )
    ]
    if not lesson_ids:
        return {}

    lessons = list(db.scalars(select(Lesson).where(Lesson.id.in_(lesson_ids))))
    progress = {
        p.lesson_id: p
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == teacher.id,
                Progress.lesson_id.in_(lesson_ids),
            )
        )
    }

    # Group into tracks and order each one.
    tracks: dict[tuple[int, str | None], list[Lesson]] = {}
    for lesson in lessons:
        tracks.setdefault(_track_key(lesson), []).append(lesson)

    out: dict[str, LessonAccess] = {}
    wait = _wait()

    now = datetime.now(timezone.utc)
    for track in tracks.values():
        track.sort(key=_order_key)
        # `gate_open` is True while the sequence is still reachable. It starts
        # open (first lesson), is consumed by the first non-completed lesson,
        # and re-opens after a completed lesson once its wait elapses.
        # `pending_unlock_at` is the time the next lesson is waiting on (if any).
        gate_open = True
        pending_unlock_at: datetime | None = None
        for lesson in track:
            p = progress.get(lesson.id)
            override = bool(p and p.unlocked_override)
            completed = bool(p and p.status == LessonStatus.completed)

            # Only a genuinely completed lesson (locked, NOT reopened) starts the
            # countdown for the next lesson. A completed lesson an admin has
            # reopened is treated below as the teacher's active lesson instead —
            # so the next lesson's countdown does not run until this one is
            # actually finished again.
            if completed and not override:
                out[lesson.id] = LessonAccess(
                    status=COMPLETED,
                    message="Completed — ask your admin to reopen it.",
                )
                completed_at = p.completed_at if p else None
                if completed_at is not None:
                    unlock_at = _as_utc(completed_at) + wait
                    gate_open = now >= unlock_at
                    pending_unlock_at = None if gate_open else unlock_at
                else:
                    # Completed but no timestamp recorded (legacy row): unlock now.
                    gate_open = True
                    pending_unlock_at = None
                continue

            # The teacher's active lesson: the first not-completed lesson, or a
            # completed one an admin reopened (override). Either way it consumes
            # the gate — nothing after it opens or counts down until it is
            # (re)completed.
            if override or gate_open:
                out[lesson.id] = LessonAccess(status=AVAILABLE)
            elif pending_unlock_at is not None:
                out[lesson.id] = LessonAccess(
                    status=WAITING,
                    available_at=pending_unlock_at,
                    message="Available after the waiting period — or ask your admin for access.",
                )
            else:
                out[lesson.id] = LessonAccess(
                    status=LOCKED,
                    message="Finish the previous lesson first — or ask your admin for access.",
                )
            # This lesson consumes the gate; nothing further in the track opens
            # until it is completed and its own wait elapses.
            gate_open = False
            pending_unlock_at = None

    return out


def _as_utc(dt: datetime) -> datetime:
    """SQLite may hand back naive datetimes; treat those as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_lesson_available(db: Session, teacher: User, lesson_id: str) -> bool:
    """True if the teacher may currently open/ground-on this lesson."""
    access = compute_access(db, teacher).get(lesson_id)
    return access is not None and access.status == AVAILABLE


def get_access_status(db: Session, teacher: User, lesson_id: str) -> str | None:
    """The teacher's access status for one lesson, or None if not assigned."""
    access = compute_access(db, teacher).get(lesson_id)
    return access.status if access else None
