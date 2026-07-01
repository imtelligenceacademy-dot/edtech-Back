"""Security-log helper. Every auth-relevant event is recorded for the
Security Logs screens (super-admin global, school-admin school-scoped).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import SecurityLog, User
from app.models.enums import SecurityEvent, SecurityStatus
from app.utils import new_id


def record_event(
    db: Session,
    *,
    event: SecurityEvent,
    status: SecurityStatus,
    ip: str = "",
    device: str = "",
    user: User | None = None,
    user_name: str = "",
    location_label: str = "",
) -> SecurityLog:
    log = SecurityLog(
        id=new_id("sec"),
        user_id=user.id if user else None,
        user_name=user.name if user else user_name,
        role=user.role if user else None,
        school_id=user.school_id if user else None,
        ip=ip,
        device=device,
        location_label=location_label,
        event=event,
        status=status,
    )
    db.add(log)
    return log
