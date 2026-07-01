"""Deterministic lesson auto-creation + teacher assignment from a PDF filename.

No LLM involved: the filenames follow a strict convention
("Grade 7 Lesson 04 Light Sensor.pdf"), so a regex extracts the grade and
lesson number reliably. The lesson is named exactly as the PDF (minus the
extension), and assigned to every active teacher whose grades include that
grade and whose language matches the uploaded file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lesson, LessonAssignment, Progress, UploadedFile, User
from app.models.enums import LessonStatus, Role, UserStatus, WatchdogStatus
from app.utils import new_id

# "Grade 7 Lesson 04 Light Sensor.pdf" -> grade=7, lesson_no=4, rest="Light Sensor"
_FILENAME_RE = re.compile(
    r"^\s*Grade\s+(\d{1,2})\s+Lesson\s+(\d{1,3})\b\s*(.*)$",
    re.IGNORECASE,
)


@dataclass
class ParsedName:
    grade: int
    grade_token: str  # e.g. "G7"
    lesson_no: int
    title: str  # full filename without extension


@dataclass
class AssignResult:
    lesson_id: str | None = None
    lesson_title: str | None = None
    grade_token: str | None = None
    language: str | None = None
    assigned_count: int = 0
    teacher_names: list[str] = field(default_factory=list)
    note: str | None = None


def parse_lesson_filename(filename: str) -> ParsedName | None:
    base = filename
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    m = _FILENAME_RE.match(base)
    if not m:
        return None
    grade = int(m.group(1))
    lesson_no = int(m.group(2))
    if not (1 <= grade <= 12):
        return None
    return ParsedName(
        grade=grade,
        grade_token=f"G{grade}",
        lesson_no=lesson_no,
        title=base.strip(),
    )


def _language_matches(teacher_lang: str | None, lesson_lang: str | None) -> bool:
    if teacher_lang is None or lesson_lang is None:
        return False
    return teacher_lang == lesson_lang or teacher_lang == "both"


def _lesson_matches_teacher(lesson: Lesson, teacher: User) -> bool:
    """True if the grade + language rules would assign this lesson to the teacher."""
    return f"G{lesson.grade}" in set(teacher.grades or []) and _language_matches(
        teacher.language, lesson.language
    )


def _progress_untouched(p: Progress | None) -> bool:
    """A teacher hasn't started a lesson if there's no progress, or it's still
    not-started/0%/never opened. Touched lessons are kept (history preserved)."""
    if p is None:
        return True
    return (
        p.status == LessonStatus.not_started
        and p.percent_complete == 0
        and p.last_opened_at is None
    )


def sync_teacher_assignments(db: Session, teacher: User) -> int:
    """Assign a teacher every EXISTING lesson that matches their grade + language.

    The upload flow assigns new lessons to current teachers; this covers the
    other direction — a newly created or edited teacher catching up on lessons
    that were uploaded before them. Additive only (never removes). Returns the
    number of new assignments created.
    """
    if teacher.role != Role.teacher or teacher.status != UserStatus.active:
        return 0
    grades = set(teacher.grades or [])
    if not grades or not teacher.language:
        return 0

    already = {
        a.lesson_id
        for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
        )
    }
    has_progress = {
        p.lesson_id
        for p in db.scalars(
            select(Progress).where(Progress.teacher_id == teacher.id)
        )
    }

    lessons = db.scalars(select(Lesson).where(Lesson.language.isnot(None))).all()
    created = 0
    for lesson in lessons:
        if not _lesson_matches_teacher(lesson, teacher):
            continue
        if lesson.id not in already:
            db.add(
                LessonAssignment(
                    id=new_id("la"),
                    lesson_id=lesson.id,
                    teacher_id=teacher.id,
                    source="rule",
                )
            )
            created += 1
        if lesson.id not in has_progress:
            db.add(
                Progress(
                    id=new_id("p"),
                    teacher_id=teacher.id,
                    lesson_id=lesson.id,
                    status=LessonStatus.not_started,
                    percent_complete=0,
                    watchdog=WatchdogStatus.not_opened,
                    watchdog_message="Assigned by grade/language rule",
                )
            )
    return created


def prune_teacher_assignments(db: Session, teacher: User) -> int:
    """Smart strip (Option C): remove RULE-based assignments that no longer match
    the teacher's grades/language AND that the teacher has not started. Manual
    overrides and any lesson with real progress are always kept. Returns the
    number of assignments removed.
    """
    if teacher.role != Role.teacher:
        return 0

    assignments = db.scalars(
        select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
    ).all()
    progress_by_lesson = {
        p.lesson_id: p
        for p in db.scalars(
            select(Progress).where(Progress.teacher_id == teacher.id)
        )
    }

    removed = 0
    for a in assignments:
        if a.source != "rule":
            continue  # never auto-remove manual overrides
        lesson = db.get(Lesson, a.lesson_id)
        if lesson is None:
            continue
        if _lesson_matches_teacher(lesson, teacher):
            continue  # still matches — keep
        progress = progress_by_lesson.get(a.lesson_id)
        if not _progress_untouched(progress):
            continue  # teacher started it — keep their history
        db.delete(a)
        if progress is not None:
            db.delete(progress)
        removed += 1
    return removed


def assign_uploaded_file(
    db: Session,
    uploaded: UploadedFile,
    language: str,
    uploader_id: str,
) -> AssignResult:
    """Create/find the curriculum lesson for an uploaded PDF, link the file, and
    assign it to every matching teacher. Idempotent: re-uploading the same
    grade/lesson/language reuses the lesson and only adds new assignments.
    """
    parsed = parse_lesson_filename(uploaded.filename)
    if parsed is None:
        return AssignResult(
            note="Filename is not in 'Grade N Lesson M …' format — stored but not auto-assigned."
        )

    # Find or create the lesson for (grade, lesson_no, language).
    lesson = db.scalar(
        select(Lesson).where(
            Lesson.grade == parsed.grade,
            Lesson.lesson_no == parsed.lesson_no,
            Lesson.language == language,
        )
    )
    if lesson is None:
        lesson = Lesson(
            id=new_id("les"),
            title=parsed.title,
            grade=parsed.grade,
            subject="STEAM",
            school_id=None,  # curriculum-level, not tied to one school
            language=language,
            lesson_no=parsed.lesson_no,
            created_by=uploader_id,
        )
        db.add(lesson)
        db.flush()
    else:
        # Keep the title in sync with the latest uploaded filename.
        lesson.title = parsed.title

    uploaded.linked_lesson_id = lesson.id

    # Match active teachers by grade token + language.
    teachers = db.scalars(
        select(User).where(User.role == Role.teacher, User.status == UserStatus.active)
    ).all()
    matched = [
        t
        for t in teachers
        if parsed.grade_token in (t.grades or [])
        and _language_matches(t.language, language)
    ]

    existing_assignment_teachers = {
        a.teacher_id for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.lesson_id == lesson.id)
        )
    }
    existing_progress = {
        p.teacher_id for p in db.scalars(
            select(Progress).where(Progress.lesson_id == lesson.id)
        )
    }

    for t in matched:
        if t.id not in existing_assignment_teachers:
            db.add(
                LessonAssignment(
                    id=new_id("la"), lesson_id=lesson.id, teacher_id=t.id, source="rule"
                )
            )
        if t.id not in existing_progress:
            db.add(
                Progress(
                    id=new_id("p"),
                    teacher_id=t.id,
                    lesson_id=lesson.id,
                    status=LessonStatus.not_started,
                    percent_complete=0,
                    watchdog=WatchdogStatus.not_opened,
                    watchdog_message="Newly assigned — not opened yet",
                )
            )

    return AssignResult(
        lesson_id=lesson.id,
        lesson_title=lesson.title,
        grade_token=parsed.grade_token,
        language=language,
        assigned_count=len(matched),
        teacher_names=[t.name for t in matched],
    )
