"""Lessons + teacher assignment (the 'Lessons DB' and 'Connection DB' edges).

Scoping:
- teacher       -> only lessons assigned to them
- school-admin  -> lessons in their school
- super-admin   -> everything, plus create/assign
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from datetime import datetime, timezone

from app.database import get_db
from app.deps import assert_school_scope, get_current_user, require_capability, require_roles
from app.models import AccessRequest, Lesson, LessonAssignment, Progress, Slide, User
from app.models.enums import Role, UserStatus
from app.schemas.lesson import (
    AssignmentRequest,
    LessonCreate,
    LessonOut,
    OverrideRequest,
    SlideOut,
    TeacherAccessOut,
    TeacherAccessTrack,
    TeacherLessonAccessRow,
)
from app.services.lesson_access import LessonAccess, compute_access
from app.utils import new_id

router = APIRouter(prefix="/api/lessons", tags=["lessons"])


def _to_out(lesson: Lesson, access: LessonAccess | None = None) -> LessonOut:
    return LessonOut(
        id=lesson.id,
        title=lesson.title,
        grade=lesson.grade,
        subject=lesson.subject,
        school_id=lesson.school_id,
        language=lesson.language,
        lesson_no=lesson.lesson_no,
        due_date=lesson.due_date,
        created_by=lesson.created_by,
        file_id=lesson.uploaded_files[0].id if lesson.uploaded_files else None,
        slides=[SlideOut.model_validate(s) for s in lesson.slides],
        assigned_teacher_ids=[a.teacher_id for a in lesson.assignments],
        access_status=access.status if access else None,
        available_at=access.available_at if access else None,
        access_message=access.message if access else None,
    )


def _base_query():
    return select(Lesson).options(
        selectinload(Lesson.slides),
        selectinload(Lesson.assignments),
        selectinload(Lesson.uploaded_files),
    )


@router.get("", response_model=list[LessonOut])
def list_lessons(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[LessonOut]:
    stmt = _base_query()
    if current.role == Role.teacher:
        assigned = select(LessonAssignment.lesson_id).where(
            LessonAssignment.teacher_id == current.id
        )
        stmt = stmt.where(Lesson.id.in_(assigned))
        access = compute_access(db, current)
        return [
            _to_out(l, access.get(l.id))
            for l in db.scalars(stmt.order_by(Lesson.created_at.desc()))
        ]
    elif current.role == Role.school_admin:
        # A school-admin sees lessons authored for their school OR any lesson
        # assigned to one of their teachers (covers global curriculum lessons).
        school_teachers = select(User.id).where(User.school_id == current.school_id)
        assigned_in_school = select(LessonAssignment.lesson_id).where(
            LessonAssignment.teacher_id.in_(school_teachers)
        )
        stmt = stmt.where(
            (Lesson.school_id == current.school_id) | (Lesson.id.in_(assigned_in_school))
        )
    return [_to_out(l) for l in db.scalars(stmt.order_by(Lesson.created_at.desc()))]


@router.get("/{lesson_id}", response_model=LessonOut)
def get_lesson(
    lesson_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> LessonOut:
    lesson = db.scalar(_base_query().where(Lesson.id == lesson_id))
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    if current.role == Role.teacher:
        if current.id not in {a.teacher_id for a in lesson.assignments}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not assigned to you")
        access = compute_access(db, current).get(lesson.id)
        if access is None or access.status != "available":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(access.message if access else None)
                or "This lesson isn't available yet — ask your admin for access.",
            )
        return _to_out(lesson, access)
    assert_school_scope(current, lesson.school_id)
    return _to_out(lesson)


@router.post("", response_model=LessonOut, status_code=status.HTTP_201_CREATED)
def create_lesson(
    payload: LessonCreate,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("upload-files")),
) -> LessonOut:
    lesson = Lesson(
        id=new_id("les"),
        title=payload.title.strip(),
        grade=payload.grade,
        subject=payload.subject,
        school_id=payload.school_id,
        created_by=current.id,
        due_date=payload.due_date,
    )
    for s in payload.slides:
        lesson.slides.append(
            Slide(
                id=new_id("sl"),
                index=s.index,
                title=s.title,
                body=s.body,
                image_url=s.image_url,
            )
        )
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return _to_out(lesson)


@router.post("/{lesson_id}/assign", response_model=LessonOut)
def assign_teacher(
    lesson_id: str,
    payload: AssignmentRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("assign-files")),
) -> LessonOut:
    """Manually assign a lesson to a teacher — an override that intentionally
    bypasses the grade/language auto-rules (so cross-school exceptions are
    allowed). Also seeds the teacher's progress row.
    """
    lesson = db.scalar(_base_query().where(Lesson.id == lesson_id))
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    teacher = db.get(User, payload.teacher_id)
    if teacher is None or teacher.role != Role.teacher or teacher.status != UserStatus.active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid teacher")

    if payload.teacher_id not in {a.teacher_id for a in lesson.assignments}:
        db.add(
            LessonAssignment(
                id=new_id("la"),
                lesson_id=lesson.id,
                teacher_id=payload.teacher_id,
                source="manual",
            )
        )
        if not db.scalar(
            select(Progress).where(
                Progress.lesson_id == lesson.id, Progress.teacher_id == payload.teacher_id
            )
        ):
            db.add(
                Progress(
                    id=new_id("p"),
                    teacher_id=payload.teacher_id,
                    lesson_id=lesson.id,
                    watchdog_message="Manually assigned — not opened yet",
                )
            )
        db.commit()

    lesson = db.scalar(
        _base_query().where(Lesson.id == lesson_id).execution_options(populate_existing=True)
    )
    return _to_out(lesson)


# --------------------------------------------------------------------------- #
# Super-admin lesson-access management — view a teacher's sequential unlock
# state and override individual lessons (bypass the wait / reopen a completed
# lesson). Defined before the dynamic "/{lesson_id}" routes so "access" is never
# mistaken for a lesson id (segment counts differ, but order keeps it clear).
# --------------------------------------------------------------------------- #
def _grade_label(grade: int) -> str:
    return f"G{grade}"


@router.get("/access/{teacher_id}", response_model=TeacherAccessOut)
def teacher_access(
    teacher_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(Role.super_admin)),
) -> TeacherAccessOut:
    """Full per-track unlock state for one teacher (super-admin override page)."""
    teacher = db.get(User, teacher_id)
    if teacher is None or teacher.role != Role.teacher:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")

    access = compute_access(db, teacher)
    assigned_ids = [
        a.lesson_id
        for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.teacher_id == teacher_id)
        )
    ]
    lessons = {l.id: l for l in db.scalars(select(Lesson).where(Lesson.id.in_(assigned_ids)))}
    progress = {
        p.lesson_id: p
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == teacher_id,
                Progress.lesson_id.in_(assigned_ids),
            )
        )
    }

    # Group lessons into (grade, language) tracks, ordered by lesson number.
    tracks: dict[tuple[int, str | None], list[TeacherLessonAccessRow]] = {}
    for lid in assigned_ids:
        lesson = lessons.get(lid)
        if lesson is None:
            continue
        a = access.get(lid)
        p = progress.get(lid)
        row = TeacherLessonAccessRow(
            lesson_id=lid,
            title=lesson.title,
            grade=lesson.grade,
            language=lesson.language,
            lesson_no=lesson.lesson_no,
            status=a.status if a else "locked",
            available_at=a.available_at if a else None,
            percent_complete=p.percent_complete if p else 0,
            completed_at=p.completed_at if p else None,
            unlocked_override=bool(p and p.unlocked_override),
        )
        tracks.setdefault((lesson.grade, lesson.language), []).append(row)

    track_out = []
    for (grade, language), rows in sorted(tracks.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        rows.sort(key=lambda r: (r.lesson_no if r.lesson_no is not None else 10_000, r.title))
        track_out.append(TeacherAccessTrack(grade=grade, language=language, lessons=rows))

    return TeacherAccessOut(
        teacher_id=teacher.id,
        teacher_name=teacher.name,
        email=teacher.email,
        school_id=teacher.school_id,
        grades=list(teacher.grades or []),
        language=teacher.language,
        tracks=track_out,
    )


@router.patch("/access/{teacher_id}/{lesson_id}", response_model=TeacherLessonAccessRow)
def set_lesson_override(
    teacher_id: str,
    lesson_id: str,
    payload: OverrideRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_roles(Role.super_admin)),
) -> TeacherLessonAccessRow:
    """Grant or revoke a teacher's override on one lesson. Granting bypasses the
    waiting period and reopens a completed lesson; revoking returns it to the
    normal sequential rules."""
    teacher = db.get(User, teacher_id)
    if teacher is None or teacher.role != Role.teacher:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")

    assignment = db.scalar(
        select(LessonAssignment).where(
            LessonAssignment.lesson_id == lesson_id,
            LessonAssignment.teacher_id == teacher_id,
        )
    )
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not assigned to this teacher")

    progress = db.scalar(
        select(Progress).where(
            Progress.lesson_id == lesson_id, Progress.teacher_id == teacher_id
        )
    )
    if progress is None:
        progress = Progress(id=new_id("p"), teacher_id=teacher_id, lesson_id=lesson_id)
        db.add(progress)
    progress.unlocked_override = payload.unlocked

    # Granting access here also resolves any pending request for this lesson.
    if payload.unlocked:
        pending = db.scalars(
            select(AccessRequest).where(
                AccessRequest.teacher_id == teacher_id,
                AccessRequest.lesson_id == lesson_id,
                AccessRequest.status == "pending",
            )
        )
        for req in pending:
            req.status = "granted"
            req.resolved_by = admin.id
            req.resolved_at = datetime.now(timezone.utc)
    db.commit()

    lesson = db.get(Lesson, lesson_id)
    access = compute_access(db, teacher).get(lesson_id)
    return TeacherLessonAccessRow(
        lesson_id=lesson_id,
        title=lesson.title if lesson else lesson_id,
        grade=lesson.grade if lesson else 0,
        language=lesson.language if lesson else None,
        lesson_no=lesson.lesson_no if lesson else None,
        status=access.status if access else "locked",
        available_at=access.available_at if access else None,
        percent_complete=progress.percent_complete,
        completed_at=progress.completed_at,
        unlocked_override=progress.unlocked_override,
    )


@router.delete("/{lesson_id}/assign/{teacher_id}", response_model=LessonOut)
def unassign_teacher(
    lesson_id: str,
    teacher_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("assign-files")),
) -> LessonOut:
    """Remove a teacher's assignment to a lesson (and their progress for it).
    Note: re-uploading the lesson's PDF may re-add an auto-matching teacher.
    """
    lesson = db.scalar(_base_query().where(Lesson.id == lesson_id))
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    assignment = db.scalar(
        select(LessonAssignment).where(
            LessonAssignment.lesson_id == lesson_id,
            LessonAssignment.teacher_id == teacher_id,
        )
    )
    if assignment:
        db.delete(assignment)
    progress = db.scalar(
        select(Progress).where(
            Progress.lesson_id == lesson_id, Progress.teacher_id == teacher_id
        )
    )
    if progress:
        db.delete(progress)
    db.commit()

    lesson = db.scalar(
        _base_query().where(Lesson.id == lesson_id).execution_options(populate_existing=True)
    )
    return _to_out(lesson)
