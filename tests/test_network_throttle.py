"""Throttling sign-ins by the address they arrive from.

The shape of it: five failures inside a window is one cycle, which locks the
address for fifteen minutes; two cycles bans it for a day. The ban is the part
that matters, because it is the only thing that stops a slow, patient attempt
on a whole school's worth of accounts.

It was unreachable. `cycle_count` is the only route to it, and a successful
sign-in from the address set it back to zero — so on a school NAT, where every
teacher shares one address and somebody gets in every few minutes all day, two
cycles could never accumulate. The half that hurt those teachers, the
fifteen-minute lock on the whole staff room, went on working perfectly.

So the rule is now about what the address has been doing rather than about the
last thing that happened on it: a run of cycles ages out on its own, a success
forgives the failures in progress but not the cycles already completed, and an
address that is signing people in is locked rather than banned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import LoginThrottle
from app.routers import auth as auth_router
from app.utils import new_id


@pytest.fixture()
def ip() -> str:
    return f"198.51.100.{new_id('n')[-6:]}"


@pytest.fixture()
def now() -> datetime:
    return datetime.now(timezone.utc)


def _one_cycle(db, ip: str, at: datetime) -> None:
    """A full window of failures from this address, and nothing else."""
    for _ in range(settings.login_ip_max_failures):
        auth_router._record_ip_failure(db, ip, at)
    db.flush()


def _throttle(db, ip: str) -> LoginThrottle:
    return db.get(LoginThrottle, ip)


def test_a_cycle_locks_the_address(db, ip, now):
    _one_cycle(db, ip, now)

    throttle = _throttle(db, ip)
    assert throttle.cycle_count == 1
    assert throttle.locked_until is not None
    assert throttle.blocked_at is None, "one cycle locks; it does not ban"


def test_two_cycles_with_nobody_signing_in_ban_the_address(db, ip, now):
    """The case the ban was written for, and the case it never reached.

    An address producing nothing but failures is somebody working through a list
    of emails. There is no teacher behind it to shut out.
    """
    _one_cycle(db, ip, now)
    _one_cycle(db, ip, now + timedelta(minutes=16))

    throttle = _throttle(db, ip)
    assert throttle.cycle_count >= settings.login_ip_ban_cycles
    assert throttle.blocked_at is not None
    assert throttle.reason == "Repeated failed login cycles"


def test_a_success_forgives_the_run_in_progress_but_not_the_cycles(db, ip, now):
    """The bug, in one assertion.

    Clearing `cycle_count` here is what made the ban unreachable: an attacker,
    or anyone at all holding one working account, need only sign in once between
    bursts and the record went back to nothing.
    """
    _one_cycle(db, ip, now)
    auth_router._record_ip_failure(db, ip, now + timedelta(minutes=16))
    db.flush()
    assert _throttle(db, ip).failed_count == 1

    auth_router._clear_ip_failures(db, ip, now + timedelta(minutes=17))
    db.flush()

    throttle = _throttle(db, ip)
    assert throttle.failed_count == 0, "the failures in progress are forgiven"
    assert throttle.locked_until is None, "and any lock standing over them"
    assert throttle.cycle_count == 1, "the completed cycle is not unsaid"
    assert throttle.last_success_at is not None


def test_an_address_that_signs_people_in_is_locked_but_never_banned(db, ip, now):
    """A school is not an attack, however many failures are mixed into it.

    Every teacher shares the one address, so somebody is always mistyping and
    somebody is always getting in. Shutting the whole staff room out for a day
    over that would be far worse than the thing the ban is for — and there is no
    self-service password reset here, so nobody could let them back in.
    """
    _one_cycle(db, ip, now)
    auth_router._clear_ip_failures(db, ip, now + timedelta(minutes=1))
    _one_cycle(db, ip, now + timedelta(minutes=16))

    throttle = _throttle(db, ip)
    assert throttle.cycle_count >= settings.login_ip_ban_cycles, (
        "the cycles are still counted"
    )
    assert throttle.blocked_at is None, "but a staff room is locked, not banned"
    assert throttle.locked_until is not None


def test_a_run_of_cycles_ages_out(db, ip, now):
    """Two cycles a term apart are not a sustained attack.

    Before this nothing but a success brought the count down, so an address
    nobody ever signs in from kept its cycles for good and the second one — at
    any distance at all from the first — banned it.
    """
    _one_cycle(db, ip, now)
    stale = now + timedelta(minutes=settings.login_ip_cycle_window_minutes + 1)

    _one_cycle(db, ip, stale)

    throttle = _throttle(db, ip)
    assert throttle.cycle_count == 1, "the second cycle starts a new run"
    assert throttle.blocked_at is None


def test_a_ban_still_lets_go_after_its_window(db, ip, now):
    """Unchanged, and worth keeping said: the ban is the heaviest thing here and
    it has to end by itself."""
    _one_cycle(db, ip, now)
    _one_cycle(db, ip, now + timedelta(minutes=16))
    throttle = _throttle(db, ip)
    assert throttle.blocked_at is not None

    throttle.blocked_at = now - timedelta(hours=settings.login_ip_block_hours + 1)
    db.flush()
    auth_router._enforce_ip_throttle(db, ip, now)

    throttle = _throttle(db, ip)
    assert throttle.blocked_at is None
    assert throttle.cycle_count == 0
    assert throttle.cycle_started_at is None


def test_a_banned_address_is_refused_while_the_ban_stands(db, ip, now):
    _one_cycle(db, ip, now)
    _one_cycle(db, ip, now + timedelta(minutes=16))

    with pytest.raises(HTTPException) as excinfo:
        auth_router._enforce_ip_throttle(db, ip, now + timedelta(minutes=20))
    assert excinfo.value.status_code == 429


def test_lifting_a_ban_survives_the_failure_that_follows_it(db, ip, now):
    """Two writes to one row in a single request, the second of which re-reads it.

    `_enforce_ip_throttle` lifts a ban that has served its time. A wrong password
    then sends the same request into `_record_ip_failure`, which re-reads the row
    under a lock to count the failure safely — and that re-read overwrites the
    instance from what the database holds. Unless the lift has been written down
    first it is silently undone: the ban comes back, and because a banned address
    records nothing, the failure being counted is dropped on the way out too.

    The session runs with autoflush off, so nothing does that writing implicitly.
    """
    db.add(
        LoginThrottle(
            ip=ip,
            blocked_at=now - timedelta(hours=settings.login_ip_block_hours + 1),
            cycle_count=settings.login_ip_ban_cycles,
            reason="Repeated failed login cycles",
        )
    )
    db.commit()

    auth_router._enforce_ip_throttle(db, ip, now)
    auth_router._record_ip_failure(db, ip, now)
    db.flush()

    throttle = _throttle(db, ip)
    assert throttle.blocked_at is None, "the lift must not be undone by the re-read"
    assert throttle.failed_count == 1, "and the failure still has to be counted"
