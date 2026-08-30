"""Reading a slide with one model so another can answer about it.

The teacher assistant answers with a text-only model. That was fine until the
grades where the code *is* a picture: in Year 1 and the lower Year 2 grades the
lesson is MakeCode / Scratch blocks, and a PDF of block code has essentially no
text layer — the assistant was being asked about a slide it could not read a
word of.

The obvious fix, making the answering model multimodal, is the wrong trade: it
means giving up a model that reasons about robotics well in exchange for one
that can see. So the two jobs are split. A vision model reads the slide once and
writes down what is on it; that writing is cached against the file and handed to
the answering model as text, like any other lesson material. The answering model
never needs eyes, and the reading is paid for once per slide rather than once
per question.

Everything here fails soft. A missing key, a model name that does not resolve, a
timeout or a malformed reply all return None, and the teacher gets exactly the
answer they would have got before — never an error.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Lesson, SlideReading, UploadedFile
from app.services.pdf_render import SlideRenderError, render_page_data_url
from app.services.file_storage import resolve_stored_file
from app.utils import new_id

logger = logging.getLogger("app.slide_vision")

# Written for block code specifically. "Describe this image" returns "a
# screenshot showing colourful blocks", which tells the answering model nothing.
# What it needs is the program: order, nesting, and every literal value.
TRANSCRIBE_PROMPT = """You are reading one slide from a K-12 robotics lesson so that another assistant, which cannot see the slide, can answer a teacher's questions about it.

Write down what is actually on the slide. Do not teach, do not comment, do not offer improvements.

If the slide shows block code (MakeCode, Scratch, or similar), transcribe it as indented pseudocode:
- Keep the exact execution order and the nesting of every block inside its parent.
- Keep event and loop blocks by name: "on start", "forever", "on button A pressed", "repeat 4 times".
- Keep every literal: pin numbers, angles, delays in ms, variable names, comparison values, colours.
- If two stacks sit side by side, transcribe them as separate programs and say so.

If the slide shows wiring or a circuit, list each connection as "component pin -> board pin", plus any resistor, power or ground.

Also note, briefly: the slide title, any instruction text to the teacher, and what any photo or diagram depicts.

If something is too small or blurry to read with confidence, write "[unreadable]" in its place rather than guessing. Never invent a value.

Output plain text only. No markdown headings, no bold, no code fences."""


def applies_to(lesson: Lesson | None) -> bool:
    """Whether this lesson's slides are worth reading.

    Year 1 is block coding across every grade. Year 2 turns to Python from the
    configured grade up, and Python in a PDF is real selectable text that the
    existing extraction already delivers — reading those slides as pictures
    would be spend with nothing to show for it. Same boundary as the kit split,
    because it is the same change in the curriculum.
    """
    if lesson is None:
        return False
    year = lesson.year or 2
    if year == 1:
        return True
    return (lesson.grade or 0) < settings.year2_advanced_from_grade


def enabled() -> bool:
    return bool(settings.gemini_vision_enabled and settings.gemini_api_key)


def _ask(data_url: str) -> str | None:
    """One vision call, through Google's OpenAI-compatible surface."""
    try:
        import httpx

        response = httpx.post(
            f"{settings.gemini_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.gemini_api_key}"},
            json={
                "model": settings.gemini_vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": TRANSCRIBE_PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
            },
            timeout=settings.ai_timeout_seconds,
        )
        if response.status_code != 200:
            # The body carries the useful part ("model not found"), so keep it —
            # this is what the probe endpoint shows an admin.
            logger.warning(
                "slide reading failed: %s %s",
                response.status_code,
                response.text[:300],
            )
            return None
        text = response.json()["choices"][0]["message"]["content"]
        return text.strip() or None
    except Exception as exc:
        logger.warning("slide reading failed: %s", exc)
        return None


def _cached(db: Session, file_id: str, page: int) -> SlideReading | None:
    return db.scalar(
        select(SlideReading).where(
            SlideReading.file_id == file_id, SlideReading.page == page
        )
    )


def read_slide(db: Session, *, uploaded: UploadedFile | None, page: int | None) -> str | None:
    """What is on this slide, from cache or by reading it once.

    Returns None whenever the reading is unavailable for any reason, which the
    caller treats as "no visual detail this time" rather than as a failure.
    """
    if not enabled() or uploaded is None or not page or page < 1:
        return None

    hit = _cached(db, uploaded.id, page)
    if hit is not None:
        return hit.text or None

    if not uploaded.storage_path:
        return None
    path = resolve_stored_file(uploaded.storage_path)
    if path is None:
        return None

    try:
        data_url = render_page_data_url(path, page)
    except SlideRenderError as exc:
        logger.warning("slide render failed (page %s): %s", page, exc)
        return None

    text = _ask(data_url)
    if not text:
        return None

    db.add(
        SlideReading(
            id=new_id("sr"),
            file_id=uploaded.id,
            page=page,
            text=text,
            model=settings.gemini_vision_model,
        )
    )
    try:
        db.commit()
    except Exception:
        # Two teachers opening the same slide at once: one insert wins, and the
        # other still has its answer to return.
        db.rollback()
    return text


def probe() -> tuple[bool, str]:
    """Ask the configured model to answer once, and report exactly what came
    back. This is how an admin finds out whether a model name resolves, without
    reading logs or guessing."""
    if not settings.gemini_api_key:
        return False, "GEMINI_API_KEY is not set."
    try:
        import httpx

        response = httpx.post(
            f"{settings.gemini_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.gemini_api_key}"},
            json={
                "model": settings.gemini_vision_model,
                "messages": [{"role": "user", "content": "Reply with the word: ok"}],
                "max_tokens": 5,
            },
            timeout=settings.ai_timeout_seconds,
        )
    except Exception as exc:
        return False, f"Could not reach the provider: {exc}"

    if response.status_code == 200:
        return True, f"{settings.gemini_vision_model} responded."
    return False, f"HTTP {response.status_code}: {response.text[:400]}"


def list_models() -> tuple[bool, list[str], str]:
    """Every model the key can actually use — the answer to "does this name
    exist?" without anyone pasting a key into a terminal."""
    if not settings.gemini_api_key:
        return False, [], "GEMINI_API_KEY is not set."
    try:
        import httpx

        response = httpx.get(
            f"{settings.gemini_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {settings.gemini_api_key}"},
            timeout=settings.ai_timeout_seconds,
        )
        if response.status_code != 200:
            return False, [], f"HTTP {response.status_code}: {response.text[:400]}"
        data = response.json().get("data") or []
        names = sorted(
            str(m.get("id", "")).removeprefix("models/") for m in data if m.get("id")
        )
        return True, names, f"{len(names)} model(s) available to this key."
    except Exception as exc:
        return False, [], f"Could not reach the provider: {exc}"
