"""Render a single PDF page to a compressed image for the vision assistant.

Only the slide the teacher is currently looking at is ever rendered — never the
whole document. The result stays in memory as a data URL; no image is written to
disk and no public URL is created.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger("app.pdf_render")

# Rendering ladder: try the sharpest setting first, then step down until the
# encoded image fits ai_max_image_bytes. Higher zoom keeps pin labels and block
# text readable; JPEG quality is dropped before resolution because legibility
# suffers more from downscaling than from mild compression.
_RENDER_STEPS: tuple[tuple[float, int], ...] = (
    (2.0, 85),
    (2.0, 70),
    (1.5, 70),
    (1.25, 60),
    (1.0, 55),
)


class SlideRenderError(RuntimeError):
    """Raised when the page cannot be rendered (bad range, unreadable PDF...)."""


def page_count(path: Path) -> int:
    """Number of pages in the PDF, or 0 if it cannot be opened."""
    try:
        import pymupdf

        with pymupdf.open(str(path)) as doc:
            return doc.page_count
    except Exception:
        return 0


def render_page_data_url(path: Path, page_number: int) -> str:
    """Render a 1-based page to a `data:image/jpeg;base64,...` URL.

    Raises SlideRenderError when the page is out of range or rendering fails, so
    the caller can fall back to text-only grounding.
    """
    try:
        import pymupdf
    except Exception as exc:  # pragma: no cover - import guard
        raise SlideRenderError("pymupdf is not available") from exc

    # Respect the configured ceiling exactly: if a slide cannot be encoded
    # small enough, we fail and the caller falls back to text-only.
    max_bytes = settings.ai_max_image_bytes

    try:
        with pymupdf.open(str(path)) as doc:
            if not (1 <= page_number <= doc.page_count):
                raise SlideRenderError(
                    f"page {page_number} out of range (1-{doc.page_count})"
                )
            page = doc.load_page(page_number - 1)

            encoded: bytes | None = None
            for zoom, quality in _RENDER_STEPS:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                data = pix.tobytes("jpg", jpg_quality=quality)
                if len(data) <= max_bytes:
                    encoded = data
                    break
                encoded = data  # keep the smallest attempt seen so far

            if encoded is None or len(encoded) > max_bytes:
                raise SlideRenderError("rendered page exceeds the image size limit")
    except SlideRenderError:
        raise
    except Exception as exc:
        raise SlideRenderError(f"could not render page {page_number}") from exc

    b64 = base64.b64encode(encoded).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"
