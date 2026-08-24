"""Vision wiring: correct slide chosen, graceful fallback, feature flag."""

from __future__ import annotations

import pytest

from app.models import Lesson, LessonAssignment, School, UploadedFile, User
from app.models.enums import Role, UserStatus
from app.routers import ai
from app.schemas.ai import AIChatRequest
from app.services.pdf_render import SlideRenderError
from app.utils import new_id


@pytest.fixture()
def lesson_world(db, tmp_path, monkeypatch):
    """A teacher with one available lesson backed by a real 3-page PDF."""
    import pymupdf

    doc = pymupdf.open()
    for n in (1, 2, 3):
        doc.new_page().insert_text((72, 144), f"SLIDE {n}", fontsize=40)
    pdf_path = tmp_path / "lesson.pdf"
    doc.save(str(pdf_path))
    doc.close()

    school = School(id=new_id("sch"), name="S", program_year=2)
    db.add(school)
    teacher = User(
        id=new_id("u"), name="T", email=f"{new_id('t')}@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, school_id=school.id,
        grades=["G7"], language="en",
    )
    db.add(teacher)
    db.flush()

    lesson = Lesson(
        id=new_id("les"), title="Servo basics", grade=7, subject="STEAM",
        language="en", year=2, course="python", lesson_no=1, created_by=teacher.id,
    )
    db.add(lesson)
    db.flush()
    db.add(LessonAssignment(
        id=new_id("la"), lesson_id=lesson.id, teacher_id=teacher.id, source="rule"
    ))
    db.add(UploadedFile(
        id=new_id("file"), filename="lesson.pdf", content_type="application/pdf",
        size_bytes=pdf_path.stat().st_size, storage_path=pdf_path.name,
        uploaded_by=teacher.id, linked_lesson_id=lesson.id,
    ))
    db.commit()

    # Storage resolution and vision-capable provider are stubbed for the test.
    monkeypatch.setattr(
        "app.routers.ai.resolve_stored_file", lambda name: pdf_path
    )
    # pdf_text resolves storage independently when extracting lesson text.
    monkeypatch.setattr(
        "app.services.pdf_text.resolve_stored_file", lambda name: pdf_path
    )
    monkeypatch.setattr(
        "app.routers.ai.get_provider",
        lambda: type("P", (), {"name": "openai", "supports_vision": True})(),
    )
    return {"teacher": teacher, "lesson": lesson, "pdf": pdf_path}


def _ask(db, world, **kw):
    return ai._build_prompt(
        db, world["teacher"],
        AIChatRequest(message="explain this", lessonId=world["lesson"].id, **kw),
    )


def test_requested_slide_is_rendered_and_attached(db, lesson_world):
    bundle = _ask(db, lesson_world, currentSlide=2)
    assert bundle.image_data_url is not None
    assert bundle.image_data_url.startswith("data:image/jpeg;base64,")
    assert "slide 2" in bundle.system.lower()


def test_source_ref_reports_the_slide_actually_inspected(db, lesson_world):
    bundle = _ask(db, lesson_world, currentSlide=3)
    assert bundle.source_ref == "Servo basics - slide 3"


def test_no_slide_means_no_image(db, lesson_world):
    bundle = _ask(db, lesson_world)
    assert bundle.image_data_url is None
    assert bundle.source_ref == "Servo basics"
    assert "only have the extracted text" in bundle.system.lower()


def test_out_of_range_slide_falls_back_to_text(db, lesson_world):
    """Page 99 of a 3-page deck: answer still happens, visually honest."""
    bundle = _ask(db, lesson_world, currentSlide=99)
    assert bundle.image_data_url is None
    assert bundle.grounded is True          # still answers from text
    assert "could not be inspected" in bundle.system.lower()


def test_render_failure_falls_back_to_text(db, lesson_world, monkeypatch):
    def boom(*a, **k):
        raise SlideRenderError("simulated failure")

    monkeypatch.setattr(ai, "render_page_data_url", boom)
    bundle = _ask(db, lesson_world, currentSlide=1)
    assert bundle.image_data_url is None
    assert bundle.grounded is True
    assert "could not be inspected" in bundle.system.lower()


def test_feature_flag_disables_vision(db, lesson_world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_teacher_vision_enabled", False)
    bundle = _ask(db, lesson_world, currentSlide=1)
    assert bundle.image_data_url is None
    # Flag off is not a failure - don't tell the teacher a check failed.
    assert "could not be inspected" not in bundle.system.lower()


def test_text_only_provider_skips_rendering(db, lesson_world, monkeypatch):
    monkeypatch.setattr(
        "app.routers.ai.get_provider",
        lambda: type("P", (), {"name": "groq", "supports_vision": False})(),
    )
    called = []
    monkeypatch.setattr(
        ai, "render_page_data_url", lambda *a, **k: called.append(a) or "x"
    )
    bundle = _ask(db, lesson_world, currentSlide=1)
    assert bundle.image_data_url is None
    assert called == [], "must not render for a text-only provider"


def test_lesson_text_is_included_as_grounding(db, lesson_world):
    bundle = _ask(db, lesson_world, currentSlide=1)
    assert "SLIDE 1" in bundle.system  # extracted PDF text
    assert "Servo basics" in bundle.system
