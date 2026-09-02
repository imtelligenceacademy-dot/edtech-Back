"""ICT Fair sections, and the school scoping they exist to enforce.

Schools each run their own fair, so the load-bearing assertion in this file is
that one school's teacher never sees another school's projects. Before sections
existed the fair endpoint returned every project to every teacher who had the
access flag, which is the bug these tests are here to keep fixed.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models import FairProject, FairSection, School, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.routers.fair import (
    create_section,
    delete_section,
    list_fair_projects,
    list_sections,
    list_unfiled_projects,
    update_fair_project,
    update_section,
)
from app.schemas.fair import (
    FairProjectUpdate,
    FairSectionCreate,
    FairSectionUpdate,
)
from app.utils import new_id


def _school(db, name: str) -> School:
    school = School(id=new_id("sch"), name=name, country="LB", city="Beirut")
    db.add(school)
    db.commit()
    return school


def _user(
    db,
    role: Role,
    school: School | None = None,
    *,
    fair_access: bool = False,
    grades: list[str] | None = None,
) -> User:
    user = User(
        id=new_id("u"),
        name=f"{role.value} user",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=role,
        status=UserStatus.active,
        school_id=school.id if school else None,
        grades=grades or [],
        ict_fair_access=fair_access,
    )
    db.add(user)
    db.commit()
    return user


def _section(db, school: School, title: str, grades: list[str]) -> FairSection:
    section = FairSection(
        id=new_id("fsec"), school_id=school.id, title=title, grades=grades
    )
    db.add(section)
    db.commit()
    return section


def _project(db, title: str, section: FairSection | None = None) -> FairProject:
    # A real uploaded_files row, because the FK is enforced (SQLite runs with
    # PRAGMA foreign_keys=ON here, same as production Postgres).
    uploaded = UploadedFile(
        id=new_id("file"),
        filename=f"{title}.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        storage_path=f"{title}.pdf",
    )
    db.add(uploaded)
    db.flush()

    project = FairProject(
        id=new_id("fair"),
        title=title,
        file_id=uploaded.id,
        section_id=section.id if section else None,
    )
    db.add(project)
    db.commit()
    return project


# --- Scoping: the point of the whole feature -------------------------------- #


def test_teacher_never_sees_another_schools_projects(db):
    mine = _school(db, "My School")
    theirs = _school(db, "Other School")
    _project(db, "Ours", _section(db, mine, "Robotics", ["G7"]))
    _project(db, "Theirs", _section(db, theirs, "Robotics", ["G7"]))

    teacher = _user(db, Role.teacher, mine, fair_access=True, grades=["G7"])

    titles = {s.title for s in list_sections(db=db, current=teacher)}
    assert titles == {"Robotics"}
    schools = {s.school_id for s in list_sections(db=db, current=teacher)}
    assert schools == {mine.id}

    flat = {p.title for p in list_fair_projects(db=db, current=teacher)}
    assert flat == {"Ours"}, "the other school's project must not appear"


def test_school_admin_is_scoped_to_their_own_school_too(db):
    mine = _school(db, "Admin's School")
    theirs = _school(db, "Elsewhere")
    _project(db, "Ours", _section(db, mine, "Sensors", ["G8"]))
    _project(db, "Theirs", _section(db, theirs, "Sensors", ["G8"]))

    admin = _user(db, Role.school_admin, mine)

    assert {s.school_id for s in list_sections(db=db, current=admin)} == {mine.id}
    assert {p.title for p in list_fair_projects(db=db, current=admin)} == {"Ours"}


def test_teacher_without_fair_access_sees_nothing(db):
    school = _school(db, "Gated")
    _project(db, "Hidden", _section(db, school, "Robotics", ["G7"]))

    teacher = _user(db, Role.teacher, school, fair_access=False)

    assert list_sections(db=db, current=teacher) == []
    assert list_fair_projects(db=db, current=teacher) == []


def test_user_with_no_school_sees_nothing(db):
    """Fails closed. Without a school there is no scope to apply, so the answer
    is nothing — not everything."""
    school = _school(db, "Somewhere")
    _project(db, "Not theirs", _section(db, school, "Robotics", ["G7"]))

    stray = _user(db, Role.teacher, None, fair_access=True)

    assert list_sections(db=db, current=stray) == []
    assert list_fair_projects(db=db, current=stray) == []


def test_super_admin_sees_every_school_and_can_filter_to_one(db):
    a = _school(db, "School A")
    b = _school(db, "School B")
    _section(db, a, "A section", ["G1"])
    _section(db, b, "B section", ["G1"])
    owner = _user(db, Role.super_admin)

    every = list_sections(db=db, current=owner)
    assert {s.school_id for s in every} >= {a.id, b.id}

    just_a = list_sections(school_id=a.id, db=db, current=owner)
    assert {s.school_id for s in just_a} == {a.id}


def test_school_filter_cannot_be_used_to_escape_your_own_school(db):
    """A scoped user passing someone else's schoolId is still scoped to theirs."""
    mine = _school(db, "Mine")
    theirs = _school(db, "Theirs")
    _section(db, mine, "Mine section", ["G7"])
    _section(db, theirs, "Their section", ["G7"])

    teacher = _user(db, Role.teacher, mine, fair_access=True)

    sections = list_sections(school_id=theirs.id, db=db, current=teacher)

    assert {s.school_id for s in sections} == {mine.id}
    assert {s.title for s in sections} == {"Mine section"}


def test_unfiled_project_belongs_to_no_school_so_no_teacher_sees_it(db):
    school = _school(db, "Anywhere")
    _project(db, "Never filed", None)
    teacher = _user(db, Role.teacher, school, fair_access=True)
    owner = _user(db, Role.super_admin)

    assert list_fair_projects(db=db, current=teacher) == []
    assert "Never filed" in {p.title for p in list_unfiled_projects(db=db, _=owner)}


# --- Section CRUD ----------------------------------------------------------- #


def test_created_section_nests_its_projects_and_names_its_school(db):
    school = _school(db, "Antonine Sisters")
    section = _section(db, school, "Smart Home", ["G7", "G8"])
    _project(db, "Plant Watering", section)
    _project(db, "Smart Fan", section)
    owner = _user(db, Role.super_admin)

    [out] = [s for s in list_sections(school_id=school.id, db=db, current=owner)]

    assert out.school_name == "Antonine Sisters"
    assert out.grades == ["G7", "G8"]
    assert {p.title for p in out.projects} == {"Plant Watering", "Smart Fan"}


def test_grades_are_normalised_to_curriculum_order_without_duplicates(db):
    school = _school(db, "Ordering")
    owner = _user(db, Role.super_admin)

    out = create_section(
        payload=FairSectionCreate(
            school_id=school.id,
            title="Mixed",
            # Out of order, duplicated, lowercase, and one that does not exist.
            grades=["G8", "kg1", "G8", "G2", "NOPE"],
        ),
        db=db,
        current=owner,
    )

    assert out.grades == ["KG1", "G2", "G8"]


def test_creating_a_section_for_an_unknown_school_is_refused(db):
    owner = _user(db, Role.super_admin)

    with pytest.raises(HTTPException) as exc:
        create_section(
            payload=FairSectionCreate(school_id="sch_nope", title="Orphan"),
            db=db,
            current=owner,
        )

    assert exc.value.status_code == 404


def test_update_leaves_omitted_fields_alone(db):
    school = _school(db, "Patching")
    section = _section(db, school, "Original", ["G3"])
    section.blurb = "Keep me"
    db.commit()
    owner = _user(db, Role.super_admin)

    out = update_section(
        section_id=section.id,
        payload=FairSectionUpdate(title="Renamed"),
        db=db,
        current=owner,
    )

    assert out.title == "Renamed"
    assert out.blurb == "Keep me", "an omitted field must not be cleared"
    assert out.grades == ["G3"]


def test_deleting_a_section_that_still_holds_projects_is_refused(db):
    school = _school(db, "Not empty")
    section = _section(db, school, "Full", ["G5"])
    _project(db, "Still here", section)
    owner = _user(db, Role.super_admin)

    with pytest.raises(HTTPException) as exc:
        delete_section(section_id=section.id, db=db, _=owner)

    # 409, not 404 — the section exists, the request conflicts with its state.
    assert exc.value.status_code == 409
    assert "1 project" in exc.value.detail
    assert db.get(FairSection, section.id) is not None


def test_empty_section_deletes(db):
    school = _school(db, "Empty")
    section = _section(db, school, "Nothing here", ["G5"])
    owner = _user(db, Role.super_admin)

    delete_section(section_id=section.id, db=db, _=owner)

    assert db.get(FairSection, section.id) is None


# --- Filing projects -------------------------------------------------------- #


def test_filing_an_unfiled_project_gives_it_a_school(db):
    school = _school(db, "Filing")
    section = _section(db, school, "Destination", ["G6"])
    project = _project(db, "Loose", None)
    owner = _user(db, Role.super_admin)
    teacher = _user(db, Role.teacher, school, fair_access=True)

    assert list_fair_projects(db=db, current=teacher) == []

    update_fair_project(
        project_id=project.id,
        payload=FairProjectUpdate(section_id=section.id),
        db=db,
        _=owner,
    )

    assert {p.title for p in list_fair_projects(db=db, current=teacher)} == {"Loose"}


def test_a_project_can_be_moved_back_out_of_a_section(db):
    school = _school(db, "Unfiling")
    section = _section(db, school, "Source", ["G6"])
    project = _project(db, "Moving out", section)
    owner = _user(db, Role.super_admin)

    out = update_fair_project(
        project_id=project.id,
        payload=FairProjectUpdate(section_id=None),
        db=db,
        _=owner,
    )

    assert out.section_id is None
    assert "Moving out" in {p.title for p in list_unfiled_projects(db=db, _=owner)}


def test_moving_a_project_into_a_missing_section_is_refused(db):
    school = _school(db, "Bad move")
    section = _section(db, school, "Real", ["G6"])
    project = _project(db, "Stays put", section)
    owner = _user(db, Role.super_admin)

    with pytest.raises(HTTPException) as exc:
        update_fair_project(
            project_id=project.id,
            payload=FairProjectUpdate(section_id="fsec_nope"),
            db=db,
            _=owner,
        )

    assert exc.value.status_code == 404
    db.refresh(project)
    assert project.section_id == section.id, "the move must not have half-applied"


def test_renaming_a_project_leaves_its_section_alone(db):
    school = _school(db, "Renaming")
    section = _section(db, school, "Home", ["G6"])
    project = _project(db, "Old name", section)
    owner = _user(db, Role.super_admin)

    out = update_fair_project(
        project_id=project.id,
        payload=FairProjectUpdate(title="New name"),
        db=db,
        _=owner,
    )

    assert out.title == "New name"
    assert out.section_id == section.id
