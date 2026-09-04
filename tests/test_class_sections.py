"""One teacher, several classes of the same grade.

The bug these exist to prevent: progress used to be unique on (teacher, lesson),
so a teacher who takes 6A, 6B, 6C and 6D through the same curriculum shared one
row between them. Marking a lesson complete after teaching 6A locked it, leaving
her unable to reopen material she still had to teach three more times, and
started the next lesson's countdown for classes that had not had this one.

Teachers with a single class have one unnamed section (""), and the rest of the
suite covers them; what is checked here is that naming classes changes their
behaviour not at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from app.models import AccessRequest, Lesson, LessonAssignment, Progress, School, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.routers import access_requests as ar_router
from app.routers import lessons as lessons_router
from app.routers import progress as progress_router
from app.routers import users as users_router
from app.schemas.access_request import AccessRequestCreate
from app.schemas.progress import ProgressUpdate
from app.schemas.user import UserUpdate
from app.services.lesson_access import compute_access, is_lesson_available
from app.services.sections import (
    ensure_progress_for_lessons,
    normalize_sections,
    sections_for,
    sync_progress_sections,
)
from app.utils import new_id


def _teacher(db, school, grades, sections=None):
    user = User(
        id=new_id("u"),
        name="T",
        email=f"{new_id('t')}@x.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=grades,
        sections=sections or {},
        language="en",
    )
    db.add(user)
    db.flush()
    return user


def _lessons(db, teacher, grade, count):
    out = []
    for n in range(1, count + 1):
        lesson = Lesson(
            id=new_id("les"),
            title=f"Grade {grade} python lesson 0{n}",
            grade=grade,
            subject="STEAM",
            language="en",
            year=2,
            course="python",
            lesson_no=n,
            created_by=teacher.id,
        )
        db.add(lesson)
        db.flush()
        db.add(
            LessonAssignment(
                id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
            )
        )
        out.append(lesson)
    return out


@pytest.fixture()
def world(db):
    """A teacher taking Grade 6 twice — 6A and 6B — through three lessons."""
    school = School(id=new_id("sch"), name="S", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G6"], {"G6": ["A", "B"]})
    lessons = _lessons(db, teacher, 6, 3)
    ensure_progress_for_lessons(db, teacher, lessons)
    db.commit()
    return {"school": school, "teacher": teacher, "lessons": lessons}


def _rows(db, teacher):
    return list(
        db.scalars(select(Progress).where(Progress.teacher_id == teacher.id))
    )


def _complete(db, teacher, lesson, section, when=None):
    db.flush()
    row = db.scalar(
        select(Progress).where(
            Progress.teacher_id == teacher.id,
            Progress.lesson_id == lesson.id,
            Progress.section == section,
        )
    )
    assert row is not None, f"no progress row for section {section!r}"
    row.status = LessonStatus.completed
    row.percent_complete = 100
    row.completed_at = when or datetime.now(timezone.utc)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# The bug
# --------------------------------------------------------------------------- #
def test_finishing_a_lesson_with_one_class_leaves_it_open_for_the_other(db, world):
    teacher, (l1, _, _) = world["teacher"], world["lessons"]
    _complete(db, teacher, l1, "A")

    access = compute_access(db, teacher)
    assert access[(l1.id, "A")].status == "completed"
    # The whole point: 6B has not had this lesson, so it is still theirs to teach.
    assert access[(l1.id, "B")].status == "available"


def test_the_pdf_and_assistant_stay_open_while_any_class_still_needs_it(db, world):
    teacher, (l1, _, _) = world["teacher"], world["lessons"]
    _complete(db, teacher, l1, "A")

    # No section named = "in any of her classes?", which is what the viewer and
    # the assistant ask. She must be able to open material she still has to teach.
    assert is_lesson_available(db, teacher, l1.id) is True
    assert is_lesson_available(db, teacher, l1.id, "A") is False
    assert is_lesson_available(db, teacher, l1.id, "B") is True


def test_each_class_counts_down_to_the_next_lesson_on_its_own(db, world):
    teacher, (l1, l2, _) = world["teacher"], world["lessons"]
    _complete(db, teacher, l1, "A")

    access = compute_access(db, teacher)
    # 6A finished lesson 1 just now, so lesson 2 is waiting out the gap.
    assert access[(l2.id, "A")].status == "waiting"
    # 6B has not finished lesson 1, so lesson 2 is not theirs yet either — but
    # for a different reason, and it must not inherit 6A's countdown.
    assert access[(l2.id, "B")].status == "locked"


def test_a_class_that_waited_moves_on_alone(db, world):
    teacher, (l1, l2, _) = world["teacher"], world["lessons"]
    _complete(db, teacher, l1, "A", when=datetime.now(timezone.utc) - timedelta(days=8))

    access = compute_access(db, teacher)
    assert access[(l2.id, "A")].status == "available"
    assert access[(l2.id, "B")].status == "locked"


# --------------------------------------------------------------------------- #
# Recording progress
# --------------------------------------------------------------------------- #
def test_progress_is_recorded_against_the_class_being_taught(db, world):
    teacher, (l1, _, _) = world["teacher"], world["lessons"]

    progress_router.update_progress(
        l1.id, ProgressUpdate(slide=4, total=10, section="B"), db, teacher
    )

    rows = {
        p.section: p
        for p in db.scalars(
            select(Progress).where(
                Progress.teacher_id == teacher.id, Progress.lesson_id == l1.id
            )
        )
    }
    assert rows["B"].last_slide == 4
    # 6A was not in the room.
    assert rows["A"].last_slide is None
    assert rows["A"].status == LessonStatus.not_started


def test_a_teacher_cannot_record_progress_for_a_class_that_is_not_theirs(db, world):
    teacher, (l1, _, _) = world["teacher"], world["lessons"]

    with pytest.raises(HTTPException) as raised:
        progress_router.update_progress(
            l1.id, ProgressUpdate(slide=4, total=10, section="Z"), db, teacher
        )
    assert raised.value.status_code == 400


def test_a_client_that_sends_no_class_falls_back_to_the_first(db, world):
    """Older clients, and every single-class teacher, send no section at all."""
    teacher, (l1, _, _) = world["teacher"], world["lessons"]

    progress_router.update_progress(l1.id, ProgressUpdate(slide=2, total=10), db, teacher)

    row = db.scalar(
        select(Progress).where(
            Progress.teacher_id == teacher.id,
            Progress.lesson_id == l1.id,
            Progress.section == "A",
        )
    )
    assert row.last_slide == 2


def test_a_single_class_teacher_keeps_the_unnamed_section(db):
    school = School(id=new_id("sch"), name="S2", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G7"])
    lessons = _lessons(db, teacher, 7, 2)
    ensure_progress_for_lessons(db, teacher, lessons)
    db.commit()

    assert sections_for(teacher, 7) == [""]
    rows = _rows(db, teacher)
    assert len(rows) == 2
    assert {r.section for r in rows} == {""}

    access = compute_access(db, teacher)
    assert access[(lessons[0].id, "")].status == "available"
    assert access[(lessons[1].id, "")].status == "locked"


def test_assigning_a_lesson_creates_a_row_for_every_class(db):
    school = School(id=new_id("sch"), name="S3", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G6"], {"G6": ["A", "B", "C", "D"]})
    lessons = _lessons(db, teacher, 6, 2)
    created = ensure_progress_for_lessons(db, teacher, lessons)
    db.commit()

    assert created == 8  # two lessons, four classes
    # Running it again is a no-op rather than a duplicate.
    assert ensure_progress_for_lessons(db, teacher, lessons) == 0


# --------------------------------------------------------------------------- #
# Editing a teacher's classes
# --------------------------------------------------------------------------- #
def test_naming_classes_adopts_existing_progress_into_the_first(db):
    """The teacher was pacing one class; that class becomes 6A. Without this,
    naming sections mid-year would hide a term of progress."""
    school = School(id=new_id("sch"), name="S4", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G6"])
    lessons = _lessons(db, teacher, 6, 2)
    ensure_progress_for_lessons(db, teacher, lessons)
    _complete(db, teacher, lessons[0], "")

    teacher.sections = {"G6": ["A", "B"]}
    sync_progress_sections(db, teacher, {}, teacher.sections)
    db.commit()

    rows = {(p.lesson_id, p.section): p for p in _rows(db, teacher)}
    assert (lessons[0].id, "A") in rows
    assert rows[(lessons[0].id, "A")].status == LessonStatus.completed
    assert (lessons[0].id, "") not in rows


def test_clearing_classes_returns_progress_to_the_unnamed_one(db):
    school = School(id=new_id("sch"), name="S5", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G6"], {"G6": ["A", "B"]})
    lessons = _lessons(db, teacher, 6, 1)
    ensure_progress_for_lessons(db, teacher, lessons)
    _complete(db, teacher, lessons[0], "A")

    before = dict(teacher.sections)
    teacher.sections = {}
    sync_progress_sections(db, teacher, before, {})
    db.commit()

    sections = {p.section: p.status for p in _rows(db, teacher)}
    assert sections[""] == LessonStatus.completed
    # 6B's untouched row is left where it was rather than deleted.
    assert "B" in sections


def test_removing_one_class_keeps_its_progress_for_when_it_returns(db):
    school = School(id=new_id("sch"), name="S6", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G6"], {"G6": ["A", "B"]})
    lessons = _lessons(db, teacher, 6, 1)
    ensure_progress_for_lessons(db, teacher, lessons)
    _complete(db, teacher, lessons[0], "B")

    before = dict(teacher.sections)
    teacher.sections = {"G6": ["A"]}
    sync_progress_sections(db, teacher, before, teacher.sections)
    db.commit()

    # Invisible while no class carries the label, but intact.
    kept = db.scalar(
        select(Progress).where(
            Progress.teacher_id == teacher.id, Progress.section == "B"
        )
    )
    assert kept is not None and kept.status == LessonStatus.completed
    assert compute_access(db, teacher).get((lessons[0].id, "B")) is None


def test_editing_an_account_normalizes_and_syncs(db):
    school = School(id=new_id("sch"), name="S7", program_year=2)
    db.add(school)
    db.flush()
    admin = User(
        id=new_id("u"), name="A", email=f"{new_id('a')}@x.com", password_hash="x",
        role=Role.super_admin, status=UserStatus.active,
    )
    db.add(admin)
    teacher = _teacher(db, school, ["G6"])
    db.commit()

    users_router.update_user(
        teacher.id,
        UserUpdate(grades=["G6"], sections={"G6": ["a", "A", " B ", ""], "G9": ["X"]}),
        db,
        admin,
    )
    db.refresh(teacher)

    # "a"/"A" are one class, blanks are dropped, and G9 is not a grade she teaches.
    assert teacher.sections == {"G6": ["a", "B"]}


def test_dropping_a_grade_takes_its_classes_with_it(db):
    school = School(id=new_id("sch"), name="S8", program_year=2)
    db.add(school)
    db.flush()
    admin = User(
        id=new_id("u"), name="A", email=f"{new_id('a')}@x.com", password_hash="x",
        role=Role.super_admin, status=UserStatus.active,
    )
    db.add(admin)
    teacher = _teacher(db, school, ["G5", "G6"], {"G5": ["A"], "G6": ["A", "B"]})
    db.commit()

    users_router.update_user(teacher.id, UserUpdate(grades=["G6"]), db, admin)
    db.refresh(teacher)

    assert teacher.sections == {"G6": ["A", "B"]}


def test_normalize_sections_bounds_what_an_admin_can_type():
    assert normalize_sections({"G6": ["A"] * 40}, ["G6"])["G6"] == ["A"]
    assert normalize_sections({"G6": [str(i) for i in range(30)]}, ["G6"])["G6"] == [
        str(i) for i in range(12)
    ]
    assert normalize_sections({"G6": ["x" * 50]}, ["G6"])["G6"] == ["x" * 16]
    assert normalize_sections(None, ["G6"]) == {}


# --------------------------------------------------------------------------- #
# Access requests
# --------------------------------------------------------------------------- #
def test_an_access_request_unlocks_only_the_class_that_asked(db, world):
    teacher, (l1, l2, _) = world["teacher"], world["lessons"]
    admin = User(
        id=new_id("u"), name="A", email=f"{new_id('a')}@x.com", password_hash="x",
        role=Role.super_admin, status=UserStatus.active,
    )
    db.add(admin)
    db.commit()

    # 6B is stuck on lesson 2 behind lesson 1.
    req = ar_router.create_request(
        AccessRequestCreate(lesson_id=l2.id, section="B"),
        BackgroundTasks(),
        db,
        teacher,
    )
    assert req.section == "B"

    stored = db.get(AccessRequest, req.id)
    ar_router._resolve(db, stored.id, admin, granted=True)

    access = compute_access(db, teacher)
    assert access[(l2.id, "B")].status == "available"
    # 6A never asked, and is not carried along.
    assert access[(l2.id, "A")].status == "locked"


def test_two_classes_stuck_on_one_lesson_are_two_requests(db, world):
    teacher, (_, l2, _) = world["teacher"], world["lessons"]

    first = ar_router.create_request(
        AccessRequestCreate(lesson_id=l2.id, section="A"), BackgroundTasks(), db, teacher
    )
    second = ar_router.create_request(
        AccessRequestCreate(lesson_id=l2.id, section="B"), BackgroundTasks(), db, teacher
    )
    assert first.id != second.id

    # Asking twice for the same class reuses the pending one.
    again = ar_router.create_request(
        AccessRequestCreate(lesson_id=l2.id, section="A"), BackgroundTasks(), db, teacher
    )
    assert again.id == first.id


# --------------------------------------------------------------------------- #
# What the teacher's pickers are told
# --------------------------------------------------------------------------- #
def test_my_classes_reports_each_class_separately(db, world):
    teacher, (l1, l2, _) = world["teacher"], world["lessons"]
    _complete(db, teacher, l1, "A", when=datetime.now(timezone.utc) - timedelta(days=8))

    rows = {r.section: r for r in lessons_router.my_classes(db, teacher)}

    assert set(rows) == {"A", "B"}
    assert rows["A"].total == 3 and rows["B"].total == 3
    # 6A finished lesson 1 and waited out the gap; lesson 2 is theirs now.
    assert rows["A"].completed == 1
    assert rows["A"].next_lesson_id == l2.id
    assert rows["A"].next_status == "available"
    # 6B has not started, and is still on lesson 1.
    assert rows["B"].completed == 0
    assert rows["B"].next_lesson_id == l1.id
    assert rows["B"].next_status == "available"


def test_my_classes_gives_a_single_class_teacher_one_row_per_grade(db):
    school = School(id=new_id("sch"), name="S9", program_year=2)
    db.add(school)
    db.flush()
    teacher = _teacher(db, school, ["G7"])
    lessons = _lessons(db, teacher, 7, 2)
    ensure_progress_for_lessons(db, teacher, lessons)
    db.commit()

    rows = lessons_router.my_classes(db, teacher)

    assert len(rows) == 1
    assert rows[0].section == ""
    assert rows[0].grade == 7 and rows[0].total == 2


def test_the_lesson_list_is_scoped_to_the_class_asked_about(db, world):
    teacher, (l1, _, _) = world["teacher"], world["lessons"]
    _complete(db, teacher, l1, "A")

    for_a = {l.id: l for l in lessons_router.list_lessons("A", db, teacher)}
    for_b = {l.id: l for l in lessons_router.list_lessons("B", db, teacher)}

    assert for_a[l1.id].access_status == "completed"
    assert for_b[l1.id].access_status == "available"


def test_asking_for_a_class_that_is_not_theirs_falls_back_to_their_first(db, world):
    """A stale bookmark must not be able to read another class into existence."""
    teacher, (l1, _, _) = world["teacher"], world["lessons"]
    _complete(db, teacher, l1, "A")

    rows = {l.id: l for l in lessons_router.list_lessons("Z", db, teacher)}
    assert rows[l1.id].access_status == "completed"  # 6A, their first class
