from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.fair_project import FairProject


class FairSection(Base, TimestampMixin):
    """A named group of ICT Fair projects, for one school and one or more grades.

    Two things make this a table rather than a label on a project.

    It belongs to a school. Schools do not share fair projects — each runs its
    own — so the school is what decides who may see a project at all, and the
    section is where that fact lives. A project's school is its section's
    school; storing it on both would let them disagree.

    Its grades are a list. The curriculum groups them: KG1 and KG2 run the same
    fair project, and "Grades 7-9" is one project three grades share, not three
    copies of it.
    """

    __tablename__ = "fair_sections"

    id: Mapped[str] = mapped_column(String, primary_key=True)

    # Required. A section with no school is a section nobody can be scoped to,
    # which is how a project ends up visible to the wrong school.
    school_id: Mapped[str] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    blurb: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Grade tokens, e.g. ["KG1", "KG2"] or ["G7", "G8"]. Same vocabulary as
    # `User.grades`, so a teacher's grades can be matched against them directly.
    grades: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    projects: Mapped[list["FairProject"]] = relationship(
        back_populates="section",
        # Deleting a section never silently destroys its projects — the router
        # refuses while it still has any. This keeps the ORM in step with that
        # rather than quietly cascading behind it.
        passive_deletes=True,
    )
