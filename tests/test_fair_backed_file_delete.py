"""Deleting a PDF that an ICT Fair project is built on.

`FairProject.file_id` is ON DELETE CASCADE. Removing the file row therefore
removes the project — silently, through the database rather than through any
code path that knows what a fair project is, and with a 204 returned for it.

The Files list hides fair-backed PDFs, which is the only reason this was hard
to reach: the file id is on every fair project response, so an id in hand was
always enough. These cover both delete routes refusing, and an ordinary file
still deleting.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models import FairProject, FairSection, Lesson, School, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.routers.files import bulk_delete, delete_file
from app.schemas.file import FileSelection
from app.services.file_storage import upload_root
from app.utils import new_id

PDF_BYTES = b"%PDF-1.4\n%fake pdf used only by the tests\n"


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "files"))


def _boss(db) -> User:
    user = User(
        id=new_id("u"),
        name="owner",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.super_admin,
        status=UserStatus.active,
        grades=[],
    )
    db.add(user)
    db.commit()
    return user


def _stored_pdf(db, *, linked_lesson_id: str | None = None) -> UploadedFile:
    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    file_id = new_id("file")
    (root / f"{file_id}.pdf").write_bytes(PDF_BYTES)
    uploaded = UploadedFile(
        id=file_id,
        filename=f"{file_id}.pdf",
        content_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        storage_path=f"{file_id}.pdf",
        linked_lesson_id=linked_lesson_id,
    )
    db.add(uploaded)
    db.commit()
    return uploaded


def _fair_project(db, uploaded: UploadedFile, *, title="Smart Home") -> FairProject:
    school = School(
        id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.flush()
    section = FairSection(
        id=new_id("fs"), school_id=school.id, title="Robotics", grades=["G7"]
    )
    db.add(section)
    db.flush()
    project = FairProject(
        id=new_id("fp"), title=title, file_id=uploaded.id, section_id=section.id
    )
    db.add(project)
    db.commit()
    return project


def test_single_delete_refuses_a_fair_backed_pdf(db):
    uploaded = _stored_pdf(db)
    project = _fair_project(db, uploaded)

    with pytest.raises(HTTPException) as err:
        delete_file(file_id=uploaded.id, db=db, _=_boss(db))

    assert err.value.status_code == 409
    # The message has to name the project, or the admin is told "no" about a
    # PDF whose Files row gives no hint that a fair project exists at all.
    assert "Smart Home" in err.value.detail
    db.rollback()
    assert db.get(FairProject, project.id) is not None
    assert db.get(UploadedFile, uploaded.id) is not None
    assert (upload_root() / f"{uploaded.id}.pdf").exists()


def test_bulk_delete_refuses_and_takes_nothing_with_it(db):
    fair_pdf = _stored_pdf(db)
    project = _fair_project(db, fair_pdf, title="Line Follower")
    bystander = _stored_pdf(db)

    with pytest.raises(HTTPException) as err:
        bulk_delete(
            payload=FileSelection(file_ids=[bystander.id, fair_pdf.id]),
            db=db,
            _=_boss(db),
        )

    assert err.value.status_code == 409
    assert "Line Follower" in err.value.detail
    db.rollback()
    # All-or-nothing: the unrelated PDF in the same selection survives too.
    assert db.get(FairProject, project.id) is not None
    assert db.get(UploadedFile, fair_pdf.id) is not None
    assert db.get(UploadedFile, bystander.id) is not None


def test_an_ordinary_unlinked_pdf_still_deletes(db):
    """The guard must refuse fair-backed files and nothing else."""
    uploaded = _stored_pdf(db)

    response = delete_file(file_id=uploaded.id, db=db, _=_boss(db))

    assert response.status_code == 204
    assert db.get(UploadedFile, uploaded.id) is None
    assert not (upload_root() / f"{uploaded.id}.pdf").exists()


def test_deleting_a_sibling_pdf_cannot_reach_the_fair_project_sideways(db):
    """The gap `bulk_delete` was corrected for and this route was not.

    Deleting one PDF of a lesson takes every PDF filed under that lesson. If one
    of those siblings backs a fair project, guarding only the clicked file let
    the delete reach the project through the cascade — 204, nothing logged.
    """
    lesson = Lesson(
        id=new_id("les"),
        title="Grade 7 lesson 01",
        grade=7,
        subject="STEAM",
        language="en",
        year=2,
        lesson_no=1,
    )
    db.add(lesson)
    db.flush()
    plain = _stored_pdf(db, linked_lesson_id=lesson.id)
    fair_backed = _stored_pdf(db, linked_lesson_id=lesson.id)
    project = _fair_project(db, fair_backed, title="Weather Station")

    # The admin clicks the *plain* one, which is not itself fair-backed.
    with pytest.raises(HTTPException) as err:
        delete_file(file_id=plain.id, db=db, _=_boss(db))

    assert err.value.status_code == 409
    assert "Weather Station" in err.value.detail
    db.rollback()
    assert db.get(FairProject, project.id) is not None
    assert db.get(Lesson, lesson.id) is not None
    assert db.get(UploadedFile, plain.id) is not None
