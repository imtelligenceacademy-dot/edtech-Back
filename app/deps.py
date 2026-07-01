"""Reusable FastAPI dependencies for authentication and authorization.

`get_current_user` reads the access-token cookie (falling back to a Bearer
header for API clients), verifies the JWT, loads the user, and rejects
inactive accounts. The role/capability guards build on top of it.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.cookies import ACCESS_COOKIE_NAME
from app.database import get_db
from app.models import User
from app.models.enums import Role, UserStatus
from app.permissions import Capability, can
from app.security import decode_access_token

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_token(request: Request) -> str | None:
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _extract_token(request)
    if not token:
        raise _CREDENTIALS_EXC

    payload = decode_access_token(token)
    if not payload:
        raise _CREDENTIALS_EXC

    user_id = payload.get("sub")
    if not user_id:
        raise _CREDENTIALS_EXC

    user = db.get(User, user_id)
    if user is None:
        raise _CREDENTIALS_EXC
    if user.status != UserStatus.active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active"
        )
    return user


def require_roles(*roles: Role) -> Callable[[User], User]:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role for this action",
            )
        return user

    return dependency


def require_capability(capability: Capability) -> Callable[[User], User]:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if not can(user.role, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing capability: {capability}",
            )
        return user

    return dependency


def assert_school_scope(user: User, school_id: str | None) -> None:
    """School admins and teachers may only touch their own school's data."""
    if user.role == Role.super_admin:
        return
    if school_id is not None and user.school_id != school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Outside your school scope",
        )
