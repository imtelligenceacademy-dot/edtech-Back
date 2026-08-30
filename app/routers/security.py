from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import RefreshToken, SecurityLog, User
from app.models.enums import Role, SecurityEvent, SecurityStatus
from app.schemas.security import (
    IpHistory,
    SecurityLogDetail,
    SecurityLogOut,
    SessionOut,
)
from app.services import geoip, useragent

router = APIRouter(prefix="/api/security-logs", tags=["security"])

# How many distinct addresses one page view will resolve. A first look at a long
# history shouldn't hang on a few hundred lookups; the rest fill in next time.
MAX_LOOKUPS_PER_REQUEST = 25


def _scope(stmt, current: User):
    if current.role == Role.school_admin:
        return stmt.where(SecurityLog.school_id == current.school_id)
    if current.role == Role.teacher:
        # Teachers see only their own security events.
        return stmt.where(SecurityLog.user_id == current.id)
    return stmt


def _may_read(current: User, log: SecurityLog) -> bool:
    if current.role == Role.super_admin:
        return True
    if current.role == Role.school_admin:
        return log.school_id is not None and log.school_id == current.school_id
    return log.user_id == current.id


def _resolve_locations(db: Session, logs: list[SecurityLog]) -> None:
    """Fill in missing locations and keep the answer on the row.

    Done here rather than at sign-in so a slow or dead lookup can never delay
    somebody's login. With GEOIP_PROVIDER unset this is a no-op and the screen
    shows nothing — which is the honest output, and better than the (0.00, 0.00)
    it used to draw for every row on earth.
    """
    if not geoip.enabled():
        return
    pending = {log.ip for log in logs if log.ip and not log.location_label}
    if not pending:
        return

    found: dict[str, geoip.Place] = {}
    for ip in list(pending)[:MAX_LOOKUPS_PER_REQUEST]:
        place = geoip.locate(ip)
        if place is not None:
            found[ip] = place
    if not found:
        return

    for log in logs:
        place = found.get(log.ip)
        if place is not None and not log.location_label:
            log.location_label = place.label
            log.location_lat = place.lat
            log.location_lng = place.lng
    db.commit()


def _out(log: SecurityLog) -> SecurityLogOut:
    return SecurityLogOut(
        id=log.id,
        user_id=log.user_id,
        user_name=log.user_name,
        role=log.role,
        school_id=log.school_id,
        ip=log.ip,
        device=log.device,
        device_label=useragent.label(log.device),
        # Stored full, shown at the configured precision.
        location_label=geoip.display(log.location_label),
        location_lat=log.location_lat,
        location_lng=log.location_lng,
        detail=log.detail or "",
        event=log.event,
        status=log.status,
        timestamp=log.timestamp,
    )


@router.get("", response_model=list[SecurityLogOut])
def list_security_logs(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[SecurityLogOut]:
    stmt = _scope(select(SecurityLog), current)
    logs = list(db.scalars(stmt.order_by(SecurityLog.timestamp.desc()).limit(limit)))
    _resolve_locations(db, logs)
    return [_out(log) for log in logs]


@router.get("/{log_id}", response_model=SecurityLogDetail)
def security_log_detail(
    log_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> SecurityLogDetail:
    """One event, with the context that makes it readable.

    A single row can't tell you whether an address is familiar or brand new,
    whether the sign-in followed four failures, or how many sessions the account
    has open. That is all one query away, and it is what an admin actually opens
    a log line to find out.
    """
    log = db.get(SecurityLog, log_id)
    if log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    if not _may_read(current, log):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")

    _resolve_locations(db, [log])

    history = IpHistory()
    if log.ip:
        rows = list(
            db.execute(
                select(
                    SecurityLog.event,
                    func.count(SecurityLog.id),
                    func.min(SecurityLog.timestamp),
                    func.max(SecurityLog.timestamp),
                )
                .where(SecurityLog.ip == log.ip)
                .group_by(SecurityLog.event)
            ).all()
        )
        firsts = [r[2] for r in rows if r[2] is not None]
        lasts = [r[3] for r in rows if r[3] is not None]
        history = IpHistory(
            sign_ins=sum(c for e, c, _, _ in rows if e == SecurityEvent.normal_login),
            failed_attempts=sum(
                c for e, c, _, _ in rows if e == SecurityEvent.failed_login
            ),
            first_seen=min(firsts) if firsts else None,
            last_seen=max(lasts) if lasts else None,
            users=sorted(
                {
                    name
                    for name in db.scalars(
                        select(SecurityLog.user_name).where(SecurityLog.ip == log.ip)
                    )
                    if name
                }
            ),
        )

    recent: list[SecurityLog] = []
    sessions: list[SessionOut] = []
    if log.user_id:
        recent = list(
            db.scalars(
                select(SecurityLog)
                .where(SecurityLog.user_id == log.user_id, SecurityLog.id != log.id)
                .order_by(SecurityLog.timestamp.desc())
                .limit(10)
            )
        )
        now = datetime.now(timezone.utc)
        for token in db.scalars(
            select(RefreshToken)
            .where(RefreshToken.user_id == log.user_id, RefreshToken.revoked.is_(False))
            .order_by(RefreshToken.created_at.desc())
        ):
            expires = token.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                continue
            sessions.append(
                SessionOut(
                    id=token.id,
                    device_label=useragent.label(token.user_agent),
                    ip=token.ip or "",
                    created_at=token.created_at,
                    expires_at=token.expires_at,
                    matches_event=token.ip == log.ip
                    and useragent.family(token.user_agent) == useragent.family(log.device),
                )
            )

    return SecurityLogDetail(
        log=_out(log),
        ip_history=history,
        recent_events=[_out(r) for r in recent],
        active_sessions=sessions,
    )
