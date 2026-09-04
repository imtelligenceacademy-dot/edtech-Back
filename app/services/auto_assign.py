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
from app.models.enums import LessonStatus, Role, UserStatus
from app.services.sections import ensure_progress_for_lessons
from app.utils import new_id

# "Grade 7 python lesson 04 Variables.pdf"  -> grade=7, course="python",   lesson_no=4
# "Grade 7 micro:bit lesson 04 Buzzer.pdf"  -> grade=7, course="microbit", lesson_no=4
# "Grade 7 Lesson 04 Light Sensor.pdf"      -> grade=7, course=None (legacy), lesson_no=4
# The course keyword (python / micro:bit) is optional and sits between the grade
# and "lesson"; when absent the lesson belongs to a single default course.
_FILENAME_RE = re.compile(
    r"^\s*Grade\s+(\d{1,2})\s+(?:(python|micro:?bit)\s+)?lesson\s+(\d{1,3})\b\s*(.*)$",
    re.IGNORECASE,
)


@dataclass
class ParsedName:
    grade: int
    grade_token: str  # e.g. "G7"
    lesson_no: int
    title: str  # full filename without extension
    course: str | None = None  # "python" | "microbit" | None


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
    course_raw = m.group(2)
    lesson_no = int(m.group(3))
    if not (1 <= grade <= 12):
        return None
    course = None
    if course_raw:
        course = "microbit" if "micro" in course_raw.lower() else "python"
    return ParsedName(
        grade=grade,
        grade_token=f"G{grade}",
        lesson_no=lesson_no,
        title=base.strip(),
        course=course,
    )


def _language_matches(teacher_lang: str | None, lesson_lang: str | None) -> bool:
    if teacher_lang is None or lesson_lang is None:
        return False
    return teacher_lang == lesson_lang or teacher_lang == "both"


def _year_matches(teacher: User, lesson: Lesson) -> bool:
    """True if the lesson's curriculum year equals the teacher's school's current
    year. Teachers with no school match nothing (they receive no curriculum)."""
    school = teacher.school
    return school is not None and lesson.year == school.program_year


def _lesson_matches_teacher(lesson: Lesson, teacher: User) -> bool:
    """True if the grade + language + year rules would assign this lesson to the
    teacher (grade in their grades, language matches, and the lesson belongs to
    their school's current curriculum year)."""
    return (
        f"G{lesson.grade}" in set(teacher.grades or [])
        and _language_matches(teacher.language, lesson.language)
        and _year_matches(teacher, lesson)
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
    lessons = db.scalars(select(Lesson).where(Lesson.language.isnot(None))).all()
    matching = [l for l in lessons if _lesson_matches_teacher(l, teacher)]

    created = 0
    for lesson in matching:
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

    # One progress row per lesson per class the teacher takes for its grade.
    ensure_progress_for_lessons(
        db, teacher, matching, "Assigned by grade/language rule"
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
    # Every class's row for a lesson, not one of them: a lesson 6A has started
    # is history worth keeping even if 6B never opened it.
    progress_by_lesson: dict[str, list[Progress]] = {}
    for p in db.scalars(select(Progress).where(Progress.teacher_id == teacher.id)):
        progress_by_lesson.setdefault(p.lesson_id, []).append(p)

    removed = 0
    for a in assignments:
        if a.source != "rule":
            continue  # never auto-remove manual overrides
        lesson = db.get(Lesson, a.lesson_id)
        if lesson is None:
            continue
        if _lesson_matches_teacher(lesson, teacher):
            continue  # still matches — keep
        rows = progress_by_lesson.get(a.lesson_id) or []
        if not all(_progress_untouched(row) for row in rows):
            continue  # a class started it — keep their history
        db.delete(a)
        for row in rows:
            db.delete(row)
        removed += 1
    return removed


def assign_uploaded_file(
    db: Session,
    uploaded: UploadedFile,
    language: str,
    uploader_id: str,
    year: int = 2,
) -> AssignResult:
    """Create/find the curriculum lesson for an uploaded PDF, link the file, and
    assign it to every matching teacher. Idempotent: re-uploading the same
    grade/lesson/language/course/year reuses the lesson and only adds new
    assignments.
    """
    parsed = parse_lesson_filename(uploaded.filename)
    if parsed is None:
        return AssignResult(
            note="Filename is not in 'Grade N [python|micro:bit] lesson M …' format — stored but not auto-assigned."
        )

    # Find or create the lesson for (grade, lesson_no, language, course, year).
    # Course is part of the key so e.g. "python lesson 04" and "micro:bit lesson
    # 04" are distinct lessons rather than colliding on grade+lesson_no.
    lesson = db.scalar(
        select(Lesson).where(
            Lesson.grade == parsed.grade,
            Lesson.lesson_no == parsed.lesson_no,
            Lesson.language == language,
            Lesson.course == parsed.course,
            Lesson.year == year,
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
            year=year,
            course=parsed.course,
            lesson_no=parsed.lesson_no,
            created_by=uploader_id,
        )
        db.add(lesson)
        db.flush()
    else:
        # Keep the title in sync with the latest uploaded filename.
        lesson.title = parsed.title

    uploaded.linked_lesson_id = lesson.id

    # Match active teachers by grade token + language + curriculum year.
    teachers = db.scalars(
        select(User).where(User.role == Role.teacher, User.status == UserStatus.active)
    ).all()
    matched = [
        t
        for t in teachers
        if parsed.grade_token in (t.grades or [])
        and _language_matches(t.language, language)
        and _year_matches(t, lesson)
    ]

    existing_assignment_teachers = {
        a.teacher_id for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.lesson_id == lesson.id)
        )
    }
    for t in matched:
        if t.id not in existing_assignment_teachers:
            db.add(
                LessonAssignment(
                    id=new_id("la"), lesson_id=lesson.id, teacher_id=t.id, source="rule"
                )
            )
        ensure_progress_for_lessons(
            db, t, [lesson], "Newly assigned — not opened yet"
        )

    return AssignResult(
        lesson_id=lesson.id,
        lesson_title=lesson.title,
        grade_token=parsed.grade_token,
        language=language,
        assigned_count=len(matched),
        teacher_names=[t.name for t in matched],
    )


@dataclass
class UploadPreview:
    """What uploading one file would do, worked out from its name alone."""

    filename: str
    ok: bool  # the name parses; it will become a lesson
    note: str | None = None  # why not, when it doesn't
    lesson_title: str | None = None
    grade: int | None = None
    course: str | None = None
    lesson_no: int | None = None
    existing_lesson: bool = False  # adds to a lesson that already exists
    teacher_names: list[str] = field(default_factory=list)


def preview_uploads(
    db: Session, filenames: list[str], language: str, year: int
) -> list[UploadPreview]:
    """Answer "what happens if I upload these?" without uploading anything.

    Everything here comes from the filename plus the database, and it reuses the
    same parser and matching rules as the real upload — so what the admin is
    shown cannot drift from what they get.
    """
    teachers = db.scalars(
        select(User).where(User.role == Role.teacher, User.status == UserStatus.active)
    ).all()

    previews: list[UploadPreview] = []
    for filename in filenames:
        parsed = parse_lesson_filename(filename)
        if parsed is None:
            previews.append(
                UploadPreview(
                    filename=filename,
                    ok=False,
                    note="Name doesn't match 'Grade N [python|micro:bit] lesson M Title' — it would be stored but not assigned to anyone.",
                )
            )
            continue

        existing = db.scalar(
            select(Lesson).where(
                Lesson.grade == parsed.grade,
                Lesson.lesson_no == parsed.lesson_no,
                Lesson.language == language,
                Lesson.course == parsed.course,
                Lesson.year == year,
            )
        )
        # A stand-in for the lesson that would exist, so the matching rules are
        # the ones in _lesson_matches_teacher rather than a second copy of them.
        candidate = existing or Lesson(
            id="preview",
            title=parsed.title,
            grade=parsed.grade,
            subject="STEAM",
            language=language,
            year=year,
            course=parsed.course,
            lesson_no=parsed.lesson_no,
        )
        matched = [t for t in teachers if _lesson_matches_teacher(candidate, t)]

        previews.append(
            UploadPreview(
                filename=filename,
                ok=True,
                lesson_title=parsed.title,
                grade=parsed.grade,
                course=parsed.course,
                lesson_no=parsed.lesson_no,
                existing_lesson=existing is not None,
                teacher_names=[t.name for t in matched],
            )
        )
    return previews
