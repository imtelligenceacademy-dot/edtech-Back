"""Serving a lesson PDF to the reader without a download manager stealing it.

A teacher opened a Grade 1 lesson in Edge and got a red icon and "Failed to
fetch". Nothing was wrong with the file or with her account: Internet Download
Manager's extension had claimed the request and cancelled the page's own, so the
reader's ``fetch`` rejected before a byte arrived.

IDM matches on two things, and the old route handed it both — a URL ending in
``/download`` and a ``Content-Disposition`` header. ``/view`` exists to show the
same bytes with neither. ``/download`` keeps its filename, because the admin
file list offers it as a real download and the saved file should be named.

These go through the HTTP layer rather than calling the route functions, because
the whole point is a response header, and a header is only real once Starlette
has written it.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Lesson, School, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.services.file_storage import upload_root
from app.utils import new_id

PDF_BYTES = b"%PDF-1.4\n%fake pdf used only by the tests\n"


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    """Write the fake PDF to a temp dir, not the developer's storage volume."""
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "files"))


@pytest.fixture()
def client(db):
    """The real app, with the session and the signed-in user supplied.

    Request it before any fixture that writes: starting the app runs the
    migrations, and that DDL deadlocks against an open write transaction.

    The route is handed this same session, so the helpers below only flush.
    Committing would work too, and would also outlive the test: the suite shares
    one database that is never truncated, and a stray Grade 1 lesson left behind
    here locks the next lesson in that track for a test three modules away.
    """
    holder: dict[str, User] = {}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    try:
        with TestClient(app) as c:
            yield c, holder
    finally:
        app.dependency_overrides.clear()


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
    db.flush()
    return user


def _unassigned_teacher(db) -> User:
    """A real teacher with no claim on the lesson below."""
    school = School(id=new_id("sch"), name="S", country="Lebanon", city="Beirut", program_year=2)
    db.add(school)
    user = User(
        id=new_id("u"),
        name="teacher",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G1"],
    )
    db.add(user)
    db.flush()
    return user


def _lesson_pdf(db) -> UploadedFile:
    lesson = Lesson(
        id=new_id("les"),
        title="grade 1 microbit lesson 01 name badge",
        grade=1,
        subject="STEAM",
        language="en",
        year=2,
        lesson_no=1,
    )
    db.add(lesson)
    db.flush()

    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    file_id = new_id("file")
    (root / f"{file_id}.pdf").write_bytes(PDF_BYTES)
    uploaded = UploadedFile(
        id=file_id,
        # The name IDM offered to save, taken from the screenshot that started this.
        filename="grade 1 microbit lesson 01 name badge",
        content_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        storage_path=f"{file_id}.pdf",
        linked_lesson_id=lesson.id,
    )
    db.add(uploaded)
    db.flush()
    return uploaded


def test_the_reader_route_sends_no_content_disposition(client, db):
    """The header IDM reads must not be on the response the reader asks for.

    This is the assertion the fix rests on. ``FileResponse`` adds
    ``Content-Disposition`` when it is given a ``filename``, and ``/view`` gives
    it none — but that is Starlette's behaviour, not ours, so pin it here rather
    than trust it.
    """
    c, holder = client
    holder["user"] = _boss(db)
    uploaded = _lesson_pdf(db)

    response = c.get(f"/api/files/{uploaded.id}/view")

    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert response.headers["content-type"] == "application/pdf"
    assert "content-disposition" not in response.headers, (
        "the reader route grew a disposition header again; "
        "download managers claim the request and the lesson fails to fetch"
    )


def test_the_download_route_still_names_the_file(client, db):
    """The other half. An admin saving a PDF should get a named file."""
    c, holder = client
    holder["user"] = _boss(db)
    uploaded = _lesson_pdf(db)

    response = c.get(f"/api/files/{uploaded.id}/download")

    assert response.status_code == 200
    assert response.content == PDF_BYTES
    disposition = response.headers["content-disposition"]
    assert "inline" in disposition
    # Starlette percent-encodes into the RFC 5987 ``filename*=utf-8''…`` form
    # whenever the name is not a bare token, and these names have spaces in
    # them, so compare the decoded value rather than the literal header.
    assert uploaded.filename in unquote(disposition)


def test_the_reader_route_refuses_a_teacher_who_cannot_open_the_file(client, db):
    """The new route shares ``/download``'s permission check, and must keep it.

    A second route serving the same bytes is a second place for the check to go
    missing, which is why both read it out of ``_readable_file``. This asks the
    endpoint whether that is still true.
    """
    c, holder = client
    holder["user"] = _unassigned_teacher(db)
    uploaded = _lesson_pdf(db)

    response = c.get(f"/api/files/{uploaded.id}/view")

    assert response.status_code == 403
