"""The security log now says what happened.

Two events used to be written under names for things that had not occurred: a
wrong password as "new-ip", and a lockout as "blocked-second-device". These pin
the corrected taxonomy, and the two names that were freed up to finally mean
what they say.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.audit import note_unfamiliar_signin, record_event
from app.models import RefreshToken, School, SecurityLog, User
from app.models.enums import Role, SecurityEvent, SecurityStatus, UserStatus
from app.routers.security import list_security_logs, security_log_detail
from app.services import geoip, useragent
from app.utils import new_id

CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/141.0.0.0 Safari/537.36"
)
CHROME_WIN_NEWER = CHROME_WIN.replace("141.0.0.0", "142.0.0.0")
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _school(db) -> School:
    school = School(
        id=new_id("sch"), name="Sec School", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.commit()
    return school


def _user(db, school: School, role=Role.teacher, name="teacher") -> User:
    user = User(
        id=new_id("u"),
        name=name,
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=role,
        status=UserStatus.active,
        school_id=None if role == Role.super_admin else school.id,
        grades=[],
    )
    db.add(user)
    db.commit()
    return user


def _login(db, user: User, ip: str, device: str = CHROME_WIN) -> SecurityLog:
    log = record_event(
        db,
        event=SecurityEvent.normal_login,
        status=SecurityStatus.ok,
        ip=ip,
        device=device,
        user=user,
    )
    db.commit()
    return log


# --- The names mean what they say ------------------------------------------ #


def test_a_first_sign_in_from_an_address_is_flagged_once(db):
    school = _school(db)
    user = _user(db, school)

    first = note_unfamiliar_signin(db, user=user, ip="8.8.8.8", device=CHROME_WIN)
    _login(db, user, "8.8.8.8")
    again = note_unfamiliar_signin(db, user=user, ip="8.8.8.8", device=CHROME_WIN)
    db.commit()

    assert {e.event for e in first} == {
        SecurityEvent.new_ip,
        SecurityEvent.foreign_device,
    }
    # Second time from the same address and browser: nothing to say.
    assert again == []


def test_a_browser_update_is_not_a_new_device(db):
    """Comparing raw User-Agent strings would report a foreign device every time
    Chrome updated itself."""
    school = _school(db)
    user = _user(db, school)
    _login(db, user, "8.8.8.8", CHROME_WIN)

    events = note_unfamiliar_signin(db, user=user, ip="8.8.8.8", device=CHROME_WIN_NEWER)
    db.commit()

    assert events == []


def test_a_genuinely_different_device_is_flagged(db):
    school = _school(db)
    user = _user(db, school)
    _login(db, user, "8.8.8.8", CHROME_WIN)

    events = note_unfamiliar_signin(db, user=user, ip="8.8.8.8", device=SAFARI_IPHONE)
    db.commit()

    assert [e.event for e in events] == [SecurityEvent.foreign_device]
    assert "Safari on iOS" in events[0].detail


def test_a_failed_attempt_never_makes_an_address_look_familiar(db):
    """Only successful sign-ins teach the log that an address is known — or a
    stranger guessing passwords would whitelist themselves."""
    school = _school(db)
    user = _user(db, school)
    record_event(
        db,
        event=SecurityEvent.failed_login,
        status=SecurityStatus.warning,
        ip="203.0.113.9",
        device=CHROME_WIN,
        user=user,
    )
    db.commit()

    events = note_unfamiliar_signin(db, user=user, ip="203.0.113.9", device=CHROME_WIN)
    db.commit()

    assert SecurityEvent.new_ip in {e.event for e in events}


# --- Reading a row --------------------------------------------------------- #


def test_detail_puts_the_event_in_context(db):
    school = _school(db)
    user = _user(db, school, name="Rita")
    boss = _user(db, school, role=Role.super_admin, name="Owner")
    lone_ip = "203.0.113.77"  # unique to this test: IP history spans users
    for _ in range(2):
        record_event(
            db,
            event=SecurityEvent.failed_login,
            status=SecurityStatus.warning,
            ip=lone_ip,
            device=CHROME_WIN,
            user=user,
            detail="Wrong password",
        )
    log = _login(db, user, lone_ip)
    db.add(
        RefreshToken(
            id=new_id("rt"),
            user_id=user.id,
            token_hash=new_id("h"),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            user_agent=CHROME_WIN,
            ip=lone_ip,
        )
    )
    db.commit()

    detail = security_log_detail(log_id=log.id, db=db, current=boss)

    assert detail.log.device_label.startswith("Chrome 141 on Windows")
    assert detail.ip_history.sign_ins == 1
    assert detail.ip_history.failed_attempts == 2
    assert detail.ip_history.users == ["Rita"]
    # The two failures that preceded it are the context.
    assert len(detail.recent_events) == 2
    assert len(detail.active_sessions) == 1
    assert detail.active_sessions[0].matches_event is True


def test_an_address_used_by_several_accounts_is_visible(db):
    school = _school(db)
    boss = _user(db, school, role=Role.super_admin, name="Owner")
    one = _user(db, school, name="Teacher One")
    two = _user(db, school, name="Teacher Two")
    log = _login(db, one, "198.51.100.4")
    _login(db, two, "198.51.100.4")

    detail = security_log_detail(log_id=log.id, db=db, current=boss)

    assert detail.ip_history.users == ["Teacher One", "Teacher Two"]


def test_a_teacher_cannot_open_someone_elses_event(db):
    school = _school(db)
    mine = _user(db, school, name="Mine")
    theirs = _user(db, school, name="Theirs")
    log = _login(db, theirs, "8.8.8.8")

    with pytest.raises(HTTPException) as err:
        security_log_detail(log_id=log.id, db=db, current=mine)
    assert err.value.status_code == 403


def test_a_school_admin_is_confined_to_their_own_school(db):
    here = _school(db)
    elsewhere = _school(db)
    admin = _user(db, here, role=Role.school_admin, name="Admin")
    outsider = _user(db, elsewhere, name="Outsider")
    log = _login(db, outsider, "8.8.8.8")

    with pytest.raises(HTTPException) as err:
        security_log_detail(log_id=log.id, db=db, current=admin)
    assert err.value.status_code == 403


# --- Location -------------------------------------------------------------- #


def test_no_location_is_invented_when_lookups_are_off(db):
    """The screen used to draw (0.00, 0.00) for every row on earth."""
    school = _school(db)
    user = _user(db, school)
    boss = _user(db, school, role=Role.super_admin, name="Owner")
    _login(db, user, "8.8.8.8")

    rows = list_security_logs(db=db, current=boss, limit=10)
    row = next(r for r in rows if r.ip == "8.8.8.8")

    assert row.location_label == ""
    assert row.location_lat is None and row.location_lng is None


def test_private_addresses_are_named_without_a_lookup(db):
    assert geoip.locate("127.0.0.1") == geoip.LOCAL
    assert geoip.locate("192.168.1.20") == geoip.LOCAL
    # Public addresses stay unknown while the provider is off.
    assert geoip.locate("8.8.8.8") is None


# --- Device parsing -------------------------------------------------------- #


@pytest.mark.parametrize(
    "agent,expected",
    [
        (CHROME_WIN, "Chrome 141 on Windows 11/10 · Desktop"),
        (SAFARI_IPHONE, "Safari 17 on iOS 17 · Mobile"),
        ("", "Unknown device"),
        ("curl/8.4.0", "Unknown device"),
    ],
)
def test_devices_read_as_english(agent, expected):
    assert useragent.label(agent) == expected


def test_edge_is_not_reported_as_chrome():
    """Edge and Opera both claim to be Chrome in their User-Agent."""
    edge = CHROME_WIN + " Edg/141.0.0.0"
    assert useragent.parse(edge).browser == "Edge"
