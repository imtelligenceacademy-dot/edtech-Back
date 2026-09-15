"""Uploads: what they cost this process, and who waits for them.

Two things were wrong with the same handler, and neither showed up in the
response it produced.

The size limit was enforced after `await file.read()` had already built the
whole body as one `bytes`. So the refusal only arrived once the thing being
refused was in memory: a client posting two gigabytes got a 413 that cost this
process two gigabytes to produce, which is a denial of service with a polite
error code on the end of it. Starlette spools past a megabyte to a temporary
file, which bounds the body on disk and not the read of it.

And the handlers were `async def` while doing nothing asynchronous at all — a
disk write, a full scan of users and lessons, a pypdf parse, a commit. On the
event loop that runs with nothing else able to make progress, so a super-admin
uploading a few hundred curriculum PDFs stalled every concurrent teacher
request behind each one, live assistant streams included. The whole platform
paused by one person doing something entirely ordinary, and nothing in any log
to say why.
"""

from __future__ import annotations

import inspect
from io import BytesIO

import pytest

from app.config import settings
from app.routers.fair import upload_fair_project
from app.routers.files import upload_file
from app.services.file_storage import UploadTooLarge, read_upload_capped

PDF = b"%PDF-1.4 " + b"x" * 64


# --------------------------------------------------------------------------- #
# The limit costs a chunk, not a file
# --------------------------------------------------------------------------- #
def test_an_upload_within_the_limit_is_returned_whole():
    body = PDF * 10

    assert read_upload_capped(BytesIO(body), max_bytes=len(body)) == body


def test_an_upload_over_the_limit_is_refused():
    with pytest.raises(UploadTooLarge):
        read_upload_capped(BytesIO(b"x" * 5000), 4999)


def test_it_stops_reading_instead_of_swallowing_the_whole_body():
    """The point of doing this in chunks. A body far over the limit must not be
    pulled into memory in order to be told it is too big."""

    class _Counting(BytesIO):
        def __init__(self, data: bytes):
            super().__init__(data)
            self.read_bytes = 0

        def read(self, size: int = -1) -> bytes:  # type: ignore[override]
            chunk = super().read(size)
            self.read_bytes += len(chunk)
            return chunk

    cap = 1024 * 1024  # one chunk
    stream = _Counting(b"x" * (cap * 8))

    with pytest.raises(UploadTooLarge):
        read_upload_capped(stream, cap)

    assert stream.read_bytes <= cap * 2, (
        f"read {stream.read_bytes} bytes to refuse {cap}; the refusal should cost "
        "about one chunk, not the whole body"
    )


def test_an_upload_exactly_on_the_limit_is_allowed():
    """A limit of N megabytes means N is fine and N plus one byte is not."""
    body = b"x" * 4096

    assert read_upload_capped(BytesIO(body), 4096) == body
    with pytest.raises(UploadTooLarge):
        read_upload_capped(BytesIO(body + b"x"), 4096)


def test_a_stream_already_read_is_rewound_first():
    """Whatever has looked at the body before this must not silently truncate
    it — the size recorded against the file comes from what is returned here."""
    stream = BytesIO(PDF)
    stream.read(5)

    assert read_upload_capped(stream, len(PDF)) == PDF


# --------------------------------------------------------------------------- #
# And nobody else waits for it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("handler", [upload_file, upload_fair_project])
def test_the_upload_handlers_do_not_run_on_the_event_loop(handler):
    """Declared sync, FastAPI runs a handler in a worker thread. Declared async,
    it runs on the loop — and everything these two do blocks it.

    This asserts the declaration rather than the behaviour because the
    declaration *is* the behaviour here: there is nothing else in the function
    that decides it, and `async def` is a single word away at any time.
    """
    assert not inspect.iscoroutinefunction(handler), (
        f"{handler.__name__} is async, so its blocking work runs on the event "
        "loop and every other request waits for it"
    )


# --------------------------------------------------------------------------- #
# The upload still works
# --------------------------------------------------------------------------- #
def test_a_curriculum_pdf_still_uploads_end_to_end(db, tmp_path, monkeypatch):
    """Neither endpoint had a test before this, which is the wrong state for a
    change to how the body is read: the bytes now come off `file.file` rather
    than `await file.read()`, and a mistake there would store a truncated or
    empty PDF while the response still looked perfectly healthy."""
    from starlette.datastructures import UploadFile

    from app.models import User
    from app.models.enums import Role, UserStatus
    from app.routers.files import upload_file as upload
    from app.services.file_storage import upload_root
    from app.utils import new_id

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    boss = User(
        id=new_id("u"), name="Owner", email=f"{new_id('e')}@example.com",
        password_hash="x", role=Role.super_admin, status=UserStatus.active, grades=[],
    )
    db.add(boss)
    db.commit()

    body = b"%PDF-1.4\n" + b"lesson bytes " * 500
    upload_obj = UploadFile(
        filename="Grade 7 python lesson 01 Variables.pdf", file=BytesIO(body)
    )

    result = upload(
        file=upload_obj, language="en", year=2, db=db, current=boss
    )

    assert result.file.size_bytes == len(body), "the whole file, not part of it"
    stored = upload_root() / f"{result.file.id}.pdf"
    assert stored.read_bytes() == body, "what was stored is what was sent"
    assert result.lesson_title, "the lesson was created from the filename"


def test_an_oversized_upload_is_refused_by_the_endpoint(db, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from starlette.datastructures import UploadFile

    from app.models import User
    from app.models.enums import Role, UserStatus
    from app.routers.files import upload_file as upload
    from app.utils import new_id

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    boss = User(
        id=new_id("u"), name="Owner", email=f"{new_id('e')}@example.com",
        password_hash="x", role=Role.super_admin, status=UserStatus.active, grades=[],
    )
    db.add(boss)
    db.commit()

    too_big = b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024)
    upload_obj = UploadFile(filename="Grade 7 python lesson 02 Big.pdf", file=BytesIO(too_big))

    with pytest.raises(HTTPException) as err:
        upload(file=upload_obj, language="en", year=2, db=db, current=boss)

    assert err.value.status_code == 413
    assert list(tmp_path.glob("*.pdf")) == [], "nothing refused should reach disk"
