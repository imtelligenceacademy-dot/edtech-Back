"""A password reset has to end the session it is replacing.

Resetting revoked the account's refresh tokens, and the handler's docstring
promised existing sessions must re-authenticate. Half of that was true. The
access token is a signed JWT in a cookie — no server-side revocation can reach
it — so whoever held one kept every non-auth endpoint for the rest of its
fifteen minutes. An admin resetting a password *because* a session was stolen
was closing the door a quarter of an hour in advance.

Suspending the account always took effect at once. Resetting the password is
the remedy an admin reaches for far more often, and it did not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Request

from app.deps import get_current_user
from app.models import User
from app.models.enums import Role, UserStatus
from app.routers.users import reset_password
from app.schemas.user import PasswordReset
from app.security import create_access_token, decode_access_token
from app.utils import new_id


def _request(token: str) -> Request:
    """A request carrying the token the way an API client does."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/whatever",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("203.0.113.9", 51000),
        }
    )


def _user(db, role: Role = Role.teacher) -> User:
    user = User(
        id=new_id("u"),
        name="Session teacher",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=role,
        status=UserStatus.active,
        grades=["G7"],
    )
    db.add(user)
    db.commit()
    return user


def _token_for(user: User) -> str:
    return create_access_token(user_id=user.id, role=user.role.value)


def test_a_token_issued_before_the_reset_is_refused(db):
    user = _user(db)
    stolen = _token_for(user)
    issued_at = decode_access_token(stolen)["iat"]

    # The reset lands after that token was minted, which is the whole scenario.
    user.sessions_valid_from = datetime.fromtimestamp(issued_at + 1, tz=timezone.utc)
    db.commit()

    with pytest.raises(HTTPException) as err:
        get_current_user(_request(stolen), db=db)
    assert err.value.status_code == 401


def test_a_token_issued_after_the_reset_still_works(db):
    """The teacher signs in with the new password and is not locked out by it."""
    user = _user(db)
    user.sessions_valid_from = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()

    fresh = _token_for(user)

    assert get_current_user(_request(fresh), db=db).id == user.id


def test_an_account_that_was_never_reset_is_untouched(db):
    user = _user(db)
    assert user.sessions_valid_from is None

    assert get_current_user(_request(_token_for(user)), db=db).id == user.id


def test_resetting_a_password_arms_the_cutoff(db):
    """The wiring: the handler has to set it, not merely have somewhere to."""
    admin = _user(db, role=Role.super_admin)
    teacher = _user(db)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)

    reset_password(
        user_id=teacher.id,
        payload=PasswordReset(password="a-long-enough-new-password"),
        request=_request(_token_for(admin)),
        db=db,
        current=admin,
    )

    db.refresh(teacher)
    cutoff = teacher.sessions_valid_from
    assert cutoff is not None
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    assert cutoff >= before


def test_a_token_with_no_issued_at_is_refused_once_a_cutoff_exists(db):
    """Nothing this codebase mints lacks `iat`. If one arrives anyway, it cannot
    be shown to post-date the reset, so it does not get the benefit of doubt."""
    import jwt

    from app.config import settings
    from app.security import ACCESS_TOKEN_TYPE

    user = _user(db)
    user.sessions_valid_from = datetime.now(timezone.utc)
    db.commit()

    forged = jwt.encode(
        {
            "sub": user.id,
            "role": user.role.value,
            "type": ACCESS_TOKEN_TYPE,
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()),
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(HTTPException) as err:
        get_current_user(_request(forged), db=db)
    assert err.value.status_code == 401
