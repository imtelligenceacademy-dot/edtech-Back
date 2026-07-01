"""Helpers for setting and clearing the auth cookies.

Both tokens are delivered as httpOnly cookies so JavaScript (and therefore any
XSS payload) cannot read them — a strict improvement over the localStorage
approach the frontend currently uses. The refresh cookie is path-scoped to the
refresh endpoint so it is not sent on every request.
"""

from __future__ import annotations

from fastapi import Response

from app.config import settings

ACCESS_COOKIE_NAME = "imt_access"
REFRESH_COOKIE_NAME = "imt_refresh"
REFRESH_COOKIE_PATH = "/api/auth"


def set_access_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=REFRESH_COOKIE_PATH,
    )


def clear_auth_cookies(response: Response) -> None:
    # Deletion must repeat the Secure/SameSite attributes, or browsers reject the
    # clearing of a SameSite=None; Secure cookie (so logout wouldn't stick).
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
