"""Sequential lesson unlocking for teachers.

A teacher progresses through their lessons one at a time, per
(grade, language, year, section) track. Rules:

- The first lesson in each track is available immediately.
- A later lesson only becomes available once the previous lesson in the track
  is completed AND the configured wait period (default 7 days) has elapsed since
  that completion.
- A completed lesson locks (it can't be reopened) — so at any moment a teacher
  has exactly one "current" lesson per track, plus locked past/future ones.
- A super-admin can set ``unlocked_override`` on a teacher's progress row, which
  forces that lesson available regardless of the rules above (this both bypasses
  the wait and reopens a completed lesson).

Sections are part of the track key, so a teacher who takes 6A, 6B and 6C through
the same curriculum walks three independent sequences. Completing a lesson with
6A leaves it open for 6B, and the countdown to the next lesson runs per class
rather than starting for classes that have not had this one. Teachers with a
single class have one unnamed section and see exactly the behaviour they always
did.

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
from app.services.sections import sections_for

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


# Relative order of courses within a track. Lower = earlier: a teacher works
# through the whole "python" course before any "microbit" lesson opens. Unknown
# / null courses (Year-1 and legacy content) share order 0 as a single course.
COURSE_ORDER: dict[str, int] = {"python": 1, "microbit": 2}


def _wait() -> timedelta:
    return timedelta(days=settings.lesson_unlock_wait_days)


def _course_order(lesson: Lesson) -> int:
    return COURSE_ORDER.get(lesson.course or "", 0)


def _track_key(lesson: Lesson, section: str) -> tuple[int, str | None, int, str]:
    """Lessons are sequenced within a (grade, language, year, section) track —
    Year 1 and Year 2 are entirely separate curricula, so they never share a
    sequence, and neither do two classes of the same grade."""
    return (lesson.grade, lesson.language, lesson.year, section)


def lesson_order_key(lesson: Lesson) -> tuple[int, int, str]:
    # Order a track by course first (python before microbit), then lesson number,
    # then title. This makes the whole track one linear sequence, so micro:bit
    # lesson 1 only becomes reachable after the last python lesson is completed
    # (plus its wait) — the cross-course gate falls out of one-at-a-time unlocking.
    return (
        _course_order(lesson),
        lesson.lesson_no if lesson.lesson_no is not None else 10_000,
        lesson.title,
    )


def compute_access(db: Session, teacher: User) -> dict[tuple[str, str], LessonAccess]:
    """Access info for every (lesson id, section) pair the teacher has.

    One pass covers all of a teacher's classes, so callers that need a single
    section filter this map rather than recomputing it. Non-teachers get an
    empty map (no gating applies to them).
    """
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
        (p.lesson_id, p.section): p
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == teacher.id,
                Progress.lesson_id.in_(lesson_ids),
            )
        )
    }

    # Group into tracks and order each one. A lesson appears once per section the
    # teacher takes for its grade.
    tracks: dict[tuple[int, str | None, int, str], list[Lesson]] = {}
    for lesson in lessons:
        for section in sections_for(teacher, lesson.grade):
            tracks.setdefault(_track_key(lesson, section), []).append(lesson)

    out: dict[tuple[str, str], LessonAccess] = {}
    wait = _wait()

    now = datetime.now(timezone.utc)
    for (_, _, _, section), track in tracks.items():
        track.sort(key=lesson_order_key)
        # `gate_open` is True while the sequence is still reachable. It starts
        # open (first lesson), is consumed by the first non-completed lesson,
        # and re-opens after a completed lesson once its wait elapses.
        # `pending_unlock_at` is the time the next lesson is waiting on (if any).
        gate_open = True
        pending_unlock_at: datetime | None = None
        sequence_blocked = False
        for lesson in track:
            key = (lesson.id, section)
            p = progress.get(key)
            override = bool(p and p.unlocked_override)
            completed = bool(p and p.status == LessonStatus.completed)

            # Only a genuinely completed lesson (locked, NOT reopened) starts the
            # countdown for the next lesson. A completed lesson an admin has
            # reopened is treated below as the teacher's active lesson instead —
            # so the next lesson's countdown does not run until this one is
            # actually finished again.
            if completed and not override:
                out[key] = LessonAccess(
                    status=COMPLETED,
                    message="Completed — ask your admin to reopen it.",
                )
                if sequence_blocked:
                    gate_open = False
                    pending_unlock_at = None
                else:
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
                out[key] = LessonAccess(status=AVAILABLE)
            elif pending_unlock_at is not None:
                out[key] = LessonAccess(
                    status=WAITING,
                    available_at=pending_unlock_at,
                    message="Available after the waiting period — or ask your admin for access.",
                )
            else:
                out[key] = LessonAccess(
                    status=LOCKED,
                    message="Finish the previous lesson first — or ask your admin for access.",
                )
            # This lesson consumes the gate; nothing further in the track opens
            # until it is completed and its own wait elapses.
            gate_open = False
            pending_unlock_at = None
            if not override:
                sequence_blocked = True

    return out


def section_access(
    db: Session, teacher: User, section: str | None = None
) -> dict[str, LessonAccess]:
    """Access keyed by lesson id, for one chosen class.

    ``section`` names the class the teacher is currently teaching. It applies to
    every grade that actually has a section by that name; any other grade falls
    back to its own first class, so a teacher looking at 6B still sees a truthful
    state for their Grade 5 lessons.
    """
    full = compute_access(db, teacher)
    if not full:
        return {}

    lesson_ids = {lesson_id for lesson_id, _ in full}
    grades = {
        lesson_id: grade
        for lesson_id, grade in db.execute(
            select(Lesson.id, Lesson.grade).where(Lesson.id.in_(lesson_ids))
        )
    }

    out: dict[str, LessonAccess] = {}
    for lesson_id, grade in grades.items():
        allowed = sections_for(teacher, grade)
        chosen = section if section in allowed else allowed[0]
        access = full.get((lesson_id, chosen))
        if access is not None:
            out[lesson_id] = access
    return out


def _as_utc(dt: datetime) -> datetime:
    """SQLite may hand back naive datetimes; treat those as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_lesson_available(
    db: Session, teacher: User, lesson_id: str, section: str | None = None
) -> bool:
    """True if the teacher may currently open/ground-on this lesson.

    With no section, the question is "in any of their classes?" — which is what
    the PDF viewer and the AI assistant need. A lesson finished with 6A but still
    ahead for 6B is one the teacher must be able to open and ask about; scoping
    those to a single class would lock her out of material she still has to
    teach.
    """
    if section is None:
        return any(
            access.status == AVAILABLE
            for (lid, _), access in compute_access(db, teacher).items()
            if lid == lesson_id
        )
    access = compute_access(db, teacher).get((lesson_id, section))
    return access is not None and access.status == AVAILABLE


def get_access_status(
    db: Session, teacher: User, lesson_id: str, section: str | None = None
) -> str | None:
    """The teacher's access status for one lesson in one class, or None if the
    lesson isn't assigned to them."""
    access = section_access(db, teacher, section).get(lesson_id)
    return access.status if access else None
