"""Nothing a test writes survives it, committed or not.

The suite shares one database and never truncates it. For a long time the
only thing keeping tests apart was that nothing called ``commit`` — untrue as
soon as a route that saves was tested, and the leftovers broke tests in other
modules: a Grade 1 lesson committed by the file-view tests took the open slot
in its track and failed a kindergarten test that had nothing to do with files.

These run in order on purpose. Each pair writes in the first test and looks
in the second, from a fresh session on a fresh connection, which is where a
leftover would be seen.
"""

from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models import School
from app.utils import new_id

COMMITTED = f"isolation-committed-{new_id('x')}"
BY_THE_APP = f"isolation-by-the-app-{new_id('x')}"


def _school(name: str) -> School:
    return School(id=new_id("sch"), name=name, country="LB", city="Beirut", program_year=2)


def _found(db, name: str) -> bool:
    return db.scalar(select(School.id).where(School.name == name)) is not None


def test_a_test_commits(db):
    db.add(_school(COMMITTED))
    db.commit()

    assert _found(db, COMMITTED), "precondition: the commit is visible inside the test"


def test_and_the_next_one_does_not_see_it(db):
    assert not _found(db, COMMITTED), "a committed row outlived the test that wrote it"


def test_the_app_opens_its_own_session_and_commits(db):
    # What the chat-history and refund paths do: a session of their own,
    # opened and committed inside a request.
    with SessionLocal() as theirs:
        theirs.add(_school(BY_THE_APP))
        theirs.commit()

    # And the test can see it, which on a separate connection it could not.
    assert _found(db, BY_THE_APP), "the app's session cannot see the test's world"


def test_and_that_does_not_survive_either(db):
    assert not _found(db, BY_THE_APP), "a row committed by the app outlived the test"
