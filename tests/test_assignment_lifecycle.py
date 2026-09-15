"""What happens to a teacher's curriculum when something around it changes.

The assignment rule is re-applied when a teacher is created or edited, and it
was not re-applied anywhere else — so two ordinary administrative acts left an
account in a state nothing would ever repair on its own, and nothing on any
screen said so.

Suspending an account is one. Both auto-assign paths skip a teacher who is not
active, so every lesson uploaded while she was away passed her by; coming back
"active" gave her the ability to sign in and nothing else, and her track simply
stopped at whatever was current the day she was suspended.

Editing her grades is the other. Stripping an assignment took its progress with
it and left the access request she had open for that lesson standing, pending,
in the super-admin's inbox — where granting it unlocked a lesson she no longer
had.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AccessRequest, Lesson, LessonAssignment, School, User
from app.models.enums import Role, UserStatus
from app.routers import users as users_router
from app.schemas.user import UserStatusUpdate, UserUpdate
from app.services.auto_assign import sync_teacher_assignments
from app.utils import new_id


def _boss(db) -> User:
    user = User(
        id=new_id("u"), name="Owner", email=f"{new_id('e')}@example.com",
        password_hash="x", role=Role.super_admin, status=UserStatus.active, grades=[],
    )
    db.add(user)
    db.flush()
    return user


def _lesson(db, *, grade: int, lesson_no: int) -> Lesson:
    lesson = Lesson(
        id=new_id("les"),
        title=f"Grade {grade} python lesson {lesson_no:02d}",
        grade=grade, subject="STEAM", language="en", year=2,
        course="python", lesson_no=lesson_no,
    )
    db.add(lesson)
    db.flush()
    return lesson


@pytest.fixture()
def world(db):
    school = School(id=new_id("sch"), name="Lifecycle School", program_year=2)
    db.add(school)
    db.flush()
    teacher = User(
        id=new_id("u"), name="Rita", email=f"{new_id('e')}@example.com",
        password_hash="x", role=Role.teacher, status=UserStatus.active,
        school_id=school.id, grades=["G7"], language="en",
    )
    db.add(teacher)
    db.flush()
    before = [_lesson(db, grade=7, lesson_no=n) for n in (1, 2)]
    sync_teacher_assignments(db, teacher)
    db.commit()
    return {"school": school, "teacher": teacher, "admin": _boss(db), "before": before}


def _assigned(db, teacher) -> set[str]:
    return {
        a.lesson_id
        for a in db.scalars(
            select(LessonAssignment).where(LessonAssignment.teacher_id == teacher.id)
        )
    }


def _set_status(db, world, status):
    return users_router.update_status(
        world["teacher"].id, UserStatusUpdate(status=status), db, world["admin"]
    )


# --------------------------------------------------------------------------- #
# Coming back from a suspension
# --------------------------------------------------------------------------- #
def test_reinstating_a_teacher_catches_her_up_on_what_she_missed(db, world):
    """The lessons uploaded while she was away.

    `assign_uploaded_file` skips a suspended account, so nothing linked them to
    her at the time and nothing re-ran the rule afterwards. Her Grade 7 sequence
    ended at whatever was current on the day she was suspended, for good.
    """
    teacher = world["teacher"]
    _set_status(db, world, UserStatus.suspended)

    # A term's uploads arrive while the account is suspended.
    missed = [_lesson(db, grade=7, lesson_no=n) for n in (3, 4)]
    db.commit()
    assert not ({l.id for l in missed} & _assigned(db, teacher)), "precondition"

    _set_status(db, world, UserStatus.active)

    assert {l.id for l in missed} <= _assigned(db, teacher), (
        "she should hold the lessons uploaded while she was away"
    )


def test_suspending_a_teacher_does_not_strip_her_curriculum(db, world):
    """The other direction, so this cannot be satisfied by resyncing on any
    status change. A suspension is temporary and takes nothing away."""
    teacher = world["teacher"]
    before = _assigned(db, teacher)

    _set_status(db, world, UserStatus.suspended)

    assert _assigned(db, teacher) == before


def test_reinstating_a_school_admin_is_left_alone(db, world):
    """Only teachers hold lessons. Running the rule over anybody else is work
    that can only find nothing, on the one screen used to approve accounts."""
    admin = User(
        id=new_id("u"), name="Head", email=f"{new_id('e')}@example.com",
        password_hash="x", role=Role.school_admin, status=UserStatus.suspended,
        school_id=world["school"].id, grades=[],
    )
    db.add(admin)
    db.commit()

    reinstated = users_router.update_status(
        admin.id, UserStatusUpdate(status=UserStatus.active), db, world["admin"]
    )

    assert reinstated.status == UserStatus.active
    assert _assigned(db, admin) == set()


# --------------------------------------------------------------------------- #
# Losing a grade
# --------------------------------------------------------------------------- #
def test_stripping_a_grade_takes_the_pending_request_with_it(db, world):
    """A request to unlock a lesson she no longer has.

    Left pending it kept its place in the super-admin's inbox as though it were
    a decision somebody could make. Granting it found no progress row, wrote a
    fresh one with `unlocked_override`, and reported success — while she saw
    nothing change, because `compute_access` only walks lessons she is assigned.
    """
    teacher, (lesson, _) = world["teacher"], world["before"]
    request = AccessRequest(
        id=new_id("req"), teacher_id=teacher.id, lesson_id=lesson.id,
        section="", status="pending",
    )
    db.add(request)
    db.commit()

    users_router.update_user(teacher.id, UserUpdate(grades=["G9"]), db, world["admin"])
    db.commit()

    assert lesson.id not in _assigned(db, teacher), "precondition: the grade went"
    assert db.get(AccessRequest, request.id) is None, (
        "a request for a lesson she no longer holds is not a decision anybody "
        "can make"
    )


def test_a_resolved_request_is_history_and_stays(db, world):
    """Deliberately narrower than it could be. A granted or denied request
    records a decision somebody actually made, and clearing history is not what
    stripping an assignment is for."""
    teacher, (lesson, _) = world["teacher"], world["before"]
    decided = AccessRequest(
        id=new_id("req"), teacher_id=teacher.id, lesson_id=lesson.id,
        section="", status="granted",
    )
    db.add(decided)
    db.commit()

    users_router.update_user(teacher.id, UserUpdate(grades=["G9"]), db, world["admin"])
    db.commit()

    assert db.get(AccessRequest, decided.id) is not None


def test_a_request_for_a_grade_she_keeps_is_untouched(db, world):
    """The guard on the whole thing: only the stripped lesson's request goes."""
    teacher = world["teacher"]
    kept_lesson = _lesson(db, grade=9, lesson_no=1)
    users_router.update_user(
        teacher.id, UserUpdate(grades=["G7", "G9"]), db, world["admin"]
    )
    db.commit()
    kept = AccessRequest(
        id=new_id("req"), teacher_id=teacher.id, lesson_id=kept_lesson.id,
        section="", status="pending",
    )
    db.add(kept)
    db.commit()

    # Drop G7 only; G9 stays.
    users_router.update_user(teacher.id, UserUpdate(grades=["G9"]), db, world["admin"])
    db.commit()

    assert kept_lesson.id in _assigned(db, teacher), "precondition: G9 is kept"
    assert db.get(AccessRequest, kept.id) is not None
