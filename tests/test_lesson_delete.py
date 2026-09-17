"""Deleting a lesson: what it takes, and in which order.

Two corrections that landed on the file routes and were not carried across.

The bytes were unlinked inside the loop, before the commit — putting an
irreversible disk write ahead of the transaction that is meant to make the whole
thing all-or-nothing. A failed commit rolled the rows back and left the PDFs
gone, so every teacher still saw the lesson and every request for it 404'd, with
no way back. On Postgres a deadlock against a teacher writing progress for that
same lesson is enough to cause it.

And it had no ICT Fair guard at all, so a lesson with a fair-backed PDF filed
under it removed the fair project through the database cascade — unlogged,
unaccounted, and reported as 204.
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
from app.routers.lessons import delete_lesson
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


def _lesson_with_pdfs(db, count: int = 1) -> tuple[Lesson, list[UploadedFile]]:
    lesson = Lesson(
        id=new_id("les"),
        title=f"Grade 7 lesson {new_id('t')}",
        grade=7,
        subject="STEAM",
        language="en",
        year=2,
        lesson_no=1,
    )
    db.add(lesson)
    db.flush()
    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for _ in range(count):
        file_id = new_id("file")
        (root / f"{file_id}.pdf").write_bytes(PDF_BYTES)
        uploaded = UploadedFile(
            id=file_id,
            filename=f"{file_id}.pdf",
            content_type="application/pdf",
            size_bytes=len(PDF_BYTES),
            storage_path=f"{file_id}.pdf",
            linked_lesson_id=lesson.id,
        )
        db.add(uploaded)
        files.append(uploaded)
    db.commit()
    return lesson, files


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


# --- the ordering ---------------------------------------------------------- #


def test_a_failed_commit_leaves_the_pdfs_on_disk(db, monkeypatch):
    """The rows roll back, so the bytes must still be there to match them."""
    lesson, files = _lesson_with_pdfs(db, count=2)
    paths = [upload_root() / f"{f.id}.pdf" for f in files]
    assert all(p.exists() for p in paths), "precondition: the bytes are there"
    # Built before the commit is sabotaged — `_boss` commits, and as an inline
    # argument it exploded first, so the handler never ran and the test passed
    # without exercising anything.
    boss = _boss(db)

    def explode():
        raise RuntimeError("deadlock detected")

    monkeypatch.setattr(db, "commit", explode)

    with pytest.raises(RuntimeError):
        delete_lesson(lesson_id=lesson.id, db=db, _=boss)

    db.rollback()
    # The lesson survived the failure; if the bytes had not, every teacher would
    # still see it and every request for it would 404 with no way back.
    assert all(p.exists() for p in paths)
    assert db.get(Lesson, lesson.id) is not None


def test_an_ordinary_delete_still_removes_the_rows_and_the_bytes(db):
    """The guard and the reordering must not stop the thing working."""
    lesson, files = _lesson_with_pdfs(db, count=2)
    paths = [upload_root() / f"{f.id}.pdf" for f in files]

    response = delete_lesson(lesson_id=lesson.id, db=db, _=_boss(db))

    assert response.status_code == 204
    assert db.get(Lesson, lesson.id) is None
    for f in files:
        assert db.get(UploadedFile, f.id) is None
    assert not any(p.exists() for p in paths)


# --- the fair guard -------------------------------------------------------- #


def test_deleting_a_lesson_refuses_to_take_a_fair_project_with_it(db):
    lesson, files = _lesson_with_pdfs(db, count=2)
    # One of the lesson's PDFs also backs a fair project — reachable through the
    # route that links an arbitrary file to a lesson.
    project = _fair_project(db, files[1], title="Line Follower")

    with pytest.raises(HTTPException) as err:
        delete_lesson(lesson_id=lesson.id, db=db, _=_boss(db))

    assert err.value.status_code == 409
    assert "Line Follower" in err.value.detail
    db.rollback()
    assert db.get(FairProject, project.id) is not None
    assert db.get(Lesson, lesson.id) is not None
    # Nothing was written to disk either: the refusal comes before any deletion.
    assert all((upload_root() / f"{f.id}.pdf").exists() for f in files)


def test_a_missing_lesson_is_still_a_404(db):
    with pytest.raises(HTTPException) as err:
        delete_lesson(lesson_id="les_does_not_exist", db=db, _=_boss(db))
    assert err.value.status_code == 404
