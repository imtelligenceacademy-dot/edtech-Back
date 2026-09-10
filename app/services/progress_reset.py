"""Putting a teacher's recorded progress back to untouched.

Two things make this necessary, and neither is covered by the unlock override.

A school trains its teachers on the platform before term starts, and they walk
through real lessons to learn it. Every one of those is then recorded as
completed, so on the first day of teaching the whole curriculum reads as done,
the sequence has moved on, and the reports count work that never happened in
front of a class.

And a teacher marks a lesson complete by mistake, which locks it, moves the
class on, and starts the next lesson's countdown.

The unlock override reopens a completed lesson so it can be taught again, but it
leaves the record saying completed at 100%. That is right for "let her back into
this one" and wrong for "this never happened" — which is what a reset is for.

Scope is chosen by the caller: one lesson or all of them, one class or all of
them. Everything else about the teacher is left alone. In particular their
assignments stay, so the lessons they had are the lessons they still have, and
their conversations with the assistant stay, because those are the teacher's own
notes about the material rather than a record of progress.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Progress, User
from app.models.enums import LessonStatus, Role, WatchdogStatus


@dataclass
class ResetResult:
    """What the reset actually did, in figures the admin can check.

    `lessons` is rows, not distinct lessons: a teacher who takes four classes
    through one lesson has four records of it, and resetting that lesson clears
    all four. The counts are reported after the fact rather than predicted, so
    what the screen says is what happened.
    """

    lessons: int = 0
    completed_cleared: int = 0
    started_cleared: int = 0
    overrides_cleared: int = 0
    classes: int = 0


def reset_progress(
    db: Session,
    teacher: User,
    *,
    lesson_id: str | None = None,
    section: str | None = None,
    note: str = "Reset by an administrator",
) -> ResetResult:
    """Return a teacher's progress to never-opened.

    ``lesson_id`` of None means every lesson; ``section`` of None means every
    class. A row that is already untouched is counted but costs nothing, so
    running this twice is safe and the second run simply reports zero of the
    things it cleared.
    """
    if teacher.role != Role.teacher:
        return ResetResult()

    query = select(Progress).where(Progress.teacher_id == teacher.id)
    if lesson_id is not None:
        query = query.where(Progress.lesson_id == lesson_id)
    if section is not None:
        query = query.where(Progress.section == section)

    rows = list(db.scalars(query))
    result = ResetResult(lessons=len(rows), classes=len({r.section for r in rows}))

    for row in rows:
        if row.status == LessonStatus.completed:
            result.completed_cleared += 1
        elif row.status != LessonStatus.not_started or row.percent_complete:
            result.started_cleared += 1
        if row.unlocked_override:
            result.overrides_cleared += 1

        row.status = LessonStatus.not_started
        row.percent_complete = 0
        row.last_slide = None
        row.slide_total = None
        row.last_opened_at = None
        row.completed_at = None
        # The override goes too. It exists to reopen a lesson the teacher had
        # finished; once the lesson is untouched there is nothing to reopen, and
        # leaving it set would quietly exempt that lesson from the sequence.
        row.unlocked_override = False
        row.watchdog = WatchdogStatus.not_opened
        row.watchdog_message = note

    return result
