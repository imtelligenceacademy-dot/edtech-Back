"""Reading a slide with one model so a text-only model can answer about it.

The important properties are the cheap ones: read each slide once, never on
tracks whose code is already text, and never let a vision failure reach the
teacher.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import Lesson, SlideReading, UploadedFile
from app.services import slide_vision
from app.utils import new_id

PNG = "data:image/jpeg;base64,AAAA"
READING = "on start:\n  set servo P0 to 90\nforever:\n  if button A pressed:\n    pause 1000"


@pytest.fixture()
def vision_on(monkeypatch):
    monkeypatch.setattr(settings, "gemini_vision_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_vision_model", "gemini-test")


def _lesson(db, *, year: int, grade: int) -> Lesson:
    lesson = Lesson(
        id=new_id("les"),
        title=f"Y{year} G{grade}",
        grade=grade,
        subject="STEAM",
        language="en",
        year=year,
    )
    db.add(lesson)
    db.commit()
    return lesson


def _file(db) -> UploadedFile:
    uploaded = UploadedFile(
        id=new_id("file"),
        filename="lesson.pdf",
        content_type="application/pdf",
        size_bytes=1,
        storage_path=f"{new_id('f')}.pdf",
    )
    db.add(uploaded)
    db.commit()
    return uploaded


# --- Which lessons are worth reading --------------------------------------- #


@pytest.mark.parametrize(
    "year,grade,expected",
    [
        (1, 1, True),
        (1, 7, True),
        (1, 12, True),   # Year 1 is block coding all the way up
        (2, 1, True),
        (2, 6, True),    # the configured boundary, inclusive
        (2, 7, False),   # Year 2 turns to Python here: the PDF text layer has it
        (2, 12, False),
    ],
)
def test_only_the_tracks_where_the_code_is_a_picture(db, year, grade, expected):
    assert slide_vision.applies_to(_lesson(db, year=year, grade=grade)) is expected


def test_no_lesson_is_not_a_lesson_to_read(db):
    assert slide_vision.applies_to(None) is False


# --- Read once, keep it ----------------------------------------------------- #


def test_a_slide_is_read_once_and_then_remembered(db, vision_on, monkeypatch):
    """Without the cache every question about slide 4 would pay to read slide 4
    again."""
    uploaded = _file(db)
    calls = []

    monkeypatch.setattr(slide_vision, "render_page_data_url", lambda path, page: PNG)
    monkeypatch.setattr(slide_vision, "resolve_stored_file", lambda p: "/tmp/x.pdf")
    monkeypatch.setattr(slide_vision, "_ask", lambda url: calls.append(url) or READING)

    first = slide_vision.read_slide(db, uploaded=uploaded, page=4)
    second = slide_vision.read_slide(db, uploaded=uploaded, page=4)

    assert first == READING
    assert second == READING
    assert len(calls) == 1  # the second answer came from the row
    assert db.query(SlideReading).filter(SlideReading.file_id == uploaded.id).count() == 1


def test_each_slide_is_its_own_reading(db, vision_on, monkeypatch):
    uploaded = _file(db)
    monkeypatch.setattr(slide_vision, "render_page_data_url", lambda path, page: PNG)
    monkeypatch.setattr(slide_vision, "resolve_stored_file", lambda p: "/tmp/x.pdf")
    monkeypatch.setattr(slide_vision, "_ask", lambda url: f"slide text {len(url)}")

    slide_vision.read_slide(db, uploaded=uploaded, page=4)
    slide_vision.read_slide(db, uploaded=uploaded, page=7)

    assert db.query(SlideReading).filter(SlideReading.file_id == uploaded.id).count() == 2


def test_the_model_that_read_it_is_recorded(db, vision_on, monkeypatch):
    uploaded = _file(db)
    monkeypatch.setattr(slide_vision, "render_page_data_url", lambda path, page: PNG)
    monkeypatch.setattr(slide_vision, "resolve_stored_file", lambda p: "/tmp/x.pdf")
    monkeypatch.setattr(slide_vision, "_ask", lambda url: READING)

    slide_vision.read_slide(db, uploaded=uploaded, page=1)
    row = db.query(SlideReading).filter(SlideReading.file_id == uploaded.id).one()

    assert row.model == "gemini-test"


# --- Nothing here may ever reach the teacher -------------------------------- #


def test_a_dead_provider_costs_the_teacher_nothing(db, vision_on, monkeypatch):
    uploaded = _file(db)
    monkeypatch.setattr(slide_vision, "render_page_data_url", lambda path, page: PNG)
    monkeypatch.setattr(slide_vision, "resolve_stored_file", lambda p: "/tmp/x.pdf")

    # _ask swallows its own errors and reports "nothing" — see the test below.
    monkeypatch.setattr(slide_vision, "_ask", lambda url: None)

    assert slide_vision.read_slide(db, uploaded=uploaded, page=4) is None
    # And nothing was cached, so a later working call still gets its chance.
    assert db.query(SlideReading).filter(SlideReading.file_id == uploaded.id).count() == 0


def test_the_ask_itself_swallows_provider_errors(monkeypatch):
    """_ask is the only place that touches the network; it must not raise."""
    import httpx

    def explode(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", explode)
    assert slide_vision._ask(PNG) is None


def test_an_unrenderable_page_is_not_a_failure(db, vision_on, monkeypatch):
    from app.services.pdf_render import SlideRenderError

    uploaded = _file(db)
    monkeypatch.setattr(slide_vision, "resolve_stored_file", lambda p: "/tmp/x.pdf")

    def boom(path, page):
        raise SlideRenderError("page 99 out of range")

    monkeypatch.setattr(slide_vision, "render_page_data_url", boom)
    assert slide_vision.read_slide(db, uploaded=uploaded, page=99) is None


def test_nothing_happens_while_it_is_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "gemini_vision_enabled", False)
    uploaded = _file(db)

    def should_not_run(*args, **kwargs):
        raise AssertionError("read attempted while disabled")

    monkeypatch.setattr(slide_vision, "_ask", should_not_run)
    assert slide_vision.read_slide(db, uploaded=uploaded, page=4) is None


def test_a_key_without_the_switch_is_still_off(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_vision_enabled", False)
    assert slide_vision.enabled() is False

    monkeypatch.setattr(settings, "gemini_vision_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    assert slide_vision.enabled() is False


def test_no_slide_open_means_no_reading(db, vision_on):
    assert slide_vision.read_slide(db, uploaded=_file(db), page=None) is None
    assert slide_vision.read_slide(db, uploaded=None, page=4) is None


# --- The prompt the reader is given ----------------------------------------- #


def test_the_reader_is_told_to_transcribe_not_describe():
    """"Describe this image" returns "a screenshot with colourful blocks", which
    tells the answering model nothing it can reason about."""
    prompt = slide_vision.TRANSCRIBE_PROMPT
    assert "execution order" in prompt
    assert "nesting" in prompt
    assert "[unreadable]" in prompt          # gaps are marked, never filled
    assert "Never invent a value" in prompt
    assert "Do not teach" in prompt


def test_a_missing_key_is_reported_rather_than_attempted(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    ok, message = slide_vision.probe()
    assert ok is False
    assert "GEMINI_API_KEY" in message
