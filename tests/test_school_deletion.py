"""Deleting a school, and what it takes with it.

The guard counted users and lessons. Everything else the school owns went
through the database's own cascades without being mentioned — and ICT Fair
sections are owned by a school and by nothing else.

Which gave it two ways to go wrong and no way to go right. A school whose
sections were empty lost them to the cascade, silently, on a request that
reported success. A school whose sections held projects hit the RESTRICT on
`fair_projects.section_id` instead: the cascade was refused, the delete failed
with a driver-level error rather than the 409 this endpoint takes care to
produce for users and lessons, and the school could not be deleted at all
without anyone being told why.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models import (
    FairProject,
    FairSection,
    Lesson,
    School,
    UploadedFile,
    User,
)
from app.models.enums import Role, UserStatus
from app.routers.schools import delete_school
from app.utils import new_id


@pytest.fixture()
def boss(db) -> User:
    user = User(
        id=new_id("u"), name="Owner", email=f"{new_id('e')}@example.com",
        password_hash="x", role=Role.super_admin, status=UserStatus.active, grades=[],
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def school(db) -> School:
    row = School(id=new_id("sch"), name="Closing Down", program_year=2)
    db.add(row)
    db.commit()
    return row


def _section(db, school: School) -> FairSection:
    row = FairSection(
        id=new_id("fsec"), school_id=school.id, title="Smart Home", grades=["G7"]
    )
    db.add(row)
    db.commit()
    return row


def _project(db, section: FairSection) -> FairProject:
    uploaded = UploadedFile(
        id=new_id("file"), filename="project.pdf", content_type="application/pdf",
        size_bytes=10, storage_path="project.pdf",
    )
    db.add(uploaded)
    db.flush()
    row = FairProject(
        id=new_id("fprj"), section_id=section.id, title="Door alarm", file_id=uploaded.id
    )
    db.add(row)
    db.commit()
    return row


def test_an_empty_school_is_deleted(db, boss, school):
    delete_school(school.id, db, boss)

    assert db.get(School, school.id) is None


def test_a_school_with_sections_is_refused_rather_than_stripped(db, boss, school):
    """The silent half. These went to the cascade on a request that reported
    success, so the sections were gone and the deletion looked clean."""
    section = _section(db, school)

    with pytest.raises(HTTPException) as err:
        delete_school(school.id, db, boss)

    assert err.value.status_code == 409
    assert "ICT Fair section" in err.value.detail
    assert db.get(School, school.id) is not None
    assert db.get(FairSection, section.id) is not None


def test_a_school_whose_sections_hold_projects_is_refused_the_same_way(db, boss, school):
    """The loud half. The cascade met the RESTRICT on `fair_projects.section_id`
    and came back as a driver error, not as this endpoint's own 409 — so the
    school could not be deleted and nothing said what was holding it."""
    section = _section(db, school)
    _project(db, section)

    with pytest.raises(HTTPException) as err:
        delete_school(school.id, db, boss)

    assert err.value.status_code == 409
    assert "ICT Fair section" in err.value.detail
    assert db.get(School, school.id) is not None


def test_users_and_lessons_still_hold_a_school(db, boss, school):
    """Unchanged, and worth keeping said while the guard around it moves."""
    db.add(
        User(
            id=new_id("u"), name="T", email=f"{new_id('e')}@example.com",
            password_hash="x", role=Role.teacher, status=UserStatus.active,
            school_id=school.id, grades=[],
        )
    )
    db.commit()

    with pytest.raises(HTTPException) as err:
        delete_school(school.id, db, boss)

    assert err.value.status_code == 409
    assert "1 user" in err.value.detail


def test_the_refusal_names_only_what_is_actually_there(db, boss, school):
    """The old wording printed every count whichever were zero, so a school held
    up by a single lesson was refused with "0 user(s) and 1 lesson(s)"."""
    db.add(
        Lesson(
            id=new_id("les"), title="Grade 7 python lesson 01", grade=7,
            subject="STEAM", school_id=school.id, language="en", year=2, lesson_no=1,
        )
    )
    db.commit()

    with pytest.raises(HTTPException) as err:
        delete_school(school.id, db, boss)

    detail = err.value.detail
    assert "1 lesson" in detail
    assert "0 user" not in detail
    assert "ICT Fair" not in detail


def test_one_of_a_thing_is_not_called_one_things(db, boss, school):
    _section(db, school)

    with pytest.raises(HTTPException) as err:
        delete_school(school.id, db, boss)

    assert "1 ICT Fair section." in err.value.detail + "."
    assert "1 ICT Fair sections" not in err.value.detail
