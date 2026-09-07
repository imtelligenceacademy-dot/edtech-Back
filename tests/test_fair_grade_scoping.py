"""A teacher sees the ICT Fair sections for the grades she actually teaches.

Sections carry the grades they are for, and for a long time nothing consulted
them: scoping stopped at the school, so a Grade 6 teacher opening the fair was
shown her school's Grade 1-3 projects too, and a "Show all grades" control
offered to reveal any the screen had filtered client-side.

Two halves have to hold, and the second is the one that matters. Hiding a
section from the list is cosmetic if the PDF behind it still opens for anyone
holding its id — so the file gate is asserted here as well as the list.
"""

from __future__ import annotations

import pytest

from app.models import FairProject, FairSection, School, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.routers.fair import list_fair_projects, list_sections
from app.routers.files import _can_access
from app.utils import new_id


def _school(db, name: str) -> School:
    school = School(id=new_id("sch"), name=name, country="LB", city="Beirut")
    db.add(school)
    db.commit()
    return school


def _teacher(db, school, grades, *, fair_access=True) -> User:
    user = User(
        id=new_id("u"),
        name="T",
        email=f"{new_id('t')}@x.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id if school else None,
        grades=grades,
        ict_fair_access=fair_access,
    )
    db.add(user)
    db.commit()
    return user


def _section(db, school, title, grades) -> FairSection:
    section = FairSection(
        id=new_id("fsec"), school_id=school.id, title=title, grades=grades
    )
    db.add(section)
    db.commit()
    return section


def _project(db, title, section) -> tuple[FairProject, UploadedFile]:
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
    return project, uploaded


@pytest.fixture()
def fair(db):
    """One school running a fair for three grade bands."""
    school = _school(db, "Grade scoping")
    juniors = _section(db, school, "Grade 1->3", ["G1", "G2", "G3"])
    middles = _section(db, school, "Grade 4-5", ["G4", "G5"])
    seniors = _section(db, school, "Grade 6", ["G6"])
    return {
        "school": school,
        "juniors": (juniors, _project(db, "Clap_Light", juniors)),
        "middles": (middles, _project(db, "Traffic_Light", middles)),
        "seniors": (seniors, _project(db, "Plant_Health_Guardian", seniors)),
    }


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #
def test_a_grade_six_teacher_sees_only_the_grade_six_section(db, fair):
    teacher = _teacher(db, fair["school"], ["G6"])

    titles = {s.title for s in list_sections(db=db, current=teacher)}
    assert titles == {"Grade 6"}

    projects = {p.title for p in list_fair_projects(db=db, current=teacher)}
    assert projects == {"Plant_Health_Guardian"}


def test_a_section_covering_several_grades_reaches_all_of_them(db, fair):
    """"Grades 1-3" is one project three grades share, not three copies."""
    for grade in ("G1", "G2", "G3"):
        teacher = _teacher(db, fair["school"], [grade])
        titles = {s.title for s in list_sections(db=db, current=teacher)}
        assert titles == {"Grade 1->3"}, f"{grade} should see the junior section"


def test_a_teacher_of_several_grades_sees_each_of_them(db, fair):
    teacher = _teacher(db, fair["school"], ["G3", "G6"])

    titles = {s.title for s in list_sections(db=db, current=teacher)}
    assert titles == {"Grade 1->3", "Grade 6"}
    assert "Grade 4-5" not in titles


def test_a_teacher_with_no_grades_sees_nothing(db, fair):
    """Fails closed. No grades means nothing overlaps, not everything does."""
    teacher = _teacher(db, fair["school"], [])

    assert list_sections(db=db, current=teacher) == []
    assert list_fair_projects(db=db, current=teacher) == []


def test_a_section_tagged_with_no_grades_reaches_no_teacher(db):
    """Nobody has said who an untagged section is for, and guessing "everyone"
    is exactly how the junior projects ended up in front of a Grade 6 teacher."""
    school = _school(db, "Untagged")
    section = _section(db, school, "Filed but not tagged", [])
    _project(db, "Orphan", section)

    teacher = _teacher(db, school, ["G6"])

    assert list_sections(db=db, current=teacher) == []
    assert list_fair_projects(db=db, current=teacher) == []


def test_a_school_admin_still_sees_every_grade_in_their_school(db, fair):
    """They administer the fair rather than teach it, so a grade they do not
    teach is still theirs to see — and is how an untagged section gets fixed."""
    admin = User(
        id=new_id("u"),
        name="A",
        email=f"{new_id('a')}@x.com",
        password_hash="x",
        role=Role.school_admin,
        status=UserStatus.active,
        school_id=fair["school"].id,
        grades=[],
    )
    db.add(admin)
    db.commit()

    titles = {s.title for s in list_sections(db=db, current=admin)}
    assert titles == {"Grade 1->3", "Grade 4-5", "Grade 6"}


# --------------------------------------------------------------------------- #
# The file behind it
# --------------------------------------------------------------------------- #
def test_the_pdf_of_another_grades_project_will_not_open(db, fair):
    """The load-bearing one. A teacher who has a project's id — from a stale
    tab, a shared link, a guess — must not get the bytes either."""
    teacher = _teacher(db, fair["school"], ["G6"])
    _, (_, junior_file) = fair["juniors"]
    _, (_, senior_file) = fair["seniors"]

    assert _can_access(db, teacher, senior_file) is True
    assert _can_access(db, teacher, junior_file) is False


def test_the_pdf_of_another_schools_project_will_not_open(db, fair):
    other = _school(db, "Elsewhere")
    theirs = _section(db, other, "Grade 6", ["G6"])
    _, their_file = _project(db, "Their_Grade_Six", theirs)

    # Same grade, different school: the school check still has to bite.
    teacher = _teacher(db, fair["school"], ["G6"])
    assert _can_access(db, teacher, their_file) is False


def test_a_teacher_without_fair_access_opens_nothing(db, fair):
    teacher = _teacher(db, fair["school"], ["G6"], fair_access=False)
    _, (_, senior_file) = fair["seniors"]

    assert _can_access(db, teacher, senior_file) is False


def test_a_super_admin_still_opens_everything(db, fair):
    owner = User(
        id=new_id("u"),
        name="O",
        email=f"{new_id('o')}@x.com",
        password_hash="x",
        role=Role.super_admin,
        status=UserStatus.active,
        grades=[],
    )
    db.add(owner)
    db.commit()
    _, (_, junior_file) = fair["juniors"]

    assert _can_access(db, owner, junior_file) is True
