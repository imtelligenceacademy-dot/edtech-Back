"""Who may see which ICT Fair sections.

A section is filed under a school and tagged with the grades it is for. Both
have to be enforced, and until now only the school was: a Grade 6 teacher was
shown every section her school runs, including the Grade 1-3 projects, because
the grade tags were recorded and then never consulted.

The rules, in one place so the list, the flat project list and the PDF itself
cannot drift apart — the list hiding a section means nothing if the file behind
it still opens for anyone who has its id:

- **super-admin** sees every school and every grade.
- **school-admin** sees their own school, every grade in it. They administer the
  fair rather than teach it, so a grade they do not teach is still theirs to
  see.
- **teacher** sees their own school, and only sections tagged with a grade they
  actually teach — and only with ``ict_fair_access`` at all.

A section tagged with no grades reaches no teacher. That is deliberate: an
untagged section is one nobody has said who it is for, and guessing "everyone"
is how the Grade 1 projects ended up in front of the Grade 6 teacher. Admins
still see it, which is how it gets fixed.
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import FairProject, FairSection, User
from app.models.enums import Role


def teaches_any_grade(user: User, grades: list[str] | None) -> bool:
    """True if any of these grades is one the teacher takes.

    Compared as a set intersection rather than "the teacher's first grade",
    because a section covers several grades ("Grades 1-3" is one project three
    grades share) and a teacher may take several.
    """
    return bool(set(grades or []) & set(user.grades or []))


def can_see_fair(user: User) -> bool:
    """Whether this user reaches the ICT Fair at all, before any scoping."""
    if user.role == Role.teacher and not user.ict_fair_access:
        return False
    # Everyone below super-admin is pinned to a school. Without one there is no
    # scope to apply, so there is nothing they may be shown.
    if user.role != Role.super_admin and not user.school_id:
        return False
    return True


def section_visible_to(user: User, section: FairSection) -> bool:
    """Whether one section is this user's to see."""
    if not can_see_fair(user):
        return False
    if user.role == Role.super_admin:
        return True
    if section.school_id != user.school_id:
        return False
    if user.role == Role.teacher:
        return teaches_any_grade(user, section.grades)
    return True


def scope_sections(query: Select, user: User, school_id: str | None = None) -> Select:
    """Narrow a section query to the school this user may read.

    The school half is a SQL filter because it is a column; the grade half is
    not, because grades are a JSON list and the overlap test is a set
    intersection that SQLite and Postgres do not express the same way. Callers
    finish the job with :func:`section_visible_to`, which is also what the file
    gate uses, so the two cannot disagree.
    """
    if user.role == Role.super_admin:
        # A super-admin may filter to one school; everyone else *is* filtered.
        return query.where(FairSection.school_id == school_id) if school_id else query
    return query.where(FairSection.school_id == user.school_id)


def visible_sections(
    db: Session, user: User, school_id: str | None = None
) -> list[FairSection]:
    """Every section this user may see, ordered by title."""
    if not can_see_fair(user):
        return []
    query = scope_sections(select(FairSection), user, school_id)
    return [
        section
        for section in db.scalars(query.order_by(FairSection.title))
        if section_visible_to(user, section)
    ]


def can_open_fair_file(db: Session, user: User, file_id: str) -> bool:
    """Whether this user may open the PDF behind a fair project.

    Answered from the project's own section, so hiding a section from the list
    also closes the file. Without this a teacher who had a project's id could
    open another school's Grade 1 material — the list was scoped and the bytes
    were not.
    """
    section = db.scalar(
        select(FairSection)
        .join(FairProject, FairProject.section_id == FairSection.id)
        .where(FairProject.file_id == file_id)
    )
    if section is None:
        # Unfiled: no section means no school and no grades, so it belongs to
        # nobody but a super-admin, who is already allowed above this check.
        return False
    return section_visible_to(user, section)
