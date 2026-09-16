"""Ending an account's sessions — all of them, everywhere."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import RefreshToken, User


def end_all_sessions(db: Session, user: User) -> int:
    """Revoke every refresh token and invalidate every access token already
    issued. Returns the number of live sessions this ended.

    Both halves are required and only one of them is obvious. Refresh tokens are
    rows, so they can be revoked. The access token is a signed JWT held in a
    cookie that no server-side revocation reaches, so without moving
    `sessions_valid_from` the holder keeps every non-auth endpoint for the rest
    of its lifetime — which is precisely the window the caller is trying to
    close.

    This is one function because it was previously written out twice and the two
    copies disagreed. The password reset was taught to move the cutoff; "sign
    out from all devices", the control whose entire purpose is this, was not —
    so the platform's only user-facing way to evict a stolen session did not
    evict it.
    """
    # Serialise against a concurrent /api/auth/refresh for the same account.
    # Refresh rotates by revoking one row and inserting another; without this
    # lock that insert can land after the sweep below has chosen its rows,
    # leaving a live token behind on the account that was just locked out — and
    # a token minted at that moment carries an `iat` newer than the cutoff, so
    # the stolen session would renew itself indefinitely.
    #
    # SQLite ignores FOR UPDATE and can: a write transaction takes a
    # database-wide lock, so the serialisation is already there.
    db.flush()
    db.get(User, user.id, with_for_update=True, populate_existing=True)

    live = (
        db.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        )
        or 0
    )
    # A statement rather than the loaded `user.refresh_tokens` collection, so it
    # acts on every row that exists now rather than the ones this request
    # happened to load earlier.
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )
    user.sessions_valid_from = datetime.now(timezone.utc)
    return live
