"""Class sections — the several classes one teacher takes through one grade.

Most teachers have a single class per grade, and for them none of this is
visible. Their sections are undefined, every progress row carries the
empty-string section, and the product behaves exactly as it did before sections
existed. That is the default and it stays the default.

A super-admin names sections on a teacher's account when one teacher takes the
same grade more than once::

    {"G6": ["A", "B", "C", "D"]}

From then on that teacher picks a class before teaching, and each class carries
its own progress, its own place in the lesson sequence, and its own unlock
countdown. Without this, marking a lesson complete after teaching 6A would lock
it — the teacher could not open it again for 6B, 6C or 6D — and the wait before
the next lesson would start counting for classes that had not had this one.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lesson, Progress, User
from app.models.enums import LessonStatus, Role, WatchdogStatus
from app.utils import new_id

# The section of a grade that has only one class. Empty rather than NULL: both
# SQLite and Postgres treat NULLs as distinct in a UNIQUE constraint, so a
# nullable column would silently permit duplicate progress rows.
NO_SECTION = ""

# Bounds on what a super-admin may type. Generous — they exist to stop a typo
# becoming a thousand progress rows, not to express a real limit on classes.
MAX_SECTIONS_PER_GRADE = 12
MAX_LABEL_LENGTH = 16


def grade_token(grade: int | str) -> str:
    """The token form of a grade ("G6"), matching ``User.grades``."""
    return grade if isinstance(grade, str) else f"G{grade}"


def normalize_sections(
    raw: dict[str, list[str]] | None, grades: list[str] | None
) -> dict[str, list[str]]:
    """Clean a super-admin's section input into what gets stored.

    Labels are stripped and de-duplicated case-insensitively while keeping the
    case typed ("a" and "A" are one class, spelled the admin's way). Grades the
    teacher does not teach are dropped, so removing a grade from an account
    cannot leave its sections behind to reappear if that grade returns.
    """
    if not raw:
        return {}
    allowed = set(grades or [])
    out: dict[str, list[str]] = {}
    for token, labels in raw.items():
        if token not in allowed or not isinstance(labels, list):
            continue
        cleaned: list[str] = []
        seen: set[str] = set()
        for label in labels:
            if not isinstance(label, str):
                continue
            text = label.strip()[:MAX_LABEL_LENGTH]
            if not text or text.casefold() in seen:
                continue
            seen.add(text.casefold())
            cleaned.append(text)
            if len(cleaned) >= MAX_SECTIONS_PER_GRADE:
                break
        if cleaned:
            out[token] = cleaned
    return out


def sections_for(teacher: User, grade: int | str) -> list[str]:
    """Every section this teacher takes for one grade.

    Always non-empty: a grade with no named sections yields ``[NO_SECTION]``, so
    callers can iterate uniformly instead of special-casing the common teacher.
    """
    named = (teacher.sections or {}).get(grade_token(grade)) or []
    return list(named) if named else [NO_SECTION]


def has_named_sections(teacher: User, grade: int | str) -> bool:
    """True when this teacher takes more than one class of this grade — the only
    case in which a section is ever shown to them."""
    return len(sections_for(teacher, grade)) > 1


def all_sections(teacher: User) -> dict[str, list[str]]:
    """Sections for every grade the teacher teaches, named or not."""
    return {token: sections_for(teacher, token) for token in (teacher.grades or [])}


def resolve_section(teacher: User, grade: int | str, requested: str | None) -> str | None:
    """The section a request applies to, or None if it names one the teacher
    does not take.

    A missing section resolves to the teacher's first class for the grade. That
    is what keeps single-class teachers — and any older client that does not
    send a section at all — working unchanged.
    """
    allowed = sections_for(teacher, grade)
    if requested is None:
        return allowed[0]
    return requested if requested in allowed else None


def find_progress(
    db: Session, teacher_id: str, lesson_id: str, section: str
) -> Progress | None:
    """The one progress row for a teacher, lesson and section."""
    return db.scalar(
        select(Progress).where(
            Progress.teacher_id == teacher_id,
            Progress.lesson_id == lesson_id,
            Progress.section == section,
        )
    )


def ensure_progress_for_lessons(
    db: Session,
    teacher: User,
    lessons: list[Lesson],
    message: str | None = None,
) -> int:
    """Create the missing progress rows for a teacher across many lessons — one
    per lesson per section.

    Every place that assigns lessons goes through here rather than building a
    Progress by hand, so a teacher with four classes gets four rows and no
    caller has to remember that sections exist. One query covers the whole set,
    because assigning a teacher a year of curriculum is a normal thing to do.
    Returns the number of rows created.
    """
    if not lessons:
        return 0

    lesson_ids = [lesson.id for lesson in lessons]
    existing = {
        (p.lesson_id, p.section)
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == teacher.id,
                Progress.lesson_id.in_(lesson_ids),
            )
        )
    }

    created = 0
    for lesson in lessons:
        for section in sections_for(teacher, lesson.grade):
            if (lesson.id, section) in existing:
                continue
            existing.add((lesson.id, section))
            db.add(
                Progress(
                    id=new_id("p"),
                    teacher_id=teacher.id,
                    lesson_id=lesson.id,
                    section=section,
                    status=LessonStatus.not_started,
                    percent_complete=0,
                    watchdog=WatchdogStatus.not_opened,
                    watchdog_message=message,
                )
            )
            created += 1
    return created


def ensure_progress_rows(
    db: Session, teacher: User, lesson: Lesson, message: str | None = None
) -> int:
    """Create the missing progress rows for one teacher and one lesson."""
    return ensure_progress_for_lessons(db, teacher, [lesson], message)


def sync_progress_sections(
    db: Session,
    teacher: User,
    before: dict[str, list[str]],
    after: dict[str, list[str]],
) -> None:
    """Re-key a teacher's progress after a super-admin edits their sections.

    The teacher's existing work follows their first class, in both directions:

    - Naming sections on a grade that had none moves their rows onto the first
      label. They were pacing one class; that class is now 6A. Without this,
      adding sections mid-year would hide a term of progress behind a section
      nobody is keyed to.
    - Clearing every section moves the first label's rows back to the unnamed
      section, so the grade keeps working.

    Rows for sections that simply went away are left alone, not deleted. They
    are invisible while no section carries their label and come back intact if
    the label does — which makes an accidental removal recoverable rather than
    destructive.
    """
    if teacher.role != Role.teacher:
        return

    grade_of_lesson = {
        lesson_id: grade
        for lesson_id, grade in db.execute(
            select(Progress.lesson_id, Lesson.grade)
            .join(Lesson, Lesson.id == Progress.lesson_id)
            .where(Progress.teacher_id == teacher.id)
            .distinct()
        )
    }

    for token in set(before) | set(after) | set(teacher.grades or []):
        old = (before.get(token) or [NO_SECTION])[0]
        new = (after.get(token) or [NO_SECTION])[0]
        if old == new:
            continue

        rows = [
            p
            for p in db.scalars(
                select(Progress).where(
                    Progress.teacher_id == teacher.id, Progress.section == old
                )
            )
            if grade_token(grade_of_lesson.get(p.lesson_id, -1)) == token
        ]
        if not rows:
            continue

        # Never re-key onto a lesson that already has a row for the new section:
        # that would collide on (teacher, lesson, section) and, worse, silently
        # merge two classes' histories into one.
        taken = {
            p.lesson_id
            for p in db.scalars(
                select(Progress).where(
                    Progress.teacher_id == teacher.id, Progress.section == new
                )
            )
        }
        for row in rows:
            if row.lesson_id not in taken:
                row.section = new
