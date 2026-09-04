"""Every record of what happened in a classroom is scoped to the class.

This bug has already shipped twice. Progress was keyed by (teacher, lesson), so
a teacher taking 6A, 6B and 6C through the same curriculum shared one row and
finishing a lesson with one class locked it for the others. Chat was keyed the
same way, so opening a lesson for 6B replayed what was asked in 6A. Both were
the same mistake in two places, made months apart, because "scope this by class"
was a rule written down nowhere and each table had to remember it alone.

That is what this file is: the rule, enforced. It reads the models rather than
listing tables by hand, so a table added next year is covered the day it exists.
"""

from __future__ import annotations

from sqlalchemy import inspect as sa_inspect

from app.database import Base
import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.models import AccessRequest, ChatMessage, Progress
from app.services.sections import SECTION_KEYED_MODELS

# Tables that pair a teacher with a lesson and are deliberately NOT per class.
# Every entry needs a reason, because the point of the test is that being on
# this list is a decision somebody made, not an oversight nobody noticed.
NOT_PER_CLASS: dict[str, str] = {
    # Which lessons a teacher has at all. A teacher of Grade 6 receives the
    # Grade 6 curriculum once; all of her Grade 6 classes are taught from it.
    # Splitting this per class would multiply every assignment for no meaning.
    "lesson_assignments": "a teacher's curriculum is the same in all her rooms",
}


def _teacher_lesson_tables() -> dict[str, set[str]]:
    """Every table that records something about a teacher and a lesson."""
    return {
        name: set(table.columns.keys())
        for name, table in Base.metadata.tables.items()
        if "teacher_id" in table.columns and "lesson_id" in table.columns
    }


def test_every_teacher_lesson_table_is_scoped_to_a_class():
    missing = [
        name
        for name, columns in _teacher_lesson_tables().items()
        if "section" not in columns and name not in NOT_PER_CLASS
    ]
    assert not missing, (
        "These tables record something about a teacher and a lesson but have no "
        f"class: {sorted(missing)}.\n"
        "A teacher who takes the same grade more than once teaches that lesson "
        "in several rooms, and this table would merge them — which is how the "
        "progress and chat bugs happened.\n"
        "Add a `section` column (String(16), not null, default \"\"), or add the "
        "table to NOT_PER_CLASS in this file with the reason it is genuinely "
        "the same in every room."
    )


def test_the_exemptions_still_exist():
    """A stale exemption is worse than none: it silently excuses a table that
    was renamed or dropped, and would excuse a new one that reuses the name."""
    tables = _teacher_lesson_tables()
    for name in NOT_PER_CLASS:
        assert name in tables, (
            f"{name} is exempted from class scoping but no longer pairs a "
            "teacher with a lesson. Remove it from NOT_PER_CLASS."
        )


def test_the_section_column_cannot_be_null():
    """Empty string, never NULL.

    SQLite and Postgres both treat NULLs as distinct inside a UNIQUE
    constraint, so a nullable section would let two progress rows exist for the
    same teacher and lesson — exactly what the constraint is there to prevent.
    """
    for name, columns in _teacher_lesson_tables().items():
        if name in NOT_PER_CLASS:
            continue
        column = Base.metadata.tables[name].columns["section"]
        assert not column.nullable, f"{name}.section must not be nullable"
        assert column.default is not None or column.server_default is not None, (
            f"{name}.section needs a default so existing rows take the unnamed class"
        )


def test_renaming_a_class_moves_every_kind_of_history():
    """The rename path has to cover all of them, not just progress.

    Listing the models in one place is what makes that true; this checks the
    list has not drifted from what is actually keyed by class.
    """
    keyed = {
        Base.metadata.tables[m.__tablename__].name for m in SECTION_KEYED_MODELS
    }
    expected = {
        name for name in _teacher_lesson_tables() if name not in NOT_PER_CLASS
    }
    assert keyed == expected, (
        "SECTION_KEYED_MODELS is what a class rename moves. It has drifted from "
        f"the tables that are actually per class: {sorted(expected ^ keyed)}"
    )


def test_the_models_a_rename_moves_are_the_ones_we_think():
    """Guards the assertion above from passing for the wrong reason."""
    assert set(SECTION_KEYED_MODELS) == {Progress, ChatMessage, AccessRequest}
    for model in SECTION_KEYED_MODELS:
        mapper = sa_inspect(model)
        assert "section" in mapper.columns
        assert "teacher_id" in mapper.columns
        assert "lesson_id" in mapper.columns
