from __future__ import annotations

from datetime import datetime

from app.models.enums import Role, SecurityEvent, SecurityStatus
from app.schemas.base import CamelModel


class SecurityLogOut(CamelModel):
    id: str
    user_id: str | None = None
    user_name: str
    role: Role | None = None
    school_id: str | None = None
    ip: str
    device: str
    location_label: str
    event: SecurityEvent
    status: SecurityStatus
    timestamp: datetime
