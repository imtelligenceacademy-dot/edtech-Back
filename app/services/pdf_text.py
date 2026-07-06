"""Extract the text of a lesson for AI grounding — from its linked PDF if
present, otherwise from its slide titles/bodies. Truncated to a char budget so
the prompt stays within a sane size.
"""

from __future__ import annotations

from app.config import settings
from app.models import Lesson, UploadedFile
from app.services.file_storage import resolve_stored_file


def _pdf_to_text(path: Path) -> str:
    """Extract text with each PDF page labelled as a slide, so the assistant can
    map "slide N" to the actual Nth page of the deck.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts: list[str] = []
        for i, page in enumerate(reader.pages, start=1):
            body = (page.extract_text() or "").strip()
            parts.append(f"--- Slide {i} ---\n{body}")
        return "\n\n".join(parts)
    except Exception:
        return ""


def lesson_context(lesson: Lesson) -> str:
    """Return grounding text for a lesson, capped to ai_max_context_chars."""
    text = ""
    files = getattr(lesson, "uploaded_files", []) or []
    if files and files[0].storage_path:
        path = resolve_stored_file(files[0].storage_path)
        if path is not None:
            text = _pdf_to_text(path)

    if not text.strip() and lesson.slides:
        text = "\n\n".join(
            f"--- Slide {s.index} ---\n{s.title}\n{s.body}" for s in lesson.slides
        )

    return text.strip()[: settings.ai_max_context_chars]


def uploaded_file_context(uploaded: UploadedFile | None) -> str:
    """Return grounding text for a standalone uploaded PDF."""
    if uploaded is None or not uploaded.storage_path:
        return ""
    path = resolve_stored_file(uploaded.storage_path)
    if path is None:
        return ""
    return _pdf_to_text(path).strip()[: settings.ai_max_context_chars]
