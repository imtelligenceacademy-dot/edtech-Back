"""Uploading an ICT Fair project.

Nothing in this suite posted to /api/fair, which is how the endpoint came to
raise NameError on every call and stay green through CI. `read_upload_capped`
and `UploadTooLarge` were added to the handler and imported into files.py only,
so the first line of real work in the function referenced a name the module did
not have — every upload, every school, an unconditional 500.

The size cap is the reason that call is there: the whole body used to be read
into memory before the limit was checked.
"""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException, UploadFile

from app.config import settings
from app.models import FairProject, FairSection, School, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.routers.fair import upload_fair_project
from app.services.file_storage import upload_root
from app.utils import new_id

PDF_BYTES = b"%PDF-1.4\n%fake pdf used only by the tests\n"


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
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


def _section(db) -> FairSection:
    school = School(
        id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2
    )
    db.add(school)
    db.flush()
    section = FairSection(
        id=new_id("fs"), school_id=school.id, title="Robotics", grades=["G7"]
    )
    db.add(section)
    db.commit()
    return section


def _upload(content: bytes = PDF_BYTES, name: str = "Smart Home.pdf") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


def test_a_fair_project_can_actually_be_uploaded(db):
    """The whole endpoint, end to end. It answered 500 to this."""
    section = _section(db)

    project = upload_fair_project(
        file=_upload(), section_id=section.id, db=db, current=_boss(db)
    )

    assert project.title == "Smart Home"
    assert project.section_id == section.id
    stored = db.get(UploadedFile, project.file_id)
    assert stored is not None
    # The bytes reached the disk, not just the row.
    assert (upload_root() / stored.storage_path).read_bytes() == PDF_BYTES
    assert db.get(FairProject, project.id) is not None


def test_an_oversized_upload_is_refused_by_the_cap_rather_than_read_whole(db, monkeypatch):
    """`read_upload_capped` is why the missing import was there to be missed."""
    section = _section(db)
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    too_big = b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024)
    before = db.query(FairProject).count()

    with pytest.raises(HTTPException) as err:
        upload_fair_project(
            file=_upload(too_big), section_id=section.id, db=db, current=_boss(db)
        )

    assert err.value.status_code == 413
    db.rollback()
    assert db.query(FairProject).count() == before
    # Nothing was written before the cap refused it.
    root = upload_root()
    assert not root.exists() or not any(root.iterdir())


def test_a_file_that_is_not_a_pdf_is_refused(db):
    section = _section(db)
    before = db.query(FairProject).count()

    with pytest.raises(HTTPException) as err:
        upload_fair_project(
            file=_upload(b"MZ not a pdf at all"), section_id=section.id, db=db, current=_boss(db)
        )

    assert err.value.status_code == 400
    db.rollback()
    assert db.query(FairProject).count() == before


def test_an_unknown_section_is_refused_before_any_bytes_are_written(db):
    with pytest.raises(HTTPException) as err:
        upload_fair_project(
            file=_upload(), section_id="fs_does_not_exist", db=db, current=_boss(db)
        )

    assert err.value.status_code == 404
    db.rollback()
    root = upload_root()
    assert not root.exists() or not any(root.iterdir())
