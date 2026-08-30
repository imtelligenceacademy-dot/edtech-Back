from __future__ import annotations

from datetime import datetime

from app.models.enums import Role, SecurityEvent, SecurityStatus
from app.schemas.base import CamelModel


class SecurityLogOut(CamelModel):
    id: str
    user_id: str | None = None
    user_name: str
    role: Role | None = None
    school_id: str | None = None
    ip: str
    device: str          # the raw User-Agent, kept for the details panel
    device_label: str = ""   # "Chrome 141 on Windows 11 · Desktop"
    location_label: str
    location_lat: float | None = None
    location_lng: float | None = None
    detail: str = ""
    event: SecurityEvent
    status: SecurityStatus
    timestamp: datetime


class IpHistory(CamelModel):
    """What else this address has done. "First time ever" is the whole signal."""

    sign_ins: int = 0
    failed_attempts: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    # More than one name here means an address several accounts sign in from —
    # a shared school network, or a shared password.
    users: list[str] = []


class SessionOut(CamelModel):
    id: str
    device_label: str
    ip: str
    created_at: datetime
    expires_at: datetime
    # Same address and browser family as the event being viewed. Deliberately
    # not called "current": a log row records no session id, so which of two
    # sessions from the same laptop produced it is genuinely unknowable.
    matches_event: bool = False


class SecurityLogDetail(CamelModel):
    log: SecurityLogOut
    ip_history: IpHistory
    # This user's other recent events, so the row can be read in context: four
    # failures then a success is a different story from a lone success.
    recent_events: list[SecurityLogOut] = []
    active_sessions: list[SessionOut] = []
