from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.enums import ReportScope, ReportStatus
from app.schemas.base import CamelModel


class ReportOut(CamelModel):
    id: str
    title: str
    scope: ReportScope
    school_id: str | None = None
    requested_by: str | None = None
    requested_at: datetime = Field(validation_alias="created_at")
    status: ReportStatus
    ready_at: datetime | None = None


class ReportCreate(CamelModel):
    title: str = Field(min_length=2, max_length=160)
    scope: ReportScope
    school_id: str | None = None
