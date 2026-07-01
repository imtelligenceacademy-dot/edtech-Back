from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_capability
from app.models import Lesson, LessonAssignment, Progress, User
from app.models.enums import LessonStatus, Role, WatchdogStatus
from app.schemas.progress import ProgressOut, ProgressUpdate
from app.services.lesson_access import get_access_status
from app.utils import new_id

router = APIRouter(prefix="/api/progress", tags=["progress"])


def _compute_watchdog(
    percent: int, due: date | None, last_opened: datetime | None
) -> tuple[WatchdogStatus, str | None]:
    if percent >= 100:
        return WatchdogStatus.completed, None
    if last_opened is None and percent == 0:
        return WatchdogStatus.not_opened, "Not opened yet"
    if due is not None and due < date.today():
        return WatchdogStatus.late, f"Overdue (due {due.isoformat()}), {percent}% done"
    return WatchdogStatus.on_track, f"In progress — {percent}%"


def _existing_progress(db: Session, teacher_id: str, lesson_id: str) -> Progress | None:
    return db.scalar(
        select(Progress).where(
            Progress.lesson_id == lesson_id, Progress.teacher_id == teacher_id
        )
    )


def _guard_write_access(db: Session, teacher: User, lesson_id: str) -> Progress | None:
    """Enforce sequential unlocking on writes. Raises 403 for a lesson that's
    locked or still in its waiting period. For an already-completed lesson,
    returns its Progress row so the caller can no-op instead of downgrading it;
    otherwise returns None to proceed with the write."""
    access_status = get_access_status(db, teacher, lesson_id)
    if access_status in ("locked", "waiting"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This lesson isn't available yet — ask your admin for access.",
        )
    if access_status == "completed":
        return _existing_progress(db, teacher.id, lesson_id)
    return None


def _resolve_percent(payload: ProgressUpdate, total: int) -> int:
    """Completion percentage from the teacher's report. Raises 400 if neither a
    slide position nor an explicit completion flag was provided."""
    if payload.complete:
        return 100
    if payload.slide is not None:
        # No total known yet -> 0 (still marks the lesson as opened).
        return max(0, min(100, round(payload.slide / total * 100))) if total > 0 else 0
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Provide a slide number or set complete=true",
    )


@router.get("", response_model=list[ProgressOut])
def list_progress(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[Progress]:
    stmt = select(Progress)
    if current.role == Role.teacher:
        stmt = stmt.where(Progress.teacher_id == current.id)
    elif current.role == Role.school_admin:
        # Progress for teachers in the admin's school (lessons are now global).
        school_teachers = select(User.id).where(User.school_id == current.school_id)
        stmt = stmt.where(Progress.teacher_id.in_(school_teachers))
    return list(db.scalars(stmt.order_by(Progress.updated_at.desc())))


@router.post("/{lesson_id}", response_model=ProgressOut)
def update_progress(
    lesson_id: str,
    payload: ProgressUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("view-assigned-lessons")),
) -> Progress:
    """A teacher records the slide they stopped at (or marks the lesson complete)."""
    lesson = db.get(Lesson, lesson_id)
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    assigned = db.scalar(
        select(LessonAssignment.id).where(
            LessonAssignment.lesson_id == lesson_id,
            LessonAssignment.teacher_id == current.id,
        )
    )
    if not assigned:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Lesson not assigned to you")

    # Sequential unlocking: block writes to a lesson the teacher hasn't reached
    # yet (locked / still in its waiting period). A lesson they've already
    # completed is left untouched — re-saving it is a harmless no-op, never a 403.
    already_completed = _guard_write_access(db, current, lesson_id)
    if already_completed is not None:
        return already_completed  # already done; don't downgrade or error

    # Resolve total slides: prefer the viewer's count, else the stored one.
    total = payload.total or lesson.slide_count or 0
    if payload.total and payload.total != lesson.slide_count:
        lesson.slide_count = payload.total  # keep the lesson's count in sync

    percent = _resolve_percent(payload, total)

    now = datetime.now(timezone.utc)
    progress = _existing_progress(db, current.id, lesson_id)
    if progress is None:
        progress = Progress(id=new_id("p"), teacher_id=current.id, lesson_id=lesson_id)
        db.add(progress)

    progress.percent_complete = percent
    progress.last_opened_at = now
    progress.status = (
        LessonStatus.completed
        if percent >= 100
        else LessonStatus.in_progress
        if percent > 0
        else LessonStatus.not_started
    )
    # Stamp the completion time once, so the next lesson's wait can be measured.
    if percent >= 100:
        if progress.completed_at is None:
            progress.completed_at = now
        # Completing the lesson consumes any admin override: it returns to the
        # normal sequence — locked/complete, with the next lesson counting down.
        progress.unlocked_override = False
    else:
        progress.completed_at = None
    watchdog, message = _compute_watchdog(percent, lesson.due_date, now)
    progress.watchdog = watchdog
    progress.watchdog_message = message

    db.commit()
    db.refresh(progress)
    return progress
