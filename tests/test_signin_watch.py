"""One account, two networks, minutes apart.

Most of these are about what must NOT fire. A detector that cries wolf on a
teacher whose phone changed towers gets ignored within a week, and an ignored
warning is worse than none.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.audit import record_event
from app.config import settings
from app.models import School, SecurityLog, User
from app.models.enums import Role, SecurityEvent, SecurityStatus, UserStatus
from app.services import geoip
from app.services.signin_watch import check_sign_in, network_of
from app.utils import new_id

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/141.0.0.0 Safari/537.36"
)
# Two of the real school networks, and one that is genuinely elsewhere.
SCHOOL_A = "185.81.141.197"
SCHOOL_A_NEIGHBOUR = "185.81.141.207"  # same /24 — one site, rotating address
SCHOOL_B = "185.97.95.237"


def _school(db) -> School:
    school = School(
        id=new_id("sch"), name="Watch School", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.commit()
    return school


def _teacher(db, school: School, name="teacher") -> User:
    user = User(
        id=new_id("u"),
        name=name,
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=[],
    )
    db.add(user)
    db.commit()
    return user


def _past_login(db, user: User, ip: str, minutes_ago: int, location: str = "") -> SecurityLog:
    log = record_event(
        db,
        event=SecurityEvent.normal_login,
        status=SecurityStatus.ok,
        ip=ip,
        device=UA,
        user=user,
        location_label=location,
    )
    log.timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.commit()
    return log


# --- What it catches ------------------------------------------------------- #


def test_two_networks_minutes_apart_is_flagged(db):
    user = _teacher(db, _school(db))
    _past_login(db, user, SCHOOL_A, minutes_ago=4)

    event = check_sign_in(db, user=user, ip=SCHOOL_B, device=UA)
    db.commit()

    assert event is not None
    assert event.event == SecurityEvent.suspicious_location
    assert event.status == SecurityStatus.warning  # never a block
    assert SCHOOL_A in event.detail and SCHOOL_B in event.detail
    assert "one account" in event.detail


def test_the_countries_are_named_when_both_are_already_known(db):
    """Geolocation contributes only when the answer is already in hand — never
    at the cost of a lookup while somebody is signing in."""
    user = _teacher(db, _school(db))
    _past_login(db, user, SCHOOL_A, minutes_ago=3, location="Beirut, Lebanon")
    geoip._remember(SCHOOL_B, geoip.Place("Frankfurt, Germany"))
    try:
        event = check_sign_in(db, user=user, ip=SCHOOL_B, device=UA)
        db.commit()
    finally:
        geoip._CACHE.clear()

    assert event is not None
    assert "Lebanon" in event.detail and "Germany" in event.detail


# --- What it must not do --------------------------------------------------- #


def test_the_same_network_is_not_two_places(db):
    """A rotating address inside one school's block is one connection. Comparing
    exact addresses would flag a single teacher on a single phone."""
    user = _teacher(db, _school(db))
    _past_login(db, user, SCHOOL_A, minutes_ago=2)

    assert check_sign_in(db, user=user, ip=SCHOOL_A_NEIGHBOUR, device=UA) is None


def test_a_normal_commute_is_not_flagged(db):
    """School in the morning, home in the evening: different networks, hours
    apart, entirely ordinary."""
    user = _teacher(db, _school(db))
    _past_login(db, user, SCHOOL_A, minutes_ago=settings.signin_window_minutes + 60)

    assert check_sign_in(db, user=user, ip=SCHOOL_B, device=UA) is None


def test_a_first_ever_sign_in_has_nothing_to_compare(db):
    user = _teacher(db, _school(db))
    assert check_sign_in(db, user=user, ip=SCHOOL_A, device=UA) is None


def test_one_warning_per_window_not_one_per_sign_in(db):
    """A shared account signs in all day. The admin needs to know it happens,
    not to receive forty rows of it."""
    user = _teacher(db, _school(db))
    _past_login(db, user, SCHOOL_A, minutes_ago=3)

    first = check_sign_in(db, user=user, ip=SCHOOL_B, device=UA)
    db.commit()
    _past_login(db, user, SCHOOL_B, minutes_ago=1)
    second = check_sign_in(db, user=user, ip=SCHOOL_A, device=UA)
    db.commit()

    assert first is not None
    assert second is None


def test_another_teachers_sign_in_is_not_this_teachers_problem(db):
    school = _school(db)
    mine = _teacher(db, school, name="Mine")
    theirs = _teacher(db, school, name="Theirs")
    _past_login(db, theirs, SCHOOL_A, minutes_ago=2)

    assert check_sign_in(db, user=mine, ip=SCHOOL_B, device=UA) is None


def test_a_failed_attempt_is_not_a_sign_in(db):
    """Only successful sign-ins are two people being in two places."""
    user = _teacher(db, _school(db))
    log = record_event(
        db,
        event=SecurityEvent.failed_login,
        status=SecurityStatus.warning,
        ip=SCHOOL_A,
        device=UA,
        user=user,
    )
    log.timestamp = datetime.now(timezone.utc) - timedelta(minutes=2)
    db.commit()

    assert check_sign_in(db, user=user, ip=SCHOOL_B, device=UA) is None


def test_a_missing_address_is_not_evidence(db):
    user = _teacher(db, _school(db))
    _past_login(db, user, "", minutes_ago=2)

    assert check_sign_in(db, user=user, ip=SCHOOL_B, device=UA) is None
    assert check_sign_in(db, user=user, ip="", device=UA) is None


# --- The unit the comparison is made in ------------------------------------ #


@pytest.mark.parametrize(
    "left,right,same",
    [
        (SCHOOL_A, SCHOOL_A_NEIGHBOUR, True),
        (SCHOOL_A, SCHOOL_B, False),
        ("2001:db8:1:1::5", "2001:db8:1:1::99", True),
        ("2001:db8:1:1::5", "2001:db8:9:9::5", False),
        ("not-an-ip", "not-an-ip", True),
    ],
)
def test_networks_compare_by_neighbourhood(left, right, same):
    assert (network_of(left) == network_of(right)) is same


# --- Showing a place you can stand behind ---------------------------------- #


def test_a_wrong_city_is_not_asserted_by_default(db):
    """Measured on real Lebanese addresses, every correct one reported "Beirut".
    The country is right; the city is the ISP's gateway."""
    assert settings.geoip_precision == "country"
    assert geoip.display("Beirut, Lebanon") == "Lebanon"
    assert geoip.display("Beirut, Mount Lebanon, Lebanon") == "Lebanon"
    # Nothing known stays nothing.
    assert geoip.display("") == ""
    # And the local-network label isn't a country to be trimmed.
    assert geoip.display("Local network") == "Local network"
