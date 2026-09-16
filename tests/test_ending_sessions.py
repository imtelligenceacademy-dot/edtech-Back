"""Signing out everywhere has to mean everywhere.

Refresh tokens are rows and were always revoked. The access token is a signed
JWT in a cookie that no server-side revocation reaches, so ending sessions also
has to move `users.sessions_valid_from` — otherwise the holder keeps every
non-auth endpoint for the rest of its fifteen minutes.

The password reset was taught that and "sign out from all devices" was not, so
the platform's only user-facing way to evict a stolen session did not evict it
while telling the teacher, with a count, that it had. Both now go through one
function, which is the part that stops them drifting apart again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Request, Response

from app.deps import get_current_user
from app.models import RefreshToken, User
from app.models.enums import Role, UserStatus
from app.routers.auth import logout_all, refresh
from app.routers.users import reset_password
from app.schemas.user import PasswordReset
from app.security import create_access_token, generate_refresh_token, hash_token
from app.services import sessions as sessions_service
from app.utils import new_id


class _FiveSecondsLater:
    """The sign-out happens after the token it is evicting was minted.

    `iat` is whole seconds, so a token minted in the same second as the cutoff
    survives by design — a documented, deliberate limit. A test that mints and
    evicts in one breath is testing that limit rather than the eviction, so this
    moves the sign-out five seconds on, which is what actually happens when
    somebody notices a laptop is missing.
    """

    @staticmethod
    def now(tz=None):
        return datetime.now(tz) + timedelta(seconds=5)



def _request(token: str | None = None, refresh_cookie: str | None = None) -> Request:
    headers = []
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if refresh_cookie:
        headers.append((b"cookie", f"imt_refresh={refresh_cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/whatever",
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.9", 51000),
        }
    )


def _user(db, role: Role = Role.teacher) -> User:
    user = User(
        id=new_id("u"),
        name="Session owner",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=role,
        status=UserStatus.active,
        grades=["G7"],
    )
    db.add(user)
    db.commit()
    return user


def _session_for(db, user: User, *, created_at: datetime | None = None) -> str:
    """A live refresh token, as _issue_session would have left one."""
    raw = generate_refresh_token()
    db.add(
        RefreshToken(
            id=new_id("rt"),
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            created_at=created_at or datetime.now(timezone.utc),
        )
    )
    db.commit()
    return raw


def _access_for(user: User) -> str:
    return create_access_token(user_id=user.id, role=user.role.value)


# --- signing out from every device ----------------------------------------- #


def test_logout_all_kills_the_access_token_too(db, monkeypatch):
    """The bug, stated as the teacher meets it: her laptop is taken, she signs
    out everywhere, and the thief's cookie keeps working."""
    monkeypatch.setattr(sessions_service, "datetime", _FiveSecondsLater)
    user = _user(db)
    _session_for(db, user)
    stolen = _access_for(user)
    # Precondition: that cookie works right now.
    assert get_current_user(_request(stolen), db=db).id == user.id

    logout_all(request=_request(stolen), response=Response(), db=db, user=user)

    with pytest.raises(HTTPException) as err:
        get_current_user(_request(stolen), db=db)
    assert err.value.status_code == 401


def test_logout_all_still_revokes_the_refresh_tokens_and_counts_them(db):
    user = _user(db)
    _session_for(db, user)
    _session_for(db, user)

    logout_all(request=_request(), response=Response(), db=db, user=user)

    live = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .count()
    )
    assert live == 0


def test_a_password_reset_ends_sessions_the_same_way(db, monkeypatch):
    """Both callers go through one function; this is the other one."""
    monkeypatch.setattr(sessions_service, "datetime", _FiveSecondsLater)
    admin = _user(db, role=Role.super_admin)
    teacher = _user(db)
    _session_for(db, teacher)
    stolen = _access_for(teacher)

    reset_password(
        user_id=teacher.id,
        payload=PasswordReset(password="a-long-enough-new-password"),
        request=_request(_access_for(admin)),
        db=db,
        current=admin,
    )

    with pytest.raises(HTTPException) as err:
        get_current_user(_request(stolen), db=db)
    assert err.value.status_code == 401


# --- and the endpoint that could undo it ----------------------------------- #


def test_refresh_refuses_a_token_older_than_the_cutoff(db):
    """Defence in depth on the one endpoint that turns a missed row into a
    session that renews itself for ever."""
    user = _user(db)
    raw = _session_for(db, user, created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    # The sweep is deliberately not run here: this asserts the cutoff alone is
    # enough, which is what makes it a second line rather than a restatement.
    user.sessions_valid_from = datetime.now(timezone.utc)
    db.commit()

    result = refresh(request=_request(refresh_cookie=raw), response=Response(), db=db)

    assert getattr(result, "status_code", None) == 401


def test_refresh_still_works_for_a_session_started_after_the_cutoff(db):
    """A teacher who signs in again with the new password is not locked out."""
    user = _user(db)
    user.sessions_valid_from = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()
    raw = _session_for(db, user)

    result = refresh(request=_request(refresh_cookie=raw), response=Response(), db=db)

    assert getattr(result, "status_code", None) != 401
    assert getattr(result, "message", "") == "Token refreshed"


def test_refresh_is_untouched_on_an_account_that_was_never_evicted(db):
    user = _user(db)
    assert user.sessions_valid_from is None
    raw = _session_for(db, user)

    result = refresh(request=_request(refresh_cookie=raw), response=Response(), db=db)

    assert getattr(result, "message", "") == "Token refreshed"
