"""Locking an account after failed sign-ins, and unlocking it again.

The lockout is meant to slow down somebody guessing a teacher's password. It
must not become a way to keep that teacher out, and it used to be exactly that:
`locked_until` was re-armed on *every* wrong password, including ones that
arrived while the account was already locked, and `failed_login_count` had no
decay. Four wrong guesses per quarter hour — deliberately too slow to trip the
network throttle — walked the escalation up to its 24-hour ceiling and then held
it there for as long as the sender cared to continue. Teachers cannot reset
their own passwords here, so the only way out was a super-admin, and the reset
bought only until the next attempt arrived.

These go through HTTP because the binding of address and account is half the
subject; the rest of the suite calls routers directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import LoginThrottle, SecurityLog, User
from app.models.enums import Role, SecurityEvent, UserStatus
from app.security import hash_password
from app.utils import new_id

PASSWORD = "correct-horse-battery"


def _aware(dt: datetime) -> datetime:
    """SQLite hands back a naive datetime for a tz-aware column, which is why
    app/routers/auth.py normalises every one it reads. Same here."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def teacher(db) -> User:
    user = User(
        id=new_id("u"),
        name="A Teacher",
        email=f"{new_id('t')}@example.com",
        password_hash=hash_password(PASSWORD),
        role=Role.teacher,
        status=UserStatus.active,
        grades=["G7"],
        language="en",
    )
    db.add(user)
    db.commit()
    return user


def _forget_the_network(db) -> None:
    """Drop the per-address throttle.

    These tests send more failures from one address than the network throttle
    allows, and it would answer first — with a 429 that never reaches the
    account at all. The network half has its own tests; this file is about what
    happens to the account.
    """
    db.execute(delete(LoginThrottle))
    db.commit()


def _wrong(client, db, teacher) -> int:
    _forget_the_network(db)
    return client.post(
        "/api/auth/login",
        json={"email": teacher.email, "password": "not-the-password"},
    ).status_code


def _right(client, db, teacher) -> int:
    _forget_the_network(db)
    return client.post(
        "/api/auth/login", json={"email": teacher.email, "password": PASSWORD}
    ).status_code


def _events(db, teacher, event: SecurityEvent) -> int:
    return len(
        list(
            db.scalars(
                select(SecurityLog).where(
                    SecurityLog.user_id == teacher.id, SecurityLog.event == event
                )
            )
        )
    )


def _lock_it(client, db, teacher) -> datetime:
    for _ in range(settings.max_failed_logins):
        assert _wrong(client, db, teacher) == 401
    db.refresh(teacher)
    assert teacher.locked_until is not None, "precondition: the account is locked"
    return teacher.locked_until


def test_enough_wrong_passwords_lock_the_account(client, db, teacher):
    locked_until = _lock_it(client, db, teacher)

    assert _aware(locked_until) > datetime.now(timezone.utc)
    assert teacher.failed_login_count == settings.max_failed_logins
    # Even with the right password, a locked account is refused.
    assert _right(client, db, teacher) == 429


def test_another_wrong_password_does_not_extend_the_lock(client, db, teacher):
    """The whole bug, in one assertion.

    While the lock stands, further attempts tell us nothing new — the account is
    already shut. Re-arming on each one meant the lock expired only once the
    sender stopped, which is not a lockout, it is a lock-out.
    """
    locked_until = _lock_it(client, db, teacher)
    count = teacher.failed_login_count

    for _ in range(5):
        assert _wrong(client, db, teacher) == 401

    db.refresh(teacher)
    assert teacher.locked_until == locked_until, "the lock must not move"
    assert teacher.failed_login_count == count, "and must not escalate"


def test_attempts_against_a_locked_account_are_written_down_once(client, db, teacher):
    """Each attempt leaves one line, not two.

    A locked account used to record `failed_login` *and* `account_locked` on
    every attempt, at whatever rate they arrived — so the Security Logs screen,
    which is where an admin goes to find out what is happening, filled up with
    the attack instead of describing it.
    """
    _lock_it(client, db, teacher)
    failed = _events(db, teacher, SecurityEvent.failed_login)
    locked = _events(db, teacher, SecurityEvent.account_locked)

    for _ in range(3):
        _wrong(client, db, teacher)

    assert _events(db, teacher, SecurityEvent.failed_login) == failed + 3
    assert _events(db, teacher, SecurityEvent.account_locked) == locked


def test_a_lock_expires_on_its_own(client, db, teacher):
    _lock_it(client, db, teacher)

    # Nothing arrives while it runs down.
    teacher.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    assert _right(client, db, teacher) == 200


def test_failures_spread_out_do_not_accumulate(client, db, teacher):
    """The count describes a burst, not the account's whole history.

    Without a window it only ever climbed, so a teacher who mistypes her
    password a few times each term eventually reaches a number that locks her
    out for a day on the next slip — and a stranger who knows her email can walk
    her there deliberately, slowly enough that nothing else notices.
    """
    for _ in range(settings.max_failed_logins - 1):
        assert _wrong(client, db, teacher) == 401
    db.refresh(teacher)
    assert teacher.locked_until is None

    # The run goes quiet for longer than the window.
    teacher.failed_login_window_started_at = datetime.now(timezone.utc) - timedelta(
        minutes=settings.failed_login_window_minutes + 1
    )
    db.commit()

    for _ in range(settings.max_failed_logins - 1):
        assert _wrong(client, db, teacher) == 401

    db.refresh(teacher)
    assert teacher.failed_login_count == settings.max_failed_logins - 1, (
        "the second run should start from zero, not carry the first"
    )
    assert teacher.locked_until is None, "eight scattered typos are not an attack"


def test_signing_in_clears_the_record(client, db, teacher):
    for _ in range(settings.max_failed_logins - 1):
        _wrong(client, db, teacher)
    db.refresh(teacher)
    assert teacher.failed_login_count > 0

    assert _right(client, db, teacher) == 200

    db.refresh(teacher)
    assert teacher.failed_login_count == 0
    assert teacher.locked_until is None
    assert teacher.failed_login_window_started_at is None, (
        "a stale window would make the next typo count as part of the old run"
    )
