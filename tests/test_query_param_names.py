"""Query parameters have to be named what the client actually sends.

The frontend sends camelCase — `?schoolId=...` — and FastAPI binds a parameter
by its exact name unless an alias says otherwise. A handler that declares
`school_id` therefore never receives it: the value silently arrives as None and
the filter it controls quietly does nothing.

That is not a hypothetical. A super-admin choosing a school on the ICT Fair page
was shown every school's sections, because the school never reached the handler.

These tests go through the HTTP layer on purpose. Every other test in this suite
calls the router functions directly, which passes the argument by name in Python
and cannot see the mistake — the binding is the thing being tested, so the
request has to be a real one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import FairProject, FairSection, School, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.utils import new_id


@pytest.fixture()
def client(db):
    """The real app, with the session and the signed-in user supplied."""
    holder: dict[str, User] = {}

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    try:
        with TestClient(app) as c:
            yield c, holder
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def two_schools(db):
    """Two schools, each running its own fair. They share nothing."""
    first = School(id=new_id("sch"), name="Balamand", country="LB", city="Beirut")
    second = School(id=new_id("sch"), name="Elsewhere", country="LB", city="Tripoli")
    db.add_all([first, second])
    db.flush()

    def _section(school, title, grades):
        section = FairSection(
            id=new_id("fsec"), school_id=school.id, title=title, grades=grades
        )
        db.add(section)
        db.flush()
        uploaded = UploadedFile(
            id=new_id("file"), filename=f"{title}.pdf", content_type="application/pdf",
            size_bytes=10, storage_path=f"{title}.pdf",
        )
        db.add(uploaded)
        db.flush()
        db.add(FairProject(
            id=new_id("fair"), title=f"{title} project", file_id=uploaded.id,
            section_id=section.id,
        ))
        return section

    _section(first, "Grade 11-12", ["G11", "G12"])
    _section(first, "Grade 1->3", ["G1", "G2", "G3"])
    _section(second, "Their own section", ["G7"])

    owner = User(
        id=new_id("u"), name="Owner", email=f"{new_id('o')}@x.com", password_hash="x",
        role=Role.super_admin, status=UserStatus.active, grades=[],
    )
    db.add(owner)
    db.commit()
    return {"first": first, "second": second, "owner": owner}


def test_choosing_a_school_shows_only_that_school_s_sections(client, two_schools):
    """The reported bug. Sections created for one school appeared under another,
    because `?schoolId=` never reached the handler and the filter was skipped."""
    c, holder = client
    holder["user"] = two_schools["owner"]

    res = c.get(f"/api/fair/sections?schoolId={two_schools['first'].id}")

    assert res.status_code == 200
    titles = {s["title"] for s in res.json()}
    assert titles == {"Grade 11-12", "Grade 1->3"}
    assert "Their own section" not in titles


def test_the_other_school_gets_its_own(client, two_schools):
    c, holder = client
    holder["user"] = two_schools["owner"]

    res = c.get(f"/api/fair/sections?schoolId={two_schools['second'].id}")

    assert {s["title"] for s in res.json()} == {"Their own section"}


def test_no_school_still_means_every_school(client, two_schools):
    """Unfiltered is a real answer for a super-admin, and how the page loads
    before a school is picked. It just must not be what a filtered request
    silently falls back to."""
    c, holder = client
    holder["user"] = two_schools["owner"]

    res = c.get("/api/fair/sections")

    # Scoped to this test's own sections: the suite shares a database, so a bare
    # count would depend on whatever else has run.
    titles = {s["title"] for s in res.json()}
    assert {"Grade 11-12", "Grade 1->3", "Their own section"} <= titles


def test_the_school_report_download_honours_the_school(client, two_schools):
    """Same mistake, same file. A super-admin asking for one school's report was
    handed the platform-wide one instead."""
    c, holder = client
    holder["user"] = two_schools["owner"]

    res = c.get(f"/api/reports/super/download?schoolId={two_schools['first'].id}")

    assert res.status_code == 200
    # The filename carries the school when one was asked for, which is the only
    # visible difference between this and the platform-wide report.
    disposition = res.headers.get("content-disposition", "")
    assert "Balamand" in disposition, disposition
