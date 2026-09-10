"""The reports build, and say what they should say.

Nothing in the suite used to open a report. Trimming two columns out of them
broke both builders — a stale `LessonStatus.late` in the sort key, and a stale
`stats.late` in the platform table — and every test still passed, because the
only thing that ever calls these is a route nobody exercises.

So this renders both documents and reads the text back out. It is a slow test by
this suite's standards and worth it: a report that raises is a report the admin
finds broken at the moment they need it, and one that quietly keeps a column is
a promise to a school that we removed something we did not.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from app.models import Lesson, LessonAssignment, Progress, School, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.services.report_docx import build_school_ai_report, build_super_ai_report
from app.services.sections import ensure_progress_for_lessons
from app.utils import new_id


def _text(buf: io.BytesIO) -> str:
    """The document's visible words, tags stripped."""
    xml = zipfile.ZipFile(io.BytesIO(buf.getvalue())).read("word/document.xml")
    return re.sub(r"<[^>]+>", " ", xml.decode("utf-8"))


@pytest.fixture()
def school_with_work(db):
    """A school with a teacher part-way through a curriculum, so the tables have
    rows rather than being empty and trivially passing."""
    school = School(id=new_id("sch"), name="Reported", program_year=2, city="Beirut")
    db.add(school)
    db.flush()

    admin = User(
        id=new_id("u"), name="Admin", email=f"{new_id('a')}@x.com", password_hash="x",
        role=Role.school_admin, status=UserStatus.active, school_id=school.id, grades=[],
    )
    teacher = User(
        id=new_id("u"), name="Teacher", email=f"{new_id('t')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=["G6"], sections={"G6": ["A", "B"]}, language="en",
    )
    db.add_all([admin, teacher])
    db.flush()

    lessons = []
    for n in (1, 2):
        lesson = Lesson(
            id=new_id("les"), title=f"Grade 6 python lesson 0{n}", grade=6,
            subject="STEAM", language="en", year=2, course="python", lesson_no=n,
        )
        db.add(lesson)
        db.flush()
        db.add(LessonAssignment(
            id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
        ))
        lessons.append(lesson)
    ensure_progress_for_lessons(db, teacher, lessons)
    db.flush()

    first = db.query(Progress).filter(Progress.teacher_id == teacher.id).first()
    first.status = LessonStatus.completed
    first.percent_complete = 100
    db.commit()
    return {"school": school, "admin": admin, "teacher": teacher}


def test_the_school_report_builds(school_with_work, db):
    buf, name = build_school_ai_report(
        db, school_with_work["school"].id, school_with_work["admin"].name, "Narrative."
    )
    assert name.endswith(".docx")
    assert len(buf.getvalue()) > 5_000


def test_the_platform_report_builds(school_with_work, db):
    buf, name = build_super_ai_report(db, "Owner", "Narrative.")
    assert name.endswith(".docx")
    assert len(buf.getvalue()) > 5_000


def test_neither_report_calls_anybody_late(school_with_work, db):
    """A teacher is where their classes are. Nothing in the curriculum is late,
    and the word should not appear in a document a school reads."""
    school, _ = build_school_ai_report(
        db, school_with_work["school"].id, school_with_work["admin"].name, "Narrative."
    )
    platform, _ = build_super_ai_report(db, "Owner", "Narrative.")

    assert "late" not in _text(school).lower()
    assert "late" not in _text(platform).lower()


def test_the_school_report_does_not_show_ai_usage(school_with_work, db):
    """How many questions a teacher asked reads as monitoring rather than
    support, and is not the school admin's to act on."""
    text = _text(
        build_school_ai_report(
            db, school_with_work["school"].id, school_with_work["admin"].name, "N."
        )[0]
    )

    assert "AI assistant usage" not in text
    assert "AI questions" not in text
    assert "Teachers" in text, "the teachers table itself stays"


def test_the_platform_report_still_shows_ai_usage(school_with_work, db):
    """The platform owner pays for it and decides about it, so they still see
    it. Removing it from the school report was about who it is shown to."""
    assert "AI questions" in _text(build_super_ai_report(db, "Owner", "N.")[0])
