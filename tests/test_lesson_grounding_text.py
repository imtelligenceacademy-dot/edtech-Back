"""Whether the assistant actually has the lesson, and whether it knows.

Every caller of `_pdf_to_text` asks it one question — did we get the lesson's
text? — and acts on the answer. `lesson_context` falls back to the stored slide
titles when the answer is no; `_assemble_system` switches between telling the
model "the lesson material below is your primary source" and telling it the
text could not be extracted, so say when you are working without it.

Labelling pages that produced nothing answered yes for a deck that had produced
nothing at all: the labels are not whitespace, so a three-page scan came back as
three headings with nothing under them, which is perfectly truthy. The model was
told it was holding the lesson, shown an empty block, and asked what slide 4
said.
"""

from __future__ import annotations

import pytest

from app.services.pdf_text import _pdf_to_text


class _Page:
    def __init__(self, text: str | None):
        self._text = text

    def extract_text(self):
        return self._text


class _Reader:
    """Stands in for pypdf, which is imported inside the function under test."""

    pages: list[_Page] = []

    def __init__(self, _path):
        pass


@pytest.fixture()
def pages(monkeypatch):
    """Let a test say what pypdf finds on each page."""

    def _set(texts: list[str | None]):
        import pypdf

        class _Fake(_Reader):
            def __init__(self, _path):
                self.pages = [_Page(t) for t in texts]

        monkeypatch.setattr(pypdf, "PdfReader", _Fake)

    return _set


def test_a_deck_with_no_text_layer_comes_back_empty(pages):
    """A scan, or a deck exported as flat images. The honest answer is nothing.

    Returned as headings, this was the answer that broke both callers: neither
    the fallback to the stored slides nor the prompt branch that admits the text
    is missing could ever be reached.
    """
    pages(["", "   ", None])

    assert _pdf_to_text("anything.pdf") == ""


def test_pages_that_do_have_text_are_labelled_by_their_page_number(pages):
    pages(["Intro to loops", "for i in range(10):"])

    out = _pdf_to_text("anything.pdf")

    assert "--- Slide 1 ---\nIntro to loops" in out
    assert "--- Slide 2 ---\nfor i in range(10):" in out


def test_an_image_slide_among_text_slides_does_not_shift_the_numbering(pages):
    """Slide 2 is a photograph. Slide 3 must still be called slide 3, because
    the teacher asking about "slide 3" means the third page of the deck."""
    pages(["Title page", "", "Wiring diagram explained"])

    out = _pdf_to_text("anything.pdf")

    assert "--- Slide 1 ---" in out
    assert "--- Slide 2 ---" not in out, "an empty page is not lesson material"
    assert "--- Slide 3 ---\nWiring diagram explained" in out


def test_an_unreadable_file_is_empty_rather_than_an_exception(pages, monkeypatch):
    import pypdf

    def _explode(_path):
        raise ValueError("not a pdf")

    monkeypatch.setattr(pypdf, "PdfReader", _explode)

    assert _pdf_to_text("broken.pdf") == ""
