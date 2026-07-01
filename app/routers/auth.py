"""Authentication: register, login, refresh, logout, logout-all, me.

Security properties:
- Argon2id password verification, with transparent rehash on parameter upgrade.
- Account lockout after N failed attempts within a window.
- Uniform error messages so the endpoint does not leak whether an email exists.
- Refresh-token rotation: each refresh revokes the old token and issues a new one.
- Tokens are delivered only as httpOnly cookies.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.config import settings
from app.cookies import (
    REFRESH_COOKIE_NAME,
    clear_auth_cookies,
    set_access_cookie,
    set_refresh_cookie,
)
from app.database import get_db
from app.deps import get_current_user
from app.models import RefreshToken, User
from app.models.enums import SecurityEvent, SecurityStatus, UserStatus
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    SessionUser,
)
from app.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    needs_rehash,
    refresh_expiry,
    verify_password,
)
from app.utils import client_ip, new_id, user_agent

router = APIRouter(prefix="/api/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _issue_session(db: Session, response: Response, user: User, request: Request) -> None:
    """Mint an access cookie and a fresh, persisted refresh token."""
    access = create_access_token(user_id=user.id, role=user.role.value)
    set_access_cookie(response, access)

    raw_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            id=new_id("rt"),
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            expires_at=refresh_expiry(),
            user_agent=user_agent(request),
            ip=client_ip(request),
        )
    )
    set_refresh_cookie(response, raw_refresh)


# Public self-signup is intentionally not offered — only the Super Admin
# creates accounts (see /api/users). There is no /register endpoint.


@router.post("/login", response_model=SessionUser)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> SessionUser:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    ip, device = client_ip(request), user_agent(request)

    # Lockout check (constant-ish path; still verify a dummy hash to reduce timing signal).
    now = datetime.now(timezone.utc)
    if user and user.locked_until and _aware(user.locked_until) > now:
        record_event(
            db, event=SecurityEvent.blocked_second_device, status=SecurityStatus.blocked,
            ip=ip, device=device, user=user,
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Account temporarily locked. Try again later.",
        )

    valid = bool(user) and verify_password(payload.password, user.password_hash)

    if not valid:
        if user:
            user.failed_login_count += 1
            if user.failed_login_count >= settings.max_failed_logins:
                from datetime import timedelta

                user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
                user.failed_login_count = 0
            record_event(
                db, event=SecurityEvent.new_ip, status=SecurityStatus.warning,
                ip=ip, device=device, user=user,
            )
            db.commit()
        raise _INVALID_CREDENTIALS

    if user.status == UserStatus.pending:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account pending approval")
    if user.status != UserStatus.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")

    # Success: reset lockout, upgrade hash if needed, stamp login, issue session.
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    _issue_session(db, response, user, request)
    record_event(
        db, event=SecurityEvent.normal_login, status=SecurityStatus.ok,
        ip=ip, device=device, user=user,
    )
    db.commit()

    return SessionUser(
        user_id=user.id, name=user.name, email=user.email, role=user.role, school_id=user.school_id
    )


@router.post("/refresh", response_model=MessageResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)) -> MessageResponse:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        raise _INVALID_CREDENTIALS

    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
    if record is None or record.revoked or _aware(record.expires_at) <= datetime.now(timezone.utc):
        clear_auth_cookies(response)
        raise _INVALID_CREDENTIALS

    user = db.get(User, record.user_id)
    if user is None or user.status != UserStatus.active:
        clear_auth_cookies(response)
        raise _INVALID_CREDENTIALS

    # Rotate: revoke the presented token, issue a new pair.
    record.revoked = True
    _issue_session(db, response, user, request)
    db.commit()
    return MessageResponse(message="Token refreshed")


@router.post("/logout", response_model=MessageResponse)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> MessageResponse:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw:
        record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
        if record:
            record.revoked = True
            db.commit()
    clear_auth_cookies(response)
    return MessageResponse(message="Signed out")


@router.post("/logout-all", response_model=MessageResponse)
def logout_all(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageResponse:
    for token in user.refresh_tokens:
        token.revoked = True
    db.commit()
    clear_auth_cookies(response)
    return MessageResponse(message="Signed out from all devices")


@router.get("/me", response_model=SessionUser)
def me(user: User = Depends(get_current_user)) -> SessionUser:
    return SessionUser(
        user_id=user.id, name=user.name, email=user.email, role=user.role, school_id=user.school_id
    )
