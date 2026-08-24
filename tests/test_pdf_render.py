"""Slide rendering: correct page, range validation, size ceiling."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.services.pdf_render import (
    SlideRenderError,
    page_count,
    render_page_data_url,
)


@pytest.fixture()
def pdf(tmp_path) -> Path:
    """A synthetic 3-page PDF with distinct text per page."""
    import pymupdf

    doc = pymupdf.open()
    for n in (1, 2, 3):
        page = doc.new_page()
        page.insert_text((72, 144), f"SLIDE {n} CONTENT", fontsize=40)
    target = tmp_path / "deck.pdf"
    doc.save(str(target))
    doc.close()
    return target


def test_page_count(pdf):
    assert page_count(pdf) == 3


def test_renders_a_data_url(pdf):
    url = render_page_data_url(pdf, 2)
    assert url.startswith("data:image/jpeg;base64,")
    # Decodes to a real JPEG (SOI marker).
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:2] == b"\xff\xd8"


def test_selects_the_requested_page(pdf):
    """Different pages must produce different images."""
    one = render_page_data_url(pdf, 1)
    two = render_page_data_url(pdf, 2)
    assert one != two


@pytest.mark.parametrize("bad", [0, -1, 4, 999])
def test_out_of_range_pages_are_refused(pdf, bad):
    with pytest.raises(SlideRenderError):
        render_page_data_url(pdf, bad)


def test_unreadable_file_raises(tmp_path):
    broken = tmp_path / "not-a.pdf"
    broken.write_bytes(b"this is not a pdf")
    with pytest.raises(SlideRenderError):
        render_page_data_url(broken, 1)


def test_respects_the_size_ceiling(pdf, monkeypatch):
    from app.config import settings

    # An impossibly small ceiling must fail loudly rather than send a huge image.
    monkeypatch.setattr(settings, "ai_max_image_bytes", 10)
    with pytest.raises(SlideRenderError):
        render_page_data_url(pdf, 1)


def test_image_stays_within_the_configured_ceiling(pdf):
    from app.config import settings

    url = render_page_data_url(pdf, 1)
    raw = base64.b64decode(url.split(",", 1)[1])
    assert len(raw) <= settings.ai_max_image_bytes
