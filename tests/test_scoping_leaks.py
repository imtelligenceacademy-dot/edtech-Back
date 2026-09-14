"""Things that must not be readable, and addresses that must not be believed.

Each of these was a real hole rather than a hypothetical one, and each is the
kind that comes back quietly: the code that leaks looks like the code that does
not, one filter apart.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from datetime import datetime, timedelta, timezone

from app.models import School, SecurityLog, User
from app.models.enums import Role, SecurityEvent, SecurityStatus, UserStatus
from app.routers import security as security_router
from app.utils import client_ip
from app.config import settings
from app.utils import new_id


@pytest.fixture()
def two_schools(db):
    a = School(id=new_id("sch"), name="A", program_year=2)
    b = School(id=new_id("sch"), name="B", program_year=2)
    db.add_all([a, b])
    db.flush()

    def user(name, role, school_id):
        row = User(
            id=new_id("u"), name=name, email=f"{new_id('e')}@x.com",
            password_hash="x", role=role, status=UserStatus.active,
            school_id=school_id, grades=["G7"] if role == Role.teacher else [],
        )
        db.add(row)
        db.flush()
        return row

    people = {
        "admin": user("Super Admin", Role.super_admin, None),
        "teacher_a": user("Teacher A", Role.teacher, a.id),
        "teacher_b": user("Teacher B", Role.teacher, b.id),
    }
    # Everyone signs in from the same NAT address, which is the ordinary case.
    # Unique per test: the fixture commits, so a fixed address would have its
    # rows counted across every test in the file.
    shared = f"203.0.113.{new_id('x')[-6:]}"
    for person in people.values():
        db.add(SecurityLog(
            id=new_id("sec"), user_id=person.id, user_name=person.name,
            role=person.role, school_id=person.school_id, ip=shared,
            device="Chrome", event=SecurityEvent.normal_login,
            status=SecurityStatus.ok,
        ))
    db.commit()
    return {**people, "school_a": a, "school_b": b, "ip": shared}


def _own_log(db, user) -> SecurityLog:
    return db.query(SecurityLog).filter(SecurityLog.user_id == user.id).first()


def test_a_teacher_opening_their_own_event_learns_nobody_elses_name(db, two_schools):
    """The "who else used this address" panel used to be unscoped.

    On a shared ISP address that handed a teacher the names, sign-in counts and
    failure counts of every account on the platform — including the
    super-admin's and another school's.
    """
    teacher = two_schools["teacher_a"]
    detail = security_router.security_log_detail(
        _own_log(db, teacher).id, db=db, current=teacher
    )
    assert detail.ip_history.users == ["Teacher A"]
    assert detail.ip_history.sign_ins == 1


def test_a_school_admin_sees_only_their_own_school_on_a_shared_address(db, two_schools):
    admin = User(
        id=new_id("u"), name="School Admin", email=f"{new_id('e')}@x.com",
        password_hash="x", role=Role.school_admin, status=UserStatus.active,
        school_id=two_schools["school_a"].id, grades=[],
    )
    db.add(admin)
    db.commit()
    detail = security_router.security_log_detail(
        _own_log(db, two_schools["teacher_a"]).id, db=db, current=admin
    )
    assert detail.ip_history.users == ["Teacher A"]


def test_a_super_admin_still_sees_the_whole_picture(db, two_schools):
    """The scoping must narrow the people it should and nobody else."""
    detail = security_router.security_log_detail(
        _own_log(db, two_schools["teacher_a"]).id,
        db=db, current=two_schools["admin"],
    )
    assert detail.ip_history.users == ["Super Admin", "Teacher A", "Teacher B"]
    assert detail.ip_history.sign_ins == 3


# --------------------------------------------------------------------------- #
# Which address a request is held against
# --------------------------------------------------------------------------- #
class _Req:
    """Enough of a Request for `client_ip`."""

    def __init__(self, peer: str, forwarded: str | None = None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}


def test_a_client_cannot_choose_the_address_it_is_throttled_on(db, monkeypatch):
    """X-Forwarded-For is a list the caller starts and proxies append to.

    Reading its first entry let an attacker hand over a fresh address per
    attempt (bypassing the throttle) or a victim's address repeatedly (banning
    a school). With no trusted proxy configured the header means nothing.
    """
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    assert client_ip(_Req("198.51.100.4", "1.2.3.4")) == "198.51.100.4"
    assert client_ip(_Req("198.51.100.4")) == "198.51.100.4"


def test_behind_one_proxy_the_address_our_proxy_saw_is_the_one_used(db, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    # The caller padded the header; only the last entry was added by our proxy.
    spoofed = "9.9.9.9, 8.8.8.8, 203.0.113.7"
    assert client_ip(_Req("10.0.0.1", spoofed)) == "203.0.113.7"
    # Nothing forwarded at all: the socket peer stands.
    assert client_ip(_Req("10.0.0.1")) == "10.0.0.1"


def test_a_network_ban_lets_go_of_the_network(db, monkeypatch):
    """It used to be permanent: written in one place, cleared in none."""
    from app.models import LoginThrottle
    from app.routers import auth as auth_router

    now = datetime.now(timezone.utc)
    ip = f"198.51.100.{new_id('y')[-6:]}"
    db.add(LoginThrottle(
        ip=ip, blocked_at=now - timedelta(hours=settings.login_ip_block_hours + 1),
        cycle_count=2, reason="Repeated failed login cycles",
    ))
    db.commit()

    # Past its window: lifted rather than enforced forever.
    auth_router._enforce_ip_throttle(db, ip, now)
    throttle = db.get(LoginThrottle, ip)
    assert throttle.blocked_at is None
    assert throttle.cycle_count == 0

    # Still inside it: refused.
    throttle.blocked_at = now - timedelta(minutes=5)
    db.commit()
    with pytest.raises(HTTPException) as excinfo:
        auth_router._enforce_ip_throttle(db, ip, now)
    assert excinfo.value.status_code == 429


# --------------------------------------------------------------------------- #
# Naming a teacher's classes must take all of her history with it
# --------------------------------------------------------------------------- #
def test_naming_classes_moves_chats_and_requests_not_only_progress(db):
    """Progress used to travel alone.

    A teacher pacing one unnamed class has her rows re-keyed onto "A" when a
    super-admin names her classes — but her conversations and any request she
    was waiting on stayed under "", where nothing is keyed any more. Granting
    such a request then unlocked a class she was not in.
    """
    from app.models import AccessRequest, ChatMessage, Lesson, LessonAssignment, Progress
    from app.services.sections import ensure_progress_for_lessons, sync_progress_sections

    school = School(id=new_id("sch"), name="S", program_year=2)
    db.add(school)
    teacher = User(
        id=new_id("u"), name="T", email=f"{new_id('e')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=["G6"], language="en", sections={},
    )
    db.add(teacher)
    db.flush()
    lesson = Lesson(
        id=new_id("les"), title="Grade 6 python lesson 01", grade=6, subject="STEAM",
        language="en", year=2, course="python", lesson_no=1,
    )
    db.add(lesson)
    db.flush()
    db.add(LessonAssignment(
        id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
    ))
    ensure_progress_for_lessons(db, teacher, [lesson], "seed")
    db.add(ChatMessage(
        id=new_id("msg"), teacher_id=teacher.id, lesson_id=lesson.id, section="",
        role="user", content="how do I start this?",
    ))
    db.add(AccessRequest(
        id=new_id("acc"), teacher_id=teacher.id, lesson_id=lesson.id, section="",
        status="pending",
    ))
    db.commit()

    before = dict(teacher.sections or {})
    teacher.sections = {"G6": ["A", "B"]}
    db.flush()
    sync_progress_sections(db, teacher, before, teacher.sections)
    db.commit()

    sections_now = {
        "progress": {p.section for p in db.query(Progress).filter(
            Progress.teacher_id == teacher.id)},
        "chat": {m.section for m in db.query(ChatMessage).filter(
            ChatMessage.teacher_id == teacher.id)},
        "requests": {r.section for r in db.query(AccessRequest).filter(
            AccessRequest.teacher_id == teacher.id)},
    }
    assert sections_now["progress"] == {"A"}
    assert sections_now["chat"] == {"A"}, "her conversation was left behind"
    assert sections_now["requests"] == {"A"}, "the request she is waiting on was left behind"


def test_the_proxy_count_follows_the_deployment(monkeypatch):
    """Too low behind a proxy is an outage, not a nuisance.

    Every request would look like it came from the proxy, so one address would
    carry the whole platform's failed logins and the network ban would lock out
    everybody. Production runs behind exactly one, so that is what it assumes
    unless told otherwise.
    """
    from app.config import Settings

    dev = Settings(environment="development")
    prod = Settings(environment="production", secret_key="x" * 64, cookie_secure=True)
    explicit = Settings(
        environment="production", secret_key="x" * 64, cookie_secure=True,
        trusted_proxy_hops=2,
    )
    assert dev.proxy_hops == 0
    assert prod.proxy_hops == 1
    assert explicit.proxy_hops == 2


# --------------------------------------------------------------------------- #
# Granting access must add access, never remove it
# --------------------------------------------------------------------------- #
def test_reopening_a_finished_lesson_leaves_the_current_one_open(db):
    """An override is an extra door, not a place in the queue.

    Reopening a lesson finished last month used to lock the lesson the teacher
    was part-way through — and the progress endpoint then refused her writes,
    so she could not save her place in the lesson she was actually teaching.
    """
    from datetime import datetime, timedelta, timezone
    from app.models import Lesson, LessonAssignment, Progress
    from app.models.enums import LessonStatus
    from app.services.lesson_access import compute_access

    school = School(id=new_id("sch"), name="S", program_year=2)
    db.add(school)
    teacher = User(
        id=new_id("u"), name="T", email=f"{new_id('e')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=["G8"], language="en", sections={},
    )
    db.add(teacher)
    db.flush()

    lessons = []
    for n in (1, 2, 3):
        les = Lesson(
            id=new_id("les"), title=f"Grade 8 python lesson 0{n}", grade=8,
            subject="STEAM", language="en", year=2, course="python", lesson_no=n,
        )
        db.add(les)
        db.flush()
        db.add(LessonAssignment(
            id=new_id("la"), lesson_id=les.id, teacher_id=teacher.id, source="rule"
        ))
        lessons.append(les)

    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    db.add(Progress(
        id=new_id("pr"), teacher_id=teacher.id, lesson_id=lessons[0].id, section="",
        status=LessonStatus.completed, percent_complete=100, completed_at=long_ago,
        unlocked_override=False,
    ))
    db.add(Progress(
        id=new_id("pr"), teacher_id=teacher.id, lesson_id=lessons[1].id, section="",
        status=LessonStatus.in_progress, percent_complete=40, last_slide=8,
    ))
    db.commit()

    before = compute_access(db, teacher)
    assert before[(lessons[1].id, "")].status == "available"

    # The admin reopens lesson 1 so she can re-teach it.
    reopened = db.query(Progress).filter(
        Progress.lesson_id == lessons[0].id, Progress.teacher_id == teacher.id
    ).one()
    reopened.unlocked_override = True
    db.commit()

    after = compute_access(db, teacher)
    assert after[(lessons[0].id, "")].status == "available", "the reopened lesson"
    assert after[(lessons[1].id, "")].status == "available", (
        "granting access to an old lesson must not take it away from the current one"
    )
    # And it must not hand out the whole rest of the track either.
    assert after[(lessons[2].id, "")].status == "locked"


def test_an_account_below_super_admin_needs_a_school(db):
    """A school-less school-admin is not a harmless half-filled form.

    Their scoping renders as `school_id IS NULL`, which selects the
    super-admins' security events and the entire global curriculum rather than
    nothing at all. Both the create and the edit path refuse it.
    """
    from fastapi import HTTPException
    from app.routers import users as users_router
    from app.schemas.user import UserCreate

    with pytest.raises(HTTPException) as excinfo:
        users_router.create_user(
            UserCreate(
                name="No School", email=f"{new_id('e')}@x.com",
                password="a-long-enough-password", role=Role.school_admin,
                school_id=None,
            ),
            db=db, _=None,
        )
    assert excinfo.value.status_code == 400

    # A super-admin legitimately has none.
    made = users_router.create_user(
        UserCreate(
            name="Boss", email=f"{new_id('e')}@x.com",
            password="a-long-enough-password", role=Role.super_admin,
        ),
        db=db, _=None,
    )
    assert made.school_id is None


def test_classes_can_be_relabelled_around_each_other(db):
    """A to C and B to A is a relabel, not a contradiction.

    Each rename used to be checked against the finished list on its own, so the
    "A" that B had just become looked like the "A" that was supposed to be gone
    — and the whole edit was refused, name and grades with it. Each class's
    history has to land under its own new label, not merge.
    """
    from app.models import Lesson, LessonAssignment, Progress
    from app.models.enums import LessonStatus
    from app.routers import users as users_router
    from app.schemas.user import SectionRename, UserUpdate
    from app.services.sections import ensure_progress_for_lessons, find_progress

    school = School(id=new_id("sch"), name="S", program_year=2)
    db.add(school)
    admin = User(
        id=new_id("u"), name="Admin", email=f"{new_id('e')}@x.com", password_hash="x",
        role=Role.super_admin, status=UserStatus.active,
    )
    db.add(admin)
    teacher = User(
        id=new_id("u"), name="T", email=f"{new_id('e')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=["G6"], language="en", sections={"G6": ["A", "B"]},
    )
    db.add(teacher)
    db.flush()
    lesson = Lesson(
        id=new_id("les"), title="Grade 6 python lesson 01", grade=6, subject="STEAM",
        language="en", year=2, course="python", lesson_no=1,
    )
    db.add(lesson)
    db.flush()
    db.add(LessonAssignment(
        id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
    ))
    ensure_progress_for_lessons(db, teacher, [lesson], "seed")
    db.commit()

    # Tell the two classes apart by how far each has got.
    find_progress(db, teacher.id, lesson.id, "A").percent_complete = 80
    find_progress(db, teacher.id, lesson.id, "B").percent_complete = 20
    db.commit()

    users_router.update_user(
        teacher.id,
        UserUpdate(
            sections={"G6": ["C", "A"]},
            section_renames=[
                SectionRename(grade="G6", from_section="A", to_section="C"),
                SectionRename(grade="G6", from_section="B", to_section="A"),
            ],
        ),
        db,
        admin,
    )
    db.refresh(teacher)

    assert teacher.sections == {"G6": ["C", "A"]}
    # Each class kept its own history rather than merging into one label.
    assert find_progress(db, teacher.id, lesson.id, "C").percent_complete == 80
    assert find_progress(db, teacher.id, lesson.id, "A").percent_complete == 20


# --------------------------------------------------------------------------- #
# A 401 that says "signed out" has to actually sign the browser out
# --------------------------------------------------------------------------- #
def test_a_refused_refresh_clears_the_cookies_it_refused(db):
    """Setting cookies on the injected Response and then raising does nothing.

    Those headers are merged onto the real response only when the handler
    returns, so a dead refresh token came back as a bare 401 and the browser
    went on presenting it — on every 401, for the rest of its seven-day life.
    """
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            response = client.post("/api/auth/refresh")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    cleared = [
        value.decode()
        for key, value in response.headers.raw
        if key.lower() == b"set-cookie"
    ]
    assert len(cleared) == 2, "both the access and the refresh cookie"
    assert all('=""' in c or "=;" in c or "Max-Age=0" in c for c in cleared), cleared
