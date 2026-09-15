"""Authentication: register, login, refresh, logout, logout-all, me.

Security properties:
- Argon2id password verification, with transparent rehash on parameter upgrade.
- Account lockout after N failed attempts within a window.
- Uniform error messages so the endpoint does not leak whether an email exists.
- Refresh-token rotation: each refresh revokes the old token and issues a new one.
- Tokens are delivered as httpOnly cookies, with the access token also returned
  for cross-site mobile fallback clients.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import note_unfamiliar_signin, record_event
from app.config import settings
from app.cookies import (
    REFRESH_COOKIE_NAME,
    clear_auth_cookies,
    set_access_cookie,
    set_refresh_cookie,
)
from app.database import get_db
from app.deps import get_current_user
from app.models import LoginThrottle, RefreshToken, User
from app.models.enums import SecurityEvent, SecurityStatus, UserStatus
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    SessionUser,
)
from app.services.signin_watch import check_sign_in
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

import secrets

router = APIRouter(prefix="/api/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
)


def _rejected_and_signed_out() -> JSONResponse:
    """401 that actually clears the cookies.

    Setting them on the injected Response and then raising did nothing: those
    headers are merged onto the real response only when the handler *returns*,
    so a dead refresh token was answered with a 401 carrying no Set-Cookie at
    all. The browser kept presenting it on every 401 for the rest of its
    seven-day life.
    """
    rejected = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Invalid email or password"},
    )
    clear_auth_cookies(rejected)
    return rejected

# A throwaway Argon2 hash verified when the email is unknown, so a failed login
# for a non-existent account costs the same time as one for a real account.
# Without this, the response timing leaks whether an email is registered.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _account_lock_minutes(failed_count: int) -> int:
    lockouts = max(1, failed_count // settings.max_failed_logins)
    minutes = settings.lockout_minutes * (2 ** (lockouts - 1))
    return min(minutes, settings.max_lockout_minutes)


def _get_ip_throttle(db: Session, ip: str) -> LoginThrottle | None:
    if not ip:
        return None
    throttle = db.get(LoginThrottle, ip)
    if throttle is None:
        throttle = LoginThrottle(ip=ip)
        db.add(throttle)
        db.flush()
    return throttle


def _enforce_ip_throttle(db: Session, ip: str, now: datetime) -> None:
    if not ip:
        return
    throttle = db.get(LoginThrottle, ip)
    if throttle is None:
        return
    blocked_at = _aware(throttle.blocked_at)
    if blocked_at is not None:
        # A ban that outlived its window is lifted here rather than needing a
        # hand-edited database. Nothing else clears it, and a network holding a
        # permanent block locks out every teacher behind it.
        if now - blocked_at >= timedelta(hours=settings.login_ip_block_hours):
            throttle.blocked_at = None
            throttle.cycle_count = 0
            throttle.failed_count = 0
            throttle.window_started_at = None
            throttle.locked_until = None
            # NOT NULL with an empty-string default — not None.
            throttle.reason = ""
        else:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="This network is blocked from signing in.",
            )
    locked_until = _aware(throttle.locked_until)
    if locked_until and locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts from this network. Try again later.",
        )
    if locked_until:
        throttle.locked_until = None
        throttle.failed_count = 0
        throttle.window_started_at = None


def _record_ip_failure(db: Session, ip: str, now: datetime) -> None:
    throttle = _get_ip_throttle(db, ip)
    if throttle is None or throttle.blocked_at is not None:
        return

    window_start = _aware(throttle.window_started_at)
    expired = (
        window_start is None
        or window_start <= now - timedelta(minutes=settings.login_ip_window_minutes)
    )
    if expired:
        throttle.window_started_at = now
        throttle.failed_count = 0

    throttle.failed_count += 1
    if throttle.failed_count >= settings.login_ip_max_failures:
        throttle.cycle_count += 1
        throttle.failed_count = 0
        throttle.window_started_at = None
        if throttle.cycle_count >= settings.login_ip_ban_cycles:
            throttle.blocked_at = now
            throttle.locked_until = None
            throttle.reason = "Repeated failed login cycles"
        else:
            throttle.locked_until = now + timedelta(minutes=settings.lockout_minutes)


def _clear_ip_failures(db: Session, ip: str) -> None:
    if not ip:
        return
    throttle = db.get(LoginThrottle, ip)
    if throttle is None or throttle.blocked_at is not None:
        return
    throttle.failed_count = 0
    throttle.cycle_count = 0
    throttle.window_started_at = None
    throttle.locked_until = None
    throttle.reason = ""


def _issue_session(db: Session, response: Response, user: User, request: Request) -> str:
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
    return access


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
    _enforce_ip_throttle(db, ip, now)
    # Whether this account is locked is decided here but answered further down,
    # after the password has been checked. Answering it first made the lockout
    # an oracle: a distinct 429 for a real address and a 401 for an unknown one
    # told an attacker which of the two they had, from any address. It also
    # returned before the failure could be counted against the network, so
    # attempts on an account already locked were free — and each one still
    # committed a security-log row, at request rate, for as long as they cared
    # to keep it locked.
    locked = bool(user and user.locked_until and _aware(user.locked_until) > now)

    # Always run one Argon2 verify (against a dummy hash for unknown emails) so
    # the timing doesn't reveal whether the account exists.
    if user:
        valid = verify_password(payload.password, user.password_hash)
    else:
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        valid = False

    if not valid:
        _record_ip_failure(db, ip, now)
        if user:
            # An attempt against an account that is already locked counts
            # against the network and nothing else. It used to re-arm the
            # lockout every time, which meant the lock never actually expired
            # while anyone kept knocking: four wrong passwords per quarter hour
            # — too slow to trip the network throttle — held a teacher out for
            # as long as the attacker cared to continue, and an admin's password
            # reset bought only until the next attempt. It also wrote a second
            # security-log row each time, flooding the screen an admin would go
            # to in order to understand why.
            if locked:
                record_event(
                    db, event=SecurityEvent.failed_login, status=SecurityStatus.warning,
                    ip=ip, device=device, user=user,
                    detail="Wrong password while already locked out",
                )
                db.commit()
                raise _INVALID_CREDENTIALS

            # A run of failures that has gone quiet for longer than the window
            # is over, and the next one starts a new run. Without this the count
            # only ever climbed, so the escalation below described the account's
            # whole history rather than the burst in front of it.
            window_started = _aware(user.failed_login_window_started_at)
            if window_started is None or window_started <= now - timedelta(
                minutes=settings.failed_login_window_minutes
            ):
                user.failed_login_count = 0
                user.failed_login_window_started_at = now

            user.failed_login_count += 1
            locked_for = 0
            if user.failed_login_count >= settings.max_failed_logins:
                locked_for = _account_lock_minutes(user.failed_login_count)
                user.locked_until = now + timedelta(minutes=locked_for)
            # A wrong password was logged as "new-ip", so the screen reported an
            # address change that had not happened. It says what it is now.
            record_event(
                db, event=SecurityEvent.failed_login, status=SecurityStatus.warning,
                ip=ip, device=device, user=user,
                detail=(
                    f"Wrong password (attempt {user.failed_login_count} of "
                    f"{settings.max_failed_logins})"
                ),
            )
            if locked_for:
                record_event(
                    db, event=SecurityEvent.account_locked, status=SecurityStatus.blocked,
                    ip=ip, device=device, user=user,
                    detail=(
                        f"Locked for {locked_for} min after "
                        f"{user.failed_login_count} failed attempts"
                    ),
                )
        db.commit()
        raise _INVALID_CREDENTIALS

    # Right password, locked account: now it is safe to say so, because only
    # the account holder can have got this far.
    if locked:
        minutes = max(1, round((_aware(user.locked_until) - now).total_seconds() / 60))
        record_event(
            db, event=SecurityEvent.account_locked, status=SecurityStatus.blocked,
            ip=ip, device=device, user=user,
            detail=f"Correct password while locked out; {minutes} min remaining",
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Account temporarily locked. Try again later.",
        )

    if user.status == UserStatus.pending:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account pending approval")
    if user.status != UserStatus.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")

    # Success: reset lockout, upgrade hash if needed, stamp login, issue session.
    user.clear_lockout()
    _clear_ip_failures(db, ip)
    user.last_login_at = now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    # Order matters: both checks read past successful sign-ins, so they must run
    # before this one is written.
    note_unfamiliar_signin(db, user=user, ip=ip, device=device)
    check_sign_in(db, user=user, ip=ip, device=device)
    access = _issue_session(db, response, user, request)
    record_event(
        db, event=SecurityEvent.normal_login, status=SecurityStatus.ok,
        ip=ip, device=device, user=user,
    )
    db.commit()

    return SessionUser(
        user_id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        school_id=user.school_id,
        ict_fair_access=user.ict_fair_access,
        grades=list(user.grades or []),
        sections=dict(user.sections or {}),
        access_token=access,
    )


@router.post("/refresh", response_model=MessageResponse)
def refresh(
    request: Request, response: Response, db: Session = Depends(get_db)
) -> MessageResponse | JSONResponse:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        return _rejected_and_signed_out()

    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
    if record is None or record.revoked or _aware(record.expires_at) <= datetime.now(timezone.utc):
        return _rejected_and_signed_out()

    user = db.get(User, record.user_id)
    if user is None or user.status != UserStatus.active:
        return _rejected_and_signed_out()

    # Rotate: revoke the presented token, issue a new pair.
    record.revoked = True
    access = _issue_session(db, response, user, request)
    db.commit()
    return MessageResponse(message="Token refreshed", access_token=access)


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
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageResponse:
    live = sum(1 for token in user.refresh_tokens if not token.revoked)
    for token in user.refresh_tokens:
        token.revoked = True
    record_event(
        db, event=SecurityEvent.signed_out_all, status=SecurityStatus.ok,
        ip=client_ip(request), device=user_agent(request), user=user,
        detail=f"Ended {live} session(s) on every device",
    )
    db.commit()
    clear_auth_cookies(response)
    return MessageResponse(message="Signed out from all devices")


@router.get("/me", response_model=SessionUser)
def me(user: User = Depends(get_current_user)) -> SessionUser:
    return SessionUser(
        user_id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        school_id=user.school_id,
        ict_fair_access=user.ict_fair_access,
        grades=list(user.grades or []),
        sections=dict(user.sections or {}),
    )
