from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import SecurityLog, User
from app.models.enums import Role
from app.schemas.security import SecurityLogOut

router = APIRouter(prefix="/api/security-logs", tags=["security"])


@router.get("", response_model=list[SecurityLogOut])
def list_security_logs(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[SecurityLog]:
    stmt = select(SecurityLog)
    if current.role == Role.school_admin:
        stmt = stmt.where(SecurityLog.school_id == current.school_id)
    elif current.role == Role.teacher:
        # Teachers see only their own security events.
        stmt = stmt.where(SecurityLog.user_id == current.id)
    return list(db.scalars(stmt.order_by(SecurityLog.timestamp.desc()).limit(limit)))
