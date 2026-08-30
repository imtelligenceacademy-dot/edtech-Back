"""Sign-ins that don't add up.

The question this answers is the one the product has always been asking and
never implemented: is an account being used by more than one person?

It does NOT ask it with geolocation. Location looked like the obvious
instrument, but measured on real Lebanese addresses every subscriber resolves
to the capital — the databases map an address to where the ISP routes it, and a
country with one incumbent and a central gateway routes everything through one
city. Distance between two sign-ins would therefore be zero for two people in
different towns, and the whole detector would sit silent.

The network is sharper and needs no lookup. Nobody is on two networks at once,
so one account signing in from two of them minutes apart is either a shared
password or a stolen one. That works today, on data already being recorded.

Country is used only when it happens to be known already — a cross-border hop
is the one case where geolocation is both correct and serious — and never at
the cost of a lookup during someone's sign-in.

Warning-only, deliberately. A carrier can move a subscriber between networks,
teachers use VPNs and hotspots, and locking someone out of their lesson on a
Tuesday because their phone changed towers is worse than the problem.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_event
from app.config import settings
from app.models import SecurityLog, User
from app.models.enums import SecurityEvent, SecurityStatus
from app.services import geoip


def network_of(ip: str) -> str:
    """The address's neighbourhood, not the address.

    A single subscriber's address moves around within their provider's block —
    mobile carriers especially — so comparing exact addresses would cry wolf at
    one teacher on one phone. A /24 (or /64 for IPv6) is the smallest unit that
    survives that and still separates two genuinely different connections.
    """
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return ip or ""
    if parsed.version == 4:
        return str(ipaddress.ip_network(f"{parsed}/24", strict=False))
    return str(ipaddress.ip_network(f"{parsed}/64", strict=False))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _already_warned(db: Session, user: User, since: datetime) -> bool:
    """One warning per window. A shared account signs in all day; the admin
    needs to know it is happening, not to receive it forty times."""
    return (
        db.scalar(
            select(SecurityLog.id)
            .where(
                SecurityLog.user_id == user.id,
                SecurityLog.event == SecurityEvent.suspicious_location,
                SecurityLog.timestamp >= since,
            )
            .limit(1)
        )
        is not None
    )


def check_sign_in(
    db: Session, *, user: User, ip: str, device: str
) -> SecurityLog | None:
    """Compare this sign-in with the account's previous one.

    Called before the current sign-in is recorded, so "previous" means the one
    before this. Returns the warning it wrote, or None when nothing is odd.
    """
    window = timedelta(minutes=settings.signin_window_minutes)
    if not ip or window <= timedelta(0):
        return None

    now = datetime.now(timezone.utc)
    previous = db.scalar(
        select(SecurityLog)
        .where(
            SecurityLog.user_id == user.id,
            SecurityLog.event == SecurityEvent.normal_login,
        )
        .order_by(SecurityLog.timestamp.desc())
        .limit(1)
    )
    if previous is None or not previous.ip:
        return None

    when = _aware(previous.timestamp)
    if when is None or now - when > window:
        return None
    if network_of(previous.ip) == network_of(ip):
        return None
    if _already_warned(db, user, now - window):
        return None

    minutes = max(1, round((now - when).total_seconds() / 60))
    detail = (
        f"Signed in from {previous.ip} and {ip} within {minutes} min — "
        f"two networks, one account"
    )

    # A cross-border hop is worth naming, but only if both places are already
    # known. Nothing here waits on a lookup.
    here = geoip.locate_cached(ip)
    there = geoip.country_of(previous.location_label)
    if here and there:
        country = geoip.country_of(here.label)
        if country and country != there:
            detail = (
                f"Signed in from {there} and {country} within {minutes} min — "
                f"{previous.ip} then {ip}"
            )

    return record_event(
        db,
        event=SecurityEvent.suspicious_location,
        status=SecurityStatus.warning,
        ip=ip,
        device=device,
        user=user,
        detail=detail,
    )
