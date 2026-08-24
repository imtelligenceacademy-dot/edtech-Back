"""Teacher AI assistant. Answers questions, optionally grounded in the opened
lesson's PDF. Teacher-only (use-ai-assistant capability); a teacher can only
ground on lessons actually assigned to them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_capability, require_roles
from app.models import FairProject, Lesson, LessonAssignment, UploadedFile, User
from app.models.enums import Role
from app.schemas.ai import (
    AdminChatRequest,
    AIChatRequest,
    AIChatResponse,
    AIHealth,
    AIUsageStats,
)
from app.services.ai_usage import AILimitExceeded, enforce_ai_limit, record_ai_usage, usage_stats
from app.services.lesson_access import is_lesson_available
from app.services.file_storage import resolve_stored_file
from app.services.llm import ChatMessage, LLMError, get_provider
from app.services.pdf_render import SlideRenderError, render_page_data_url
from app.services.pdf_text import lesson_context, uploaded_file_context
from app.services.report_docx import build_school_ai_report
from app.services.school_context import build_school_context

logger = logging.getLogger("app.ai")

router = APIRouter(prefix="/api/ai", tags=["ai"])

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The exact sentence the assistant uses when a request is outside its scope.
REFUSAL = (
    "I can only help with this lesson and related robotics, electronics and "
    "coding topics."
)

# --------------------------------------------------------------------------- #
# Shared robotics teaching policy.
#
# The assistant is no longer limited to repeating the PDF. The open lesson is
# its *context*, not the boundary of its knowledge: it may draw on general
# robotics / electronics / coding knowledge to help the teacher actually teach
# the material. It still refuses unrelated subjects and unsafe electrical advice.
# --------------------------------------------------------------------------- #
_ROLE = """You are IM-Telligence, an expert classroom robotics teaching assistant for a K-12 STEAM teacher. A lesson is open in front of them and you are helping them understand, prepare and teach it."""

_SCOPE = """SCOPE - what you help with:
- The open lesson: its content, how to teach it, and classroom activities.
- Robotics and physical computing: micro:bit, Arduino, sensors, actuators, servos, motors, LEDs, buzzers, breadboards and wiring.
- Electronics concepts at classroom level: voltage, current, resistance, pull-up/pull-down, analogue vs digital, power and ground.
- Code that supports the lesson or physical computing: MakeCode, Scratch, Python/MicroPython and Arduino C++ - including explaining, writing, debugging and improving it.
- Practical teaching help: student misconceptions, extension tasks, and troubleshooting a build that will not work.

REFUSE - politely decline anything outside that: news, sports, entertainment, politics, personal or medical advice, unrelated school subjects, or general non-STEM requests. When refusing, reply with exactly this sentence and nothing else:
"I can only help with this lesson and related robotics, electronics and coding topics.\""""

_SOURCES = """USING THE LESSON:
- The lesson material below is your primary source for anything specific to this lesson. Its text is split into slides labelled "--- Slide N ---"; when asked about slide N, use the text under that exact label.
- Beyond those specifics you may and should use your own robotics knowledge to explain, expand, give examples and troubleshoot.
- Make clear which is which: say when something comes from the lesson, versus when it is your own additional recommendation.
- Never contradict the lesson material, and never invent what a slide shows.
- If the board, component version, voltage or pin numbers are not stated, say what you are assuming before you answer."""

_WIRING = """WIRING AND HARDWARE ANSWERS must include:
1. The components needed.
2. A voltage compatibility check between the board and each component.
3. The exact pin-to-pin connections, where they are known.
4. Power, ground, any required resistor, and whether external power is needed.
5. An explicit warning when a motor or other load must NOT be driven directly from a controller pin (use a driver, transistor or external supply).
6. A final "check before powering on" step.

SAFETY - never give instructions involving mains or wall voltage, high-current batteries, rewiring household power, or bypassing or disabling any protection such as a fuse, resistor or driver board. Refuse those and redirect to safe low-voltage classroom equipment."""

_FORMAT = """FORMAT - reply in plain text a teacher can read at a glance:
- No Markdown tables, no Markdown headings, and never put ** or __ around words. Those markers are shown literally to the teacher and look broken.
- Use short paragraphs, or simple numbered steps for procedures.
- Use a fenced code block ONLY when actually showing code.
- Be concise and classroom-friendly."""

_LANGUAGE = """LANGUAGE - always reply in the same language the teacher wrote their question in. If that is unclear, use {lang}. Keep technical component names (micro:bit, GPIO, servo) in their usual form."""

_VISION_ON = """SLIDE IMAGE - an image of slide {slide}, the slide the teacher is looking at right now, is attached. Read it carefully: diagrams, wiring, arrows, block code (MakeCode/Scratch) nesting and order, screenshots, icons, and any text the PDF text layer missed. You can only see THIS slide - never claim to see any other slide. If a detail is too small or blurry to read, say so instead of guessing."""

_VISION_FAILED = """SLIDE IMAGE - the teacher is viewing slide {slide}, but its image could NOT be inspected this time. Answer from the lesson text and your robotics knowledge, and tell the teacher you could not visually check the slide. Never describe what the slide looks like."""

_VISION_OFF = """You cannot see the slides as images - you only have the extracted text. Never describe the visual appearance of a slide."""


def _language_name(user: User) -> str:
    return "French" if (user.language or "").lower() == "fr" else "English"


def _policy(user: User, *, vision_note: str) -> str:
    """Assemble the full teacher-assistant policy for this request."""
    return "\n\n".join(
        [
            _ROLE,
            _SCOPE,
            _SOURCES,
            _WIRING,
            _FORMAT,
            _LANGUAGE.format(lang=_language_name(user)),
            vision_note,
        ]
    )


_NO_LESSON = """You are IM-Telligence, a classroom robotics teaching assistant. No lesson or ICT Fair project is open right now, so you cannot help yet. Reply with exactly this sentence and nothing else:
"Open one of your lessons or an ICT Fair project first, then I can help you with it.\""""


def _accessible_lesson(db: Session, teacher: User, lesson_id: str) -> Lesson | None:
    lesson = db.scalar(
        select(Lesson)
        .options(selectinload(Lesson.uploaded_files), selectinload(Lesson.slides))
        .where(Lesson.id == lesson_id)
    )
    if lesson is None:
        return None
    assigned = db.scalar(
        select(LessonAssignment.id).where(
            LessonAssignment.lesson_id == lesson_id,
            LessonAssignment.teacher_id == teacher.id,
        )
    )
    if not assigned:
        return None
    # Only ground on a lesson the teacher is actually allowed to have open now.
    return lesson if is_lesson_available(db, teacher, lesson_id) else None


def _accessible_fair_project(db: Session, teacher: User, project_id: str) -> FairProject | None:
    if teacher.role == Role.teacher and not teacher.ict_fair_access:
        return None
    return db.get(FairProject, project_id)


@dataclass
class PromptBundle:
    """Everything the streaming generator needs, resolved up-front while the DB
    session is still open."""

    system: str
    messages: list[ChatMessage]
    source_ref: str | None
    image_data_url: str | None = None
    grounded: bool = False


def _pdf_path_for(db: Session, *, lesson: Lesson | None, project: FairProject | None):
    """Resolve the stored PDF backing the open lesson/project, or None.

    Only ever called after the access checks above have passed.
    """
    uploaded = None
    if lesson is not None:
        files = getattr(lesson, "uploaded_files", []) or []
        uploaded = files[0] if files else None
    elif project is not None and project.file_id:
        uploaded = db.get(UploadedFile, project.file_id)
    if uploaded is None or not uploaded.storage_path:
        return None
    return resolve_stored_file(uploaded.storage_path)


def _slide_image(
    db: Session,
    *,
    lesson: Lesson | None,
    project: FairProject | None,
    current_slide: int | None,
) -> tuple[str | None, bool]:
    """Render the slide the teacher is viewing.

    Returns (data_url, attempted). `attempted` is True whenever we were asked for
    a slide image, so the caller can tell the model that the visual check failed
    rather than silently answering text-only.
    """
    if current_slide is None:
        return None, False
    if not settings.ai_teacher_vision_enabled:
        return None, False
    provider = get_provider()
    if not getattr(provider, "supports_vision", False):
        return None, False

    path = _pdf_path_for(db, lesson=lesson, project=project)
    if path is None:
        return None, True
    try:
        return render_page_data_url(path, current_slide), True
    except SlideRenderError as exc:
        # Never log the image itself - only why it failed.
        logger.warning("slide render failed (page %s): %s", current_slide, exc)
        return None, True


def _vision_note(image_data_url: str | None, attempted: bool, slide: int | None) -> str:
    if image_data_url is not None and slide is not None:
        return _VISION_ON.format(slide=slide)
    if attempted and slide is not None:
        return _VISION_FAILED.format(slide=slide)
    return _VISION_OFF


def _build_prompt(db: Session, current: User, payload: AIChatRequest) -> PromptBundle:
    """Resolve access, lesson context and (optionally) the slide image, then
    assemble the robotics-assistant prompt."""
    lesson = _accessible_lesson(db, current, payload.lesson_id) if payload.lesson_id else None
    project = (
        _accessible_fair_project(db, current, payload.fair_project_id)
        if payload.fair_project_id
        else None
    )

    messages: list[ChatMessage] = [
        {"role": t.role, "content": t.content} for t in payload.history
    ]
    messages.append({"role": "user", "content": payload.message})

    # Nothing open (or not accessible to this teacher) - refuse before doing any
    # rendering or provider work.
    if lesson is None and project is None:
        return PromptBundle(system=_NO_LESSON, messages=messages, source_ref=None)

    image_data_url, attempted = _slide_image(
        db, lesson=lesson, project=project, current_slide=payload.current_slide
    )
    policy = _policy(
        current, vision_note=_vision_note(image_data_url, attempted, payload.current_slide)
    )

    if project is not None:
        uploaded = db.get(UploadedFile, project.file_id) if project.file_id else None
        context = uploaded_file_context(uploaded)
        title = project.title
        label = "ICT FAIR PROJECT"
    else:
        context = lesson_context(lesson)
        title = lesson.title
        label = "LESSON"

    if context:
        system = (
            f'{policy}\n\nThe open {label.lower()} is "{title}".\n'
            f"{label} MATERIAL:\n<material>\n{context}\n</material>"
        )
    else:
        system = (
            f'{policy}\n\nThe open {label.lower()} is "{title}". Its text could not '
            "be extracted, so rely on the slide image (if attached), the title, and "
            "your robotics knowledge - and say when you are doing so."
        )

    # Report what was actually consulted, so the teacher sees an honest source.
    source_ref = title
    if image_data_url is not None and payload.current_slide is not None:
        source_ref = f"{title} - slide {payload.current_slide}"

    return PromptBundle(
        system=system,
        messages=messages,
        source_ref=source_ref,
        image_data_url=image_data_url,
        grounded=True,
    )


@router.get("/health", response_model=AIHealth)
def health(_: User = Depends(get_current_user)) -> AIHealth:
    provider = get_provider()
    return AIHealth(
        provider=provider.name,
        model=getattr(provider, "model", None),
        ready=provider.name != "mock",
    )


@router.get("/usage", response_model=AIUsageStats)
def usage(
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.super_admin, Role.school_admin)),
) -> AIUsageStats:
    # Super-admins see every school; school-admins only their own (scoped in the service).
    return AIUsageStats(**usage_stats(db, current))


# What the teacher is told for each failure kind. Deliberately free of provider
# names, credentials, and internal detail.
_ERROR_TEXT = {
    "auth": "The AI assistant is not configured correctly. Please tell your administrator.",
    "rate_limit": "The AI assistant is busy right now. Please try again in a moment.",
    "quota": "The AI assistant has reached its usage quota. Please tell your administrator.",
    "timeout": "The AI assistant took too long to respond. Please try again.",
    "unavailable": "The AI assistant is unavailable right now. Please try again.",
}


def _error_text(exc: Exception) -> str:
    kind = getattr(exc, "kind", "unavailable")
    return _ERROR_TEXT.get(kind, _ERROR_TEXT["unavailable"])


def _stream_answer(bundle: PromptBundle):
    """Yield deltas from the provider, using the vision path when a slide image
    was rendered and the provider supports it."""
    provider = get_provider()
    if bundle.image_data_url is not None and getattr(provider, "supports_vision", False):
        try:
            yield from provider.chat_stream_vision(
                bundle.system, bundle.messages, bundle.image_data_url
            )
            return
        except LLMError as exc:
            # Vision failed after the prompt was built - fall back to text so the
            # teacher still gets an answer.
            logger.warning("vision stream failed (%s), falling back to text", exc.kind)
    yield from provider.chat_stream(bundle.system, bundle.messages)


@router.post("/chat", response_model=AIChatResponse)
def chat(
    payload: AIChatRequest,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("use-ai-assistant")),
) -> AIChatResponse:
    bundle = _build_prompt(db, current, payload)
    try:
        enforce_ai_limit(db, current, "teacher")
    except AILimitExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=exc.message) from exc
    record_ai_usage(db, current, "teacher")
    provider = get_provider()
    try:
        content = provider.chat(bundle.system, bundle.messages)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_error_text(exc),
        ) from exc
    return AIChatResponse(
        content=content, source_ref=bundle.source_ref, provider=provider.name
    )


@router.post("/chat/stream")
def chat_stream(
    payload: AIChatRequest,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("use-ai-assistant")),
) -> StreamingResponse:
    # Everything DB-bound (and the slide image) is resolved before the generator
    # runs, because the session is closed by the time streaming starts.
    bundle = _build_prompt(db, current, payload)
    try:
        enforce_ai_limit(db, current, "teacher")
    except AILimitExceeded as exc:
        def limited_stream():
            yield f"data: {json.dumps({'error': exc.message})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"

        return StreamingResponse(
            limited_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    record_ai_usage(db, current, "teacher")

    def event_stream():
        if bundle.source_ref:
            yield f"data: {json.dumps({'sourceRef': bundle.source_ref})}\n\n"
        try:
            for delta in _stream_answer(bundle):
                yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception as exc:
            logger.warning("teacher chat stream failed: %s", type(exc).__name__)
            yield f"data: {json.dumps({'error': _error_text(exc)})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# School-admin assistant — grounded in the school's live monitoring data.
# --------------------------------------------------------------------------- #
ADMIN_REFUSAL = (
    "I'm sorry, but I can only assist with information about your school."
)

# Response formatting shared by the chat assistants. Both chat UIs render the
# reply as raw text, so Markdown is shown literally to the user ("**Un
# micro:bit**") and tables collapse into unreadable pipes. The Word report
# prompt deliberately keeps Markdown headings - report_docx parses them.
_PLAIN_TEXT_RULES = (
    "FORMATTING - the chat shows your reply as plain text, so Markdown is not "
    "rendered and any markers you type are displayed literally. "
    "Never use Markdown tables, Markdown headings, or ** or __ around words. "
    "Use short plain-text paragraphs, or a simple numbered list when you need "
    "structure. To compare several teachers or figures, write one short line "
    "per item instead of a table. Keep it concise and easy to read at a glance."
)

_ADMIN_GUARDRAILS = (
    "You are IM-Telligence, a professional operations assistant for a school "
    "principal or administrator. Maintain a courteous, respectful, and formal "
    "tone at all times — address the user politely (e.g. 'Certainly', 'Of course', "
    "'Happy to help'), never casual or curt. "
    "Answer questions about THIS SCHOOL's data only — its teachers, their lesson "
    "progress, late/at-risk lessons, completion rates, security alerts, and reports. "
    "Be clear and concise, and cite concrete numbers from the data. If the "
    "administrator greets you, respond with a brief, warm, professional greeting "
    "and offer to help. If asked about anything unrelated to this school's "
    "operations (general knowledge, weather, other schools, lesson content, etc.), "
    "politely decline by replying with EXACTLY this sentence and nothing else:\n"
    f'"{ADMIN_REFUSAL}"\n'
    "Use only the SCHOOL DATA below; never invent figures.\n\n"
    f"{_PLAIN_TEXT_RULES}"
)


def _build_admin_prompt(
    db: Session, admin: User, payload: AdminChatRequest
) -> tuple[str, list[ChatMessage], str]:
    context, school_name = build_school_context(db, admin)
    system = f"{_ADMIN_GUARDRAILS}\n\n<SCHOOL DATA>\n{context}\n</SCHOOL DATA>"
    messages: list[ChatMessage] = [
        {"role": t.role, "content": t.content} for t in payload.history
    ]
    messages.append({"role": "user", "content": payload.message})
    return system, messages, school_name


@router.post("/admin/chat/stream")
def admin_chat_stream(
    payload: AdminChatRequest,
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.school_admin)),
) -> StreamingResponse:
    system, messages, school_name = _build_admin_prompt(db, current, payload)
    try:
        enforce_ai_limit(db, current, "admin")
    except AILimitExceeded as exc:
        def limited_stream():
            yield f"data: {json.dumps({'error': exc.message})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"

        return StreamingResponse(
            limited_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    record_ai_usage(db, current, "admin")
    provider = get_provider()

    def event_stream():
        yield f"data: {json.dumps({'sourceRef': school_name})}\n\n"
        try:
            for delta in provider.chat_stream(system, messages):
                yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception:
            yield f"data: {json.dumps({'error': 'AI assistant unavailable'})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# School-admin report — the assistant writes a narrative from the school's live
# data, rendered into a downloadable Word (.docx) alongside the data tables.
# --------------------------------------------------------------------------- #
_REPORT_SYSTEM = (
    "You are IM-Telligence, writing a concise, professional report on a school for "
    "its principal. Using ONLY the SCHOOL DATA provided, write a clear narrative "
    "report. Structure it with these markdown headings, in this order:\n"
    "## Overview\n## Teacher Engagement\n## Lesson Progress\n"
    "## Risks & Late Lessons\n## Security\n## Recommendations\n"
    "Use short paragraphs and '- ' bullet points. Cite concrete numbers from the "
    "data. Never invent figures. Keep the whole report under 500 words."
)

_REPORT_FALLBACK = (
    "## Overview\nThe automated narrative is unavailable right now, but the data "
    "tables below reflect your school's current status."
)


@router.post("/admin/report")
def admin_report(
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.school_admin)),
) -> StreamingResponse:
    if not current.school_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No school on account"
        )

    context, _ = build_school_context(db, current)
    system = f"{_REPORT_SYSTEM}\n\n<SCHOOL DATA>\n{context}\n</SCHOOL DATA>"
    provider = get_provider()
    try:
        enforce_ai_limit(db, current, "admin")
    except AILimitExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=exc.message) from exc
    try:
        narrative = provider.chat(
            system, [{"role": "user", "content": "Write the school report now."}]
        )
    except Exception:
        narrative = _REPORT_FALLBACK

    record_ai_usage(db, current, "admin")
    buf, filename = build_school_ai_report(db, current.school_id, current.name, narrative)
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        buf, media_type=DOCX_MEDIA, headers={"Content-Disposition": disposition}
    )
