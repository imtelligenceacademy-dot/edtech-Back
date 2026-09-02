from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.base import CamelModel

# Grade tokens, matching `lib/grades.ts` and `schemas/user.py`.
GRADE_CODES = ["KG1", "KG2", *[f"G{i}" for i in range(1, 13)]]


def _clean_grades(value: list[str]) -> list[str]:
    """Drop unknown tokens and duplicates, and return curriculum order.

    Order is normalised here rather than trusted from the client so a section's
    grades read the same everywhere they are shown, whoever created it.
    """
    seen = {g.strip().upper() for g in value if g and g.strip()}
    return [code for code in GRADE_CODES if code in seen]


class FairProjectOut(CamelModel):
    id: str
    title: str
    file_id: str | None = None
    section_id: str | None = None
    created_at: datetime | None = None


class FairSectionOut(CamelModel):
    """A section with its projects nested, which is how both screens read it."""

    id: str
    school_id: str
    school_name: str | None = None
    title: str
    blurb: str | None = None
    grades: list[str] = []
    projects: list[FairProjectOut] = []
    created_at: datetime | None = None


class FairSectionCreate(CamelModel):
    school_id: str
    title: str = Field(min_length=1, max_length=120)
    blurb: str | None = Field(default=None, max_length=300)
    grades: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("grades")
    @classmethod
    def _normalise(cls, value: list[str]) -> list[str]:
        return _clean_grades(value)


class FairSectionUpdate(CamelModel):
    """Every field optional — an omitted one is left alone rather than cleared."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    blurb: str | None = Field(default=None, max_length=300)
    grades: list[str] | None = Field(default=None, max_length=20)

    @field_validator("grades")
    @classmethod
    def _normalise(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_grades(value)


class FairProjectUpdate(CamelModel):
    """Rename a project, or move it into (or out of) a section."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    section_id: str | None = None
