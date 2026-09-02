from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.fair_section import FairSection


class FairProject(Base, TimestampMixin):
    """An ICT Fair project PDF, filed under a section.

    Still outside the lesson/sequencing/auto-assign pipeline: fair projects have
    no unlock sequence and no completion. What they do have, through their
    section, is a school and a set of grades — schools each run their own fair,
    so a project belongs to exactly one of them.
    """

    __tablename__ = "fair_projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    # The stored PDF backing this project.
    file_id: Mapped[str | None] = mapped_column(
        ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Nullable so a project uploaded before sections existed still loads. An
    # unfiled project has no section and therefore no school, so it is shown to
    # super-admins to be filed and to no teacher at all — scoping fails closed.
    section_id: Mapped[str | None] = mapped_column(
        ForeignKey("fair_sections.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    uploaded_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    section: Mapped["FairSection | None"] = relationship(back_populates="projects")
