from __future__ import annotations

from datetime import datetime

from app.schemas.base import CamelModel


class FairProjectOut(CamelModel):
    id: str
    title: str
    file_id: str | None = None
    created_at: datetime | None = None
