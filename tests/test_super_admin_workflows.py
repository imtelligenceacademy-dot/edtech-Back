"""The three super-admin workflows that used to make the admin do the system's
job: assigning a lesson, seeing what an upload will do, and being told what
needs attention.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models import Lesson, LessonAssignment, Progress, School, UploadedFile, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.routers.dashboard import stalled_teachers, super_admin_overview
from app.routers.files import preview_upload
from app.routers.lessons import replace_assignments
from app.schemas.file import UploadPreviewRequest
from app.schemas.lesson import AssignmentSet
from app.utils import new_id


def _school(db, name: str = "Test School", program_year: int = 2) -> School:
    school = School(
        id=new_id("sch"), name=name, country="Lebanon", city="Beirut", program_year=program_year
    )
    db.add(school)
    db.commit()
    return school


def _teacher(db, school: School, *, grades=("G7",), language="en", days_old: int = 90) -> User:
    user = User(
        id=new_id("u"),
        name=f"teacher-{new_id('n')}",
        email=f"{new_id('e')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=list(grades),
        language=language,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
    )
    db.add(user)
    db.commit()
    return user


def _lesson(db, *, grade=7, lesson_no=1, language="en", year=2, course=None) -> Lesson:
    lesson = Lesson(
        id=new_id("les"),
        title=f"Grade {grade} lesson {lesson_no:02d} test",
        grade=grade,
        subject="STEAM",
        language=language,
        year=year,
        course=course,
        lesson_no=lesson_no,
    )
    db.add(lesson)
    db.commit()
    return lesson


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


# --- 1. Assignments replace as a set --------------------------------------- #


def test_replacing_assignments_adds_and_removes_in_one_call(db):
    school = _school(db)
    keep = _teacher(db, school)
    drop = _teacher(db, school)
    add = _teacher(db, school)
    lesson = _lesson(db)
    for teacher in (keep, drop):
        db.add(
            LessonAssignment(
                id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
            )
        )
        db.add(Progress(id=new_id("p"), teacher_id=teacher.id, lesson_id=lesson.id))
    db.commit()

    out = replace_assignments(
        lesson_id=lesson.id,
        payload=AssignmentSet(school_id=school.id, teacher_ids=[keep.id, add.id]),
        db=db,
        _=_boss(db),
    )

    assert set(out.assigned_teacher_ids) == {keep.id, add.id}
    # The dropped teacher loses their progress for that lesson with it.
    assert (
        db.query(Progress)
        .filter(Progress.lesson_id == lesson.id, Progress.teacher_id == drop.id)
        .count()
        == 0
    )
    # The added teacher gets a progress row seeded, as the single-assign does.
    assert (
        db.query(Progress)
        .filter(Progress.lesson_id == lesson.id, Progress.teacher_id == add.id)
        .count()
        == 1
    )


def test_replacing_assignments_leaves_other_schools_alone(db):
    ours = _school(db, "Ours")
    theirs = _school(db, "Theirs")
    mine = _teacher(db, ours)
    other = _teacher(db, theirs)
    lesson = _lesson(db)
    for teacher in (mine, other):
        db.add(
            LessonAssignment(
                id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
            )
        )
    db.commit()

    # Clear our school's assignments entirely.
    out = replace_assignments(
        lesson_id=lesson.id,
        payload=AssignmentSet(school_id=ours.id, teacher_ids=[]),
        db=db,
        _=_boss(db),
    )

    assert set(out.assigned_teacher_ids) == {other.id}


def test_replacing_assignments_refuses_a_teacher_from_another_school(db):
    ours = _school(db, "Ours")
    theirs = _school(db, "Theirs")
    outsider = _teacher(db, theirs)
    lesson = _lesson(db)

    with pytest.raises(HTTPException) as exc:
        replace_assignments(
            lesson_id=lesson.id,
            payload=AssignmentSet(school_id=ours.id, teacher_ids=[outsider.id]),
            db=db,
            _=_boss(db),
        )
    assert exc.value.status_code == 400


# --- 2. Upload preview ------------------------------------------------------ #


def test_preview_names_what_each_file_would_do(db):
    school = _school(db, program_year=2)
    match = _teacher(db, school, grades=("G7",), language="en")
    _teacher(db, school, grades=("G9",), language="en")  # wrong grade
    _teacher(db, school, grades=("G7",), language="fr")  # wrong language
    existing = _lesson(db, grade=7, lesson_no=1, course="microbit")

    rows = preview_upload(
        payload=UploadPreviewRequest(
            filenames=[
                "Grade 7 micro:bit lesson 01 name badge.pdf",
                "Grade 7 micro:bit lesson 02 beating heart.pdf",
                "random notes.pdf",
            ],
            language="en",
            year=2,
        ),
        db=db,
        _=_boss(db),
    )

    by_name = {r.filename: r for r in rows}

    already = by_name["Grade 7 micro:bit lesson 01 name badge.pdf"]
    assert already.ok and already.existing_lesson
    assert already.grade == 7 and already.course == "microbit" and already.lesson_no == 1
    assert match.name in already.teacher_names
    assert existing.id  # the preview found it rather than inventing a lesson

    fresh = by_name["Grade 7 micro:bit lesson 02 beating heart.pdf"]
    assert fresh.ok and not fresh.existing_lesson
    assert fresh.teacher_names == already.teacher_names  # same rules, new lesson

    bad = by_name["random notes.pdf"]
    assert not bad.ok and bad.note and "Grade N" in bad.note
    assert bad.teacher_names == []


def test_preview_stores_nothing(db):
    _school(db)
    before = db.query(Lesson).count(), db.query(UploadedFile).count()
    preview_upload(
        payload=UploadPreviewRequest(
            filenames=["Grade 8 python lesson 03 loops.pdf"], language="en", year=2
        ),
        db=db,
        _=_boss(db),
    )
    assert (db.query(Lesson).count(), db.query(UploadedFile).count()) == before


# --- 3. What needs attention ------------------------------------------------ #


def test_overview_flags_the_quiet_teachers_and_unsorted_uploads(db):
    school = _school(db)
    stale = _teacher(db, school)
    active = _teacher(db, school)
    newcomer = _teacher(db, school, days_old=3)  # too new to be behind
    lesson = _lesson(db, lesson_no=5)

    now = datetime.now(timezone.utc)
    db.add(
        Progress(
            id=new_id("p"),
            teacher_id=stale.id,
            lesson_id=lesson.id,
            last_opened_at=now - timedelta(days=20),
        )
    )
    db.add(
        Progress(
            id=new_id("p"),
            teacher_id=active.id,
            lesson_id=lesson.id,
            last_opened_at=now - timedelta(days=1),
        )
    )
    # A file whose name didn't parse: stored, assigned to nobody.
    db.add(
        UploadedFile(
            id=new_id("file"),
            filename="scan001.pdf",
            content_type="application/pdf",
            size_bytes=10,
            storage_path="scan001.pdf",
        )
    )
    db.commit()

    flagged = {t.teacher_id for t in stalled_teachers(db)}
    assert stale.id in flagged
    assert active.id not in flagged
    assert newcomer.id not in flagged  # never opened anything, but only 3 days old

    overview = super_admin_overview(db=db, _=_boss(db))
    assert overview.stalled_teacher_count >= 1
    assert len(overview.stalled_teachers) <= 8  # capped; the count carries the rest
    assert any(f.filename == "scan001.pdf" for f in overview.unsorted_uploads)
    assert overview.unsorted_upload_count >= 1
    assert overview.stalled_after_days == 14
