"""Security-log helper. Every auth-relevant event is recorded for the
Security Logs screens (super-admin global, school-admin school-scoped).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SecurityLog, User
from app.models.enums import SecurityEvent, SecurityStatus
from app.services import useragent
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
    detail: str = "",
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
        detail=detail[:300],
    )
    db.add(log)
    return log


def _seen_before(db: Session, user: User, column, value: str) -> bool:
    """Has this user ever signed in successfully with this IP / device before?

    Only successful sign-ins count. A stranger failing a password from an
    address must not teach the log that the address is familiar.
    """
    if not value:
        return True  # nothing to compare; don't cry wolf
    return (
        db.scalar(
            select(SecurityLog.id)
            .where(
                SecurityLog.user_id == user.id,
                SecurityLog.event == SecurityEvent.normal_login,
                column == value,
            )
            .limit(1)
        )
        is not None
    )


def note_unfamiliar_signin(
    db: Session, *, user: User, ip: str, device: str
) -> list[SecurityLog]:
    """Flag a successful sign-in from an address or a browser never used before.

    This is what "new-ip" and "foreign-device" were always supposed to mean.
    Both fire only the first time, so they stay rare enough to be worth reading.
    The device is compared by family ("Chrome on Windows") rather than the raw
    header, which changes with every browser update and would otherwise report a
    new device every Tuesday.
    """
    events: list[SecurityLog] = []

    if not _seen_before(db, user, SecurityLog.ip, ip):
        events.append(
            record_event(
                db,
                event=SecurityEvent.new_ip,
                status=SecurityStatus.warning,
                ip=ip,
                device=device,
                user=user,
                detail="First successful sign-in from this address",
            )
        )

    family = useragent.family(device)
    if family != "Unknown on Unknown":
        known = db.scalars(
            select(SecurityLog.device).where(
                SecurityLog.user_id == user.id,
                SecurityLog.event == SecurityEvent.normal_login,
            )
        )
        if family not in {useragent.family(d) for d in known}:
            events.append(
                record_event(
                    db,
                    event=SecurityEvent.foreign_device,
                    status=SecurityStatus.warning,
                    ip=ip,
                    device=device,
                    user=user,
                    detail=f"First successful sign-in from {family}",
                )
            )

    return events
