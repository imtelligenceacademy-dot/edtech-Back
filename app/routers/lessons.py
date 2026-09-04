"""Lessons + teacher assignment (the 'Lessons DB' and 'Connection DB' edges).

Scoping:
- teacher       -> only lessons assigned to them
- school-admin  -> lessons in their school
- super-admin   -> everything, plus create/assign
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from datetime import datetime, timezone

from app.database import get_db
from app.deps import assert_school_scope, get_current_user, require_capability, require_roles
from app.models import AccessRequest, Lesson, LessonAssignment, Progress, Slide, UploadedFile, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.services.file_storage import resolve_stored_file
from app.schemas.lesson import (
    AssignmentRequest,
    AssignmentSet,
    BulkAssignment,
    BulkAssignmentPreview,
    BulkAssignmentResult,
    ClassSummary,
    LessonCreate,
    LessonOut,
    OverrideRequest,
    SlideOut,
    TeacherAccessOut,
    TeacherAccessTrack,
    TeacherLessonAccessRow,
)
from app.services.lesson_access import (
    COURSE_ORDER,
    LessonAccess,
    compute_access,
    lesson_order_key,
    section_access,
)
from app.services.sections import (
    all_sections,
    ensure_progress_rows,
    find_progress,
    resolve_section,
    sections_for,
)
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
        year=lesson.year,
        course=lesson.course,
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
    section: str | None = Query(
        default=None,
        description="The class being taught. Omit for teachers with one class.",
    ),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[LessonOut]:
    stmt = _base_query()
    if current.role == Role.teacher:
        assigned = select(LessonAssignment.lesson_id).where(
            LessonAssignment.teacher_id == current.id
        )
        stmt = stmt.where(Lesson.id.in_(assigned))
        access = section_access(db, current, section)
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


@router.get("/my-classes", response_model=list[ClassSummary])
def my_classes(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[ClassSummary]:
    """Where each of a teacher's classes has got to.

    The teacher picks a grade, and — when they take that grade more than once
    — a class, before teaching. Both pickers need the same thing: how far each
    class is, what opens next for them, and where they stopped. Computing it
    here keeps the picker honest, because it is the same access calculation the
    lesson list and the assistant use rather than a second guess at it.

    A teacher with one class per grade gets one row per grade with an empty
    section, and never sees a class named anywhere.
    """
    if current.role != Role.teacher:
        return []

    access = compute_access(db, current)
    if not access:
        return []

    lesson_ids = {lesson_id for lesson_id, _ in access}
    lessons = {l.id: l for l in db.scalars(select(Lesson).where(Lesson.id.in_(lesson_ids)))}
    progress = {
        (p.lesson_id, p.section): p
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == current.id, Progress.lesson_id.in_(lesson_ids)
            )
        )
    }

    # Group by class, then walk each in teaching order so "next" is the next
    # lesson that class actually reaches, not merely the first unfinished one
    # in the database.
    by_class: dict[tuple[int, str], list[Lesson]] = {}
    for (lesson_id, section) in access:
        lesson = lessons.get(lesson_id)
        if lesson is not None:
            by_class.setdefault((lesson.grade, section), []).append(lesson)

    out: list[ClassSummary] = []
    for (grade, section), group in sorted(by_class.items()):
        group.sort(key=lesson_order_key)
        row = ClassSummary(
            grade=grade,
            section=section,
            total=len(group),
            completed=sum(
                1
                for l in group
                if (a := access.get((l.id, section))) and a.status == "completed"
            ),
        )
        # The first lesson this class has not finished: what they open next, or
        # what they are waiting on.
        for lesson in group:
            a = access.get((lesson.id, section))
            if a is None or a.status == "completed":
                continue
            p = progress.get((lesson.id, section))
            row.next_lesson_id = lesson.id
            row.next_title = lesson.title
            row.next_status = a.status
            row.available_at = a.available_at
            row.last_slide = p.last_slide if p else None
            row.slide_total = p.slide_total if p else None
            break
        out.append(row)
    return out


@router.get("/{lesson_id}", response_model=LessonOut)
def get_lesson(
    lesson_id: str,
    section: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> LessonOut:
    lesson = db.scalar(_base_query().where(Lesson.id == lesson_id))
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    if current.role == Role.teacher:
        if current.id not in {a.teacher_id for a in lesson.assignments}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not assigned to you")
        access = section_access(db, current, section).get(lesson.id)
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


@router.delete("/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_lesson(
    lesson_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> Response:
    """Fully remove a lesson: its backing PDF files (bytes + rows), plus its
    assignments, progress, access requests, and slides (all ondelete=CASCADE).
    After this the lesson is gone for teachers and in Access Control."""
    lesson = db.get(Lesson, lesson_id)
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    for f in db.scalars(select(UploadedFile).where(UploadedFile.linked_lesson_id == lesson_id)):
        if f.storage_path:
            path = resolve_stored_file(f.storage_path)
            if path is not None:
                path.unlink(missing_ok=True)
        db.delete(f)
    db.delete(lesson)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Bulk assignment — the same teachers added to or removed from many lessons in
# one transaction. Registered before the "/{lesson_id}/..." routes so the paths
# stay unambiguous at a glance.
# --------------------------------------------------------------------------- #
MAX_BULK_LESSONS = 500


def _resolve_bulk(db: Session, payload: BulkAssignment) -> tuple[list[Lesson], set[str], set[str]]:
    """Validate a bulk edit and return (lessons, add_ids, remove_ids).

    A teacher named on both sides would make the result depend on the order the
    two halves ran in, so that is rejected rather than silently resolved.
    """
    lesson_ids = list(dict.fromkeys(payload.lesson_ids))
    if not lesson_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No lessons selected"
        )
    if len(lesson_ids) > MAX_BULK_LESSONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Select at most {MAX_BULK_LESSONS} lessons at once",
        )

    add = set(payload.add_teacher_ids)
    remove = set(payload.remove_teacher_ids)
    if add & remove:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A teacher cannot be added and removed in the same request",
        )
    if not add and not remove:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No teachers to add or remove"
        )

    school_teacher_ids = {
        t
        for t in db.scalars(
            select(User.id).where(
                User.role == Role.teacher,
                User.school_id == payload.school_id,
                User.status == UserStatus.active,
            )
        )
    }
    unknown = (add | remove) - school_teacher_ids
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Every teacher must be an active teacher in the chosen school",
        )

    lessons = list(db.scalars(_base_query().where(Lesson.id.in_(lesson_ids))))
    if not lessons:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such lessons")
    return lessons, add, remove


@router.post("/assignments/bulk-preview", response_model=BulkAssignmentPreview)
def preview_bulk_assignments(
    payload: BulkAssignment,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("assign-files")),
) -> BulkAssignmentPreview:
    """Count what a bulk edit would change — and what it would throw away.

    Adding is harmless, so the page applies that straight away. Removing is not:
    it deletes the teacher's progress on the lesson, which is the only record
    that they taught it.
    """
    lessons, add, remove = _resolve_bulk(db, payload)

    adds = 0
    removes = 0
    touched: set[str] = set()
    remove_pairs: list[tuple[str, str]] = []
    for lesson in lessons:
        assigned = {a.teacher_id for a in lesson.assignments}
        new_for_lesson = add - assigned
        gone_for_lesson = remove & assigned
        adds += len(new_for_lesson)
        removes += len(gone_for_lesson)
        if new_for_lesson or gone_for_lesson:
            touched.add(lesson.id)
        remove_pairs.extend((lesson.id, teacher_id) for teacher_id in gone_for_lesson)

    progress_lost = 0
    losing: set[str] = set()
    if remove_pairs:
        lesson_ids = {lesson_id for lesson_id, _ in remove_pairs}
        teacher_ids = {teacher_id for _, teacher_id in remove_pairs}
        pairs = set(remove_pairs)
        started = [
            row
            for row in db.scalars(
                select(Progress).where(
                    Progress.lesson_id.in_(lesson_ids),
                    Progress.teacher_id.in_(teacher_ids),
                    Progress.status != LessonStatus.not_started,
                )
            )
            if (row.lesson_id, row.teacher_id) in pairs
        ]
        progress_lost = len(started)
        if started:
            names = db.scalars(
                select(User.name).where(User.id.in_({row.teacher_id for row in started}))
            )
            losing = set(names)

    return BulkAssignmentPreview(
        lessons=len(touched),
        adds=adds,
        removes=removes,
        progress_lost=progress_lost,
        teachers_losing_progress=sorted(losing),
    )


@router.post("/assignments/bulk", response_model=BulkAssignmentResult)
def bulk_assignments(
    payload: BulkAssignment,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("assign-files")),
) -> BulkAssignmentResult:
    """Apply one assignment edit across many lessons, in one transaction."""
    lessons, add, remove = _resolve_bulk(db, payload)

    # A progress row per class needs each teacher's sections, so load the
    # teachers once for the whole edit rather than per lesson.
    teachers_by_id = {
        t.id: t for t in db.scalars(select(User).where(User.id.in_(add)))
    }

    added = 0
    removed = 0
    touched: set[str] = set()

    for lesson in lessons:
        assigned = {a.teacher_id for a in lesson.assignments}
        for teacher_id in add - assigned:
            db.add(
                LessonAssignment(
                    id=new_id("la"),
                    lesson_id=lesson.id,
                    teacher_id=teacher_id,
                    source="manual",
                )
            )
            teacher = teachers_by_id.get(teacher_id)
            if teacher is not None:
                ensure_progress_rows(db, teacher, lesson, "Manually assigned — not opened yet")
            added += 1
            touched.add(lesson.id)

        gone = remove & assigned
        if gone:
            for assignment in db.scalars(
                select(LessonAssignment).where(
                    LessonAssignment.lesson_id == lesson.id,
                    LessonAssignment.teacher_id.in_(gone),
                )
            ):
                db.delete(assignment)
            for progress in db.scalars(
                select(Progress).where(
                    Progress.lesson_id == lesson.id, Progress.teacher_id.in_(gone)
                )
            ):
                db.delete(progress)
            removed += len(gone)
            touched.add(lesson.id)

    db.commit()

    refreshed = list(
        db.scalars(
            _base_query()
            .where(Lesson.id.in_({lesson.id for lesson in lessons}))
            .execution_options(populate_existing=True)
        )
    )
    return BulkAssignmentResult(
        lessons_touched=len(touched),
        assignments_added=added,
        assignments_removed=removed,
        lessons=[_to_out(lesson) for lesson in refreshed],
    )


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
        ensure_progress_rows(db, teacher, lesson, "Manually assigned — not opened yet")
        db.commit()

    lesson = db.scalar(
        _base_query().where(Lesson.id == lesson_id).execution_options(populate_existing=True)
    )
    return _to_out(lesson)


@router.put("/{lesson_id}/assignments", response_model=LessonOut)
def replace_assignments(
    lesson_id: str,
    payload: AssignmentSet,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("assign-files")),
) -> LessonOut:
    """Set, in one transaction, exactly which of a school's teachers have this
    lesson.

    The Access Control page edits one school at a time, so this replaces only
    that school's assignments and leaves every other school's alone. Doing it as
    a set rather than a call per teacher means a failure halfway can no longer
    leave a lesson assigned to some of the teachers the admin chose and not
    others.
    """
    lesson = db.scalar(_base_query().where(Lesson.id == lesson_id))
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    wanted = set(payload.teacher_ids)
    school_teachers = {
        t.id: t
        for t in db.scalars(
            select(User).where(
                User.role == Role.teacher,
                User.school_id == payload.school_id,
                User.status == UserStatus.active,
            )
        )
    }
    unknown = wanted - school_teachers.keys()
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Every teacher must be an active teacher in the chosen school",
        )

    current_in_school = {
        a.teacher_id for a in lesson.assignments if a.teacher_id in school_teachers
    }
    to_add = wanted - current_in_school
    to_remove = current_in_school - wanted

    for teacher_id in to_add:
        db.add(
            LessonAssignment(
                id=new_id("la"),
                lesson_id=lesson.id,
                teacher_id=teacher_id,
                source="manual",
            )
        )
        ensure_progress_rows(
            db, school_teachers[teacher_id], lesson, "Manually assigned — not opened yet"
        )

    if to_remove:
        for assignment in db.scalars(
            select(LessonAssignment).where(
                LessonAssignment.lesson_id == lesson.id,
                LessonAssignment.teacher_id.in_(to_remove),
            )
        ):
            db.delete(assignment)
        for progress in db.scalars(
            select(Progress).where(
                Progress.lesson_id == lesson.id, Progress.teacher_id.in_(to_remove)
            )
        ):
            db.delete(progress)

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
        (p.lesson_id, p.section): p
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == teacher_id,
                Progress.lesson_id.in_(assigned_ids),
            )
        )
    }

    # Group lessons into (grade, language, year, section) tracks, ordered by
    # course then lesson number — matching the sequencing in lesson_access. A
    # teacher who takes three classes of one grade has three tracks there, each
    # at its own point in the curriculum, which is exactly what the admin needs
    # to see before reopening a lesson for one of them.
    tracks: dict[
        tuple[int, str | None, int, str], list[tuple[int, TeacherLessonAccessRow]]
    ] = {}
    for lid in assigned_ids:
        lesson = lessons.get(lid)
        if lesson is None:
            continue
        for section in sections_for(teacher, lesson.grade):
            a = access.get((lid, section))
            p = progress.get((lid, section))
            row = TeacherLessonAccessRow(
                lesson_id=lid,
                title=lesson.title,
                grade=lesson.grade,
                section=section,
                language=lesson.language,
                course=lesson.course,
                lesson_no=lesson.lesson_no,
                status=a.status if a else "locked",
                available_at=a.available_at if a else None,
                percent_complete=p.percent_complete if p else 0,
                completed_at=p.completed_at if p else None,
                unlocked_override=bool(p and p.unlocked_override),
            )
            order = COURSE_ORDER.get(lesson.course or "", 0)
            key = (lesson.grade, lesson.language, lesson.year, section)
            tracks.setdefault(key, []).append((order, row))

    track_out = []
    for (grade, language, year, section), rows in sorted(
        tracks.items(), key=lambda kv: (kv[0][0], kv[0][3], kv[0][1] or "", kv[0][2])
    ):
        rows.sort(
            key=lambda cr: (
                cr[0],
                cr[1].lesson_no if cr[1].lesson_no is not None else 10_000,
                cr[1].title,
            )
        )
        track_out.append(
            TeacherAccessTrack(
                grade=grade,
                section=section,
                language=language,
                year=year,
                lessons=[r for _, r in rows],
            )
        )

    return TeacherAccessOut(
        teacher_id=teacher.id,
        teacher_name=teacher.name,
        email=teacher.email,
        school_id=teacher.school_id,
        grades=list(teacher.grades or []),
        sections=all_sections(teacher),
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

    lesson = db.get(Lesson, lesson_id)
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    # An override belongs to one class. Unlocking a lesson for 6B must not also
    # reopen it for 6A, which finished it last week.
    section = resolve_section(teacher, lesson.grade, payload.section)
    if section is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That class isn't one of this teacher's for the grade.",
        )

    progress = find_progress(db, teacher_id, lesson_id, section)
    if progress is None:
        progress = Progress(
            id=new_id("p"), teacher_id=teacher_id, lesson_id=lesson_id, section=section
        )
        db.add(progress)
    progress.unlocked_override = payload.unlocked

    # Granting access here also resolves any pending request for this lesson.
    if payload.unlocked:
        pending = db.scalars(
            select(AccessRequest).where(
                AccessRequest.teacher_id == teacher_id,
                AccessRequest.lesson_id == lesson_id,
                AccessRequest.section == section,
                AccessRequest.status == "pending",
            )
        )
        for req in pending:
            req.status = "granted"
            req.resolved_by = admin.id
            req.resolved_at = datetime.now(timezone.utc)
    db.commit()

    access = compute_access(db, teacher).get((lesson_id, section))
    return TeacherLessonAccessRow(
        lesson_id=lesson_id,
        title=lesson.title,
        grade=lesson.grade,
        section=section,
        language=lesson.language,
        lesson_no=lesson.lesson_no,
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
