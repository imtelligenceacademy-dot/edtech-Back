"""Acting on a selection of lesson PDFs instead of on one row at a time.

The Files page holds hundreds of PDFs. These cover the three things the admin
does to a selection: weigh it, delete it, and take it home as a zip.
"""

from __future__ import annotations

import zipfile

import pytest
from fastapi import HTTPException

from app.models import ChatMessage, Lesson, LessonAssignment, Progress, School, UploadedFile, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.routers.files import bulk_delete, deletion_impact
from app.schemas.file import FileSelection
from app.services.backup import build_files_archive, files_archive_filename
from app.services.file_storage import upload_root
from app.utils import new_id

PDF_BYTES = b"%PDF-1.4\n%fake pdf used only by the tests\n"


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    """Write the fake PDFs to a temp dir, not the developer's storage volume."""
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


def _teacher(db) -> User:
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
        grades=["G7"],
    )
    db.add(user)
    db.commit()
    return user


def _lesson_with_pdf(db, *, grade=7, lesson_no=1, extra_pdfs=0) -> tuple[Lesson, list[UploadedFile]]:
    """A lesson plus the PDF(s) filed under it, bytes written to the upload dir."""
    lesson = Lesson(
        id=new_id("les"),
        title=f"Grade {grade} lesson {lesson_no:02d}",
        grade=grade,
        subject="STEAM",
        language="en",
        year=2,
        lesson_no=lesson_no,
    )
    db.add(lesson)
    db.flush()

    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for copy in range(1 + extra_pdfs):
        file_id = new_id("file")
        (root / f"{file_id}.pdf").write_bytes(PDF_BYTES)
        uploaded = UploadedFile(
            id=file_id,
            filename=f"Grade {grade} Lesson {lesson_no:02d} v{copy}.pdf",
            content_type="application/pdf",
            size_bytes=len(PDF_BYTES),
            storage_path=f"{file_id}.pdf",
            linked_lesson_id=lesson.id,
        )
        db.add(uploaded)
        files.append(uploaded)
    db.commit()
    return lesson, files


# --- 1. Weighing a selection before confirming it -------------------------- #


def test_impact_counts_everything_the_lesson_takes_with_it(db):
    teacher = _teacher(db)
    lesson, files = _lesson_with_pdf(db, extra_pdfs=1)
    db.add(
        LessonAssignment(
            id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
        )
    )
    db.add(
        Progress(
            id=new_id("p"),
            teacher_id=teacher.id,
            lesson_id=lesson.id,
            status=LessonStatus.in_progress,
        )
    )
    db.add(
        ChatMessage(
            id=new_id("cm"),
            teacher_id=teacher.id,
            lesson_id=lesson.id,
            role="user",
            content="how do I explain this?",
        )
    )
    db.commit()

    # The admin picked one PDF; the other copy of the same lesson goes too.
    impact = deletion_impact(
        payload=FileSelection(file_ids=[files[0].id]), db=db, _=_boss(db)
    )

    assert impact.lessons == 1
    assert impact.files == 2
    assert impact.teachers == 1
    assert impact.assignments == 1
    assert impact.progress == 1
    assert impact.chat_messages == 1
    assert impact.lessons_in_progress == 1  # a teacher had already started it
    assert lesson.title in impact.lesson_titles


def test_impact_reports_ids_that_are_already_gone(db):
    impact = deletion_impact(
        payload=FileSelection(file_ids=["file_does_not_exist"]), db=db, _=_boss(db)
    )
    assert impact.missing == 1
    assert impact.lessons == 0


def test_empty_selection_is_rejected(db):
    for endpoint in (deletion_impact, bulk_delete):
        with pytest.raises(HTTPException) as err:
            endpoint(payload=FileSelection(file_ids=[]), db=db, _=_boss(db))
        assert err.value.status_code == 400


# --- 2. Deleting a whole selection in one call ----------------------------- #


def test_bulk_delete_removes_lessons_files_and_their_cascade(db):
    teacher = _teacher(db)
    first, first_files = _lesson_with_pdf(db, lesson_no=1, extra_pdfs=1)
    second, second_files = _lesson_with_pdf(db, lesson_no=2)
    kept, kept_files = _lesson_with_pdf(db, lesson_no=3)
    for lesson in (first, second, kept):
        db.add(
            LessonAssignment(
                id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
            )
        )
        db.add(Progress(id=new_id("p"), teacher_id=teacher.id, lesson_id=lesson.id))
    db.commit()

    result = bulk_delete(
        payload=FileSelection(file_ids=[first_files[0].id, second_files[0].id]),
        db=db,
        _=_boss(db),
    )

    assert result.deleted_lessons == 2
    assert result.deleted_files == 3  # the second copy of the first lesson too
    assert db.get(Lesson, first.id) is None
    assert db.get(Lesson, second.id) is None
    assert db.get(UploadedFile, first_files[1].id) is None
    # The bytes are gone from disk, not just the rows.
    assert not (upload_root() / f"{first_files[0].id}.pdf").exists()
    # Assignments and progress went with the lessons; the untouched one stayed.
    assert (
        db.query(LessonAssignment).filter(LessonAssignment.lesson_id == first.id).count() == 0
    )
    assert db.query(Progress).filter(Progress.lesson_id == kept.id).count() == 1
    assert db.get(UploadedFile, kept_files[0].id) is not None


# --- 3. Zipping a selection ------------------------------------------------ #


def test_archive_includes_only_the_selected_files(db):
    _, wanted = _lesson_with_pdf(db, grade=8, lesson_no=4)
    _, unwanted = _lesson_with_pdf(db, grade=9, lesson_no=5)

    path, included, missing = build_files_archive([wanted[0].id])
    try:
        assert (included, missing) == (1, 0)
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n != "manifest.json"]
        # Language is part of the path, and the grade is zero-padded so a file
        # browser sorts grade-08 before grade-10.
        assert names == [f"year-2/english/grade-08/{wanted[0].filename}"]
        assert all(unwanted[0].filename not in n for n in names)
    finally:
        import os

        os.unlink(path)


def test_archive_of_nothing_is_empty_rather_than_everything(db):
    """An empty id list must not fall back to "the whole curriculum"."""
    _lesson_with_pdf(db, grade=10, lesson_no=6)
    path, included, _missing = build_files_archive([])
    try:
        assert included == 0
    finally:
        import os

        os.unlink(path)


def test_archive_filename_is_named_after_the_selection(db):
    assert files_archive_filename("Year 2 · Grade 7 / EN").startswith(
        "im-telligence-year-2-grade-7-en-"
    )
    assert files_archive_filename(None).startswith("im-telligence-lesson-pdfs-")
