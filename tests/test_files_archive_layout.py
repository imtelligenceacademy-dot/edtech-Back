"""Where each PDF lands inside the downloadable archive.

The layout was year/grade only. A bilingual curriculum stores the same lesson
twice under the same filename, so every English/French pair collided and one of
the two was renamed to `file_<id>_<name>` — which reads as a corrupted duplicate
rather than as the other language, and made the archive look like it had lost
files it had not.
"""

from __future__ import annotations

import zipfile

import pytest

from app.models import Lesson, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.services.backup import _lesson_folder, build_files_archive
from app.services.file_storage import upload_root
from app.utils import new_id


def _lesson(db, *, grade: int, year: int, language: str | None, no: int, filename: str):
    owner = db.query(User).filter(User.role == Role.super_admin).first()
    if owner is None:
        owner = User(
            id=new_id("u"),
            name="Owner",
            email=f"{new_id('e')}@example.com",
            password_hash="x",
            role=Role.super_admin,
            status=UserStatus.active,
            grades=[],
        )
        db.add(owner)
        db.flush()

    lesson = Lesson(
        id=new_id("les"),
        title=filename[:-4],
        grade=grade,
        subject="STEAM",
        language=language,
        year=year,
        lesson_no=no,
        created_by=owner.id,
    )
    db.add(lesson)
    db.flush()

    file_id = new_id("file")
    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{file_id}.pdf").write_bytes(b"%PDF-1.4 test")
    db.add(
        UploadedFile(
            id=file_id,
            filename=filename,
            content_type="application/pdf",
            size_bytes=13,
            storage_path=f"{file_id}.pdf",
            linked_lesson_id=lesson.id,
        )
    )
    db.commit()
    return lesson


def _names(paths: list[str]) -> list[str]:
    return sorted(p for p in paths if p != "manifest.json")


# --- The folder itself ------------------------------------------------------ #


def test_the_path_carries_year_language_and_grade(db):
    lesson = _lesson(db, grade=7, year=2, language="fr", no=1, filename="x.pdf")

    assert _lesson_folder(lesson) == "year-2/french/grade-07"


def test_the_grade_is_padded_so_a_file_browser_sorts_it_right(db):
    small = _lesson(db, grade=3, year=1, language="en", no=1, filename="a.pdf")
    big = _lesson(db, grade=10, year=1, language="en", no=1, filename="b.pdf")

    folders = sorted([_lesson_folder(small), _lesson_folder(big)])

    assert folders == ["year-1/english/grade-03", "year-1/english/grade-10"], (
        "grade-3 unpadded would sort after grade-10"
    )


def test_a_lesson_with_no_language_is_named_rather_than_silently_grouped(db):
    lesson = _lesson(db, grade=5, year=2, language=None, no=1, filename="c.pdf")

    assert _lesson_folder(lesson) == "year-2/language-not-set/grade-05"


# --- The archive ------------------------------------------------------------ #


def test_both_languages_keep_their_real_filename(db, tmp_path):
    """The bug: identical names in one folder forced an id prefix onto one of
    them, so the archive looked like it held a duplicate and a corrupted file."""
    name = "Grade 7 Lesson 01 Sensors.pdf"
    _lesson(db, grade=7, year=2, language="en", no=1, filename=name)
    _lesson(db, grade=7, year=2, language="fr", no=1, filename=name)

    path, _included, _missing = build_files_archive()
    try:
        with zipfile.ZipFile(path) as zf:
            names = _names(zf.namelist())
    finally:
        import os

        os.remove(path)

    # The suite shares one database, so this asserts about the pair under test
    # rather than the whole archive — the counts include every other test's rows.
    assert f"year-2/english/grade-07/{name}" in names
    assert f"year-2/french/grade-07/{name}" in names
    assert not any(n.split("/")[-1].startswith("file_") for n in names), (
        "no file should need the id-prefixed fallback name"
    )
