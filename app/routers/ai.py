"""Teacher AI assistant. Answers questions, optionally grounded in the opened
lesson's PDF. Teacher-only (use-ai-assistant capability); a teacher can only
ground on lessons actually assigned to them.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal, get_db
from app.deps import get_current_user, require_capability, require_roles
from app.models import FairProject, Lesson, LessonAssignment, UploadedFile, User
from app.models.enums import Role
from app.schemas.ai import (
    AdminChatRequest,
    AIChatRequest,
    AIChatResponse,
    AIHealth,
    AIQuota,
    AITeacherUsageReport,
    AIUsageStats,
    VisionProbe,
)
from app.services.ai_usage import (
    AILimitExceeded,
    enforce_ai_limit,
    quota_for,
    record_ai_usage,
    teacher_usage_report,
    usage_stats,
)
from app.services.chat_history import save_exchange
from app.services.lesson_access import is_lesson_available
from app.services.sections import resolve_section, sections_for
from app.services.file_storage import resolve_stored_file
from app.services.llm import ChatMessage, LLMError, get_provider
from app.services.pdf_render import SlideRenderError, render_page_data_url
from app.services.pdf_text import lesson_context, uploaded_file_context
from app.services import hardware, kits, slide_vision
from app.services.platform_context import build_platform_context
from app.services.report_docx import build_school_ai_report, build_super_ai_report
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
- Never invent what a slide shows.
- On teaching, sequencing and what the class is meant to do, the lesson is the last word. On ELECTRICAL FACTS it is not: a slide can describe a different revision of a module, or simply be wrong. Where a verified hardware profile below disagrees with the lesson, follow the profile and tell the teacher the slide looks incorrect.
- If the board, component version, voltage or pin numbers are not stated, say what you are assuming before you answer."""

_WIRING = """WIRING AND HARDWARE ANSWERS must include:
1. The components needed.
2. A voltage compatibility check between the board and each component.
3. The exact pin-to-pin connections, where they are known.
4. Power, ground, any required resistor, and whether external power is needed.
5. An explicit warning when a motor or other load must NOT be driven directly from a controller pin (use a driver, transistor or external supply).
6. A final "check before powering on" step.

SAFETY - never give instructions involving mains or wall voltage, high-current batteries, rewiring household power, or bypassing or disabling any protection such as a fuse, resistor or driver board. Refuse those and redirect to safe low-voltage classroom equipment."""

# Length is a teaching constraint before it is a cost one. The teacher is at the
# front of a room with a class waiting; they read the first line and act. An
# answer that buries the useful sentence under a preamble has failed even when
# every word in it is correct.
#
# The exemption matters as much as the limit. Wiring steps, safety warnings and
# code ARE the answer - trimming those to hit a word count would make the
# assistant shorter and worse, and in the wiring case unsafe. The prose around
# them is what gets cut.
_FORMAT = """FORMAT - plain text a teacher can read at a glance, mid-lesson, with a class waiting:
- ANSWER FIRST. The opening sentence answers the question. Background, context and caveats come after it, if at all.
- LENGTH - aim for under 120 words. Most questions need two or three short sentences. Stop when the question is answered.
- Cut throat-clearing: no "Great question", no restating the question back, no closing offer of further help.
- NEVER shorten these to hit the limit, because they are the answer rather than padding: the numbered steps of a wiring procedure, any safety warning, the "check before powering on" step, and code the teacher has to type. Cut the prose around them instead.
- When a full answer genuinely needs more room, give the short answer first, then one line offering the rest - for example "Want the full step-by-step?"
- No Markdown tables, no Markdown headings, and never put ** or __ around words. Those markers are shown literally to the teacher and look broken.
- Use short paragraphs, or simple numbered steps for procedures.
- Use a fenced code block ONLY when actually showing code."""

_LANGUAGE = """LANGUAGE - always reply in the same language the teacher wrote their question in. If that is unclear, use {lang}. Keep technical component names (micro:bit, GPIO, servo) in their usual form."""

_VISION_ON = """SLIDE IMAGE - an image of slide {slide}, the slide the teacher is looking at right now, is attached. Read it carefully: diagrams, wiring, arrows, block code (MakeCode/Scratch) nesting and order, screenshots, icons, and any text the PDF text layer missed. You can only see THIS slide - never claim to see any other slide. If a detail is too small or blurry to read, say so instead of guessing."""

_VISION_FAILED = """SLIDE IMAGE - the teacher is viewing slide {slide}, but its image could NOT be inspected this time. Answer from the lesson text and your robotics knowledge, and tell the teacher you could not visually check the slide. Never describe what the slide looks like."""

_VISION_OFF = """You cannot see the slides as images - you only have the extracted text. Never describe the visual appearance of a slide."""

# When a vision model has read the slide for you. It is a transcription, not
# sight: the assistant must use it as what the slide shows, without claiming to
# have looked at it, and must not paper over the gaps the reader marked.
_VISION_READING = """SLIDE {slide} HAS BEEN READ FOR YOU. A separate vision model looked at the image of slide {slide} - the slide the teacher is on right now - and wrote down what is on it. That writing appears below under SLIDE READING, and for anything visual on this slide it is your source: block code and its nesting, wiring, diagrams, and text the PDF layer missed.
- Treat it as what the slide shows, and answer from it as confidently as from the lesson text.
- You did not see the slide yourself. Say "the slide shows", never "I can see".
- Anything marked [unreadable] was genuinely not legible. Say so; never fill it in.
- It covers ONLY slide {slide}. You know nothing visual about any other slide."""


def _language_name(user: User) -> str:
    return "French" if (user.language or "").lower() == "fr" else "English"


def _policy(
    user: User,
    *,
    vision_note: str,
    kit_note: str = "",
    hardware_note: str = "",
) -> str:
    """Assemble the full teacher-assistant policy for this request.

    The kit sits next to the wiring rules because that is what it constrains:
    without it the assistant answers from general micro:bit knowledge and names
    components the school has never owned.

    The hardware block follows immediately, and answers the next question down:
    given that the teacher has THIS module, what does a level or a value
    actually do on it. Without it the assistant knows which parts exist and
    still explains them from a general rule about 0 and 1 - which is how a
    common-anode RGB LED came to be described as turning on with 1.
    """
    parts = [
        _ROLE,
        _SCOPE,
        _SOURCES,
        _WIRING,
        kit_note,
        hardware_note,
        _FORMAT,
        _LANGUAGE.format(lang=_language_name(user)),
        vision_note,
    ]
    return "\n\n".join(part for part in parts if part)


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
    # Rebuilds the prompt for a model that cannot see, routing the slide through
    # the Gemini reader instead of attaching it. Set only when an image was
    # attached; called only if that provider drops out.
    text_fallback: Callable[[], str] | None = None


def _uploaded_for(
    db: Session, *, lesson: Lesson | None, project: FairProject | None
) -> UploadedFile | None:
    """The stored PDF row backing the open lesson/project, or None.

    Only ever called after the access checks above have passed.
    """
    if lesson is not None:
        files = getattr(lesson, "uploaded_files", []) or []
        return files[0] if files else None
    if project is not None and project.file_id:
        return db.get(UploadedFile, project.file_id)
    return None


def _pdf_path_for(db: Session, *, lesson: Lesson | None, project: FairProject | None):
    """Resolve the stored PDF backing the open lesson/project, or None.

    Only ever called after the access checks above have passed.
    """
    uploaded = _uploaded_for(db, lesson=lesson, project=project)
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

    system, source_ref = _assemble_system(
        db, current, payload, lesson, project, image_data_url, attempted
    )

    return PromptBundle(
        system=system,
        messages=messages,
        source_ref=source_ref,
        image_data_url=image_data_url,
        grounded=True,
        # Only meaningful when an image was attached: the same prompt rebuilt for
        # a model that cannot see, which routes the slide through the Gemini
        # reader instead. Deferred because it costs a reading, and most questions
        # never need it.
        text_fallback=(
            partial(_rebuild_without_image, current.id, payload)
            if image_data_url is not None
            else None
        ),
    )


def _assemble_system(
    db: Session,
    current: User,
    payload: AIChatRequest,
    lesson: Lesson | None,
    project: FairProject | None,
    image_data_url: str | None,
    attempted: bool,
) -> tuple[str, str | None]:
    """Build the system prompt for one question, and the source line shown with
    the answer.

    Called twice when the primary can see: once with the image, and again
    without it if that provider drops out. The second pass is not a degraded
    copy of the first — with no image, the Gemini reader transcribes the slide
    and the hardware retrieval runs again over that text, so the answering model
    still knows what is on the slide even though it cannot look at it.
    """
    # When the answering model cannot see, a vision model reads the slide and
    # the answering model works from that text instead. Only for the tracks
    # where the code is a picture; elsewhere the PDF text layer already has it.
    reading = None
    if image_data_url is None and slide_vision.applies_to(lesson):
        reading = slide_vision.read_slide(
            db,
            uploaded=_uploaded_for(db, lesson=lesson, project=project),
            page=payload.current_slide,
        )

    vision_note = (
        _VISION_READING.format(slide=payload.current_slide)
        if reading
        else _vision_note(image_data_url, attempted, payload.current_slide)
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

    # Resolved after the lesson text and the slide reading, because both are
    # evidence of which component the question is about - the teacher rarely
    # names it, but the slide they are looking at does.
    policy = _policy(
        current,
        vision_note=vision_note,
        kit_note=kits.kit_note(lesson),
        hardware_note=hardware.hardware_note(
            lesson,
            question=payload.message,
            lesson_text=context,
            slide_reading=reading or "",
        ),
    )

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

    if reading:
        system += (
            f"\n\nSLIDE READING (slide {payload.current_slide}):"
            f"\n<slide_reading>\n{reading}\n</slide_reading>"
        )

    # Report what was actually consulted, so the teacher sees an honest source.
    source_ref = title
    if (image_data_url is not None or reading) and payload.current_slide is not None:
        source_ref = f"{title} - slide {payload.current_slide}"

    return system, source_ref


def _rebuild_without_image(user_id: str, payload: AIChatRequest) -> str:
    """The system prompt for a model that cannot see the slide.

    Runs on its own session: the original one closed when the request returned,
    and this is only reached from inside the streaming generator.
    """
    with SessionLocal() as db:
        current = db.get(User, user_id)
        if current is None:
            raise LookupError("user vanished mid-stream")
        lesson = (
            _accessible_lesson(db, current, payload.lesson_id) if payload.lesson_id else None
        )
        project = (
            _accessible_fair_project(db, current, payload.fair_project_id)
            if payload.fair_project_id
            else None
        )
        # `attempted=True`: an image really was rendered and really was not
        # usable, so if the reader also comes back empty the model is told the
        # visual check failed rather than that there was never one to do.
        system, _ = _assemble_system(db, current, payload, lesson, project, None, True)
        return system


@router.get("/health", response_model=AIHealth)
def health(_: User = Depends(get_current_user)) -> AIHealth:
    provider = get_provider()
    chain = getattr(provider, "_providers", None)
    return AIHealth(
        provider=provider.name,
        model=getattr(provider, "model", None),
        ready=provider.name != "mock",
        fallback_chain=[
            f"{p.name} ({p.model})" if p.model else p.name
            for p in (chain if chain else [provider])
        ],
        vision_enabled=slide_vision.enabled(),
        vision_model=settings.gemini_vision_model if slide_vision.enabled() else None,
    )


@router.post("/vision/probe", response_model=VisionProbe)
def vision_probe(_: User = Depends(require_roles(Role.super_admin))) -> VisionProbe:
    """Call the configured vision model once and report exactly what came back.

    A model name that does not resolve is the quietest possible failure - every
    slide reading just returns nothing and teachers get slightly worse answers
    forever. This turns that into a sentence.
    """
    ok, message = slide_vision.probe()
    listed, names, list_message = slide_vision.list_models()
    return VisionProbe(
        enabled=slide_vision.enabled(),
        model=settings.gemini_vision_model,
        ok=ok,
        message=message if ok else f"{message} | models: {list_message}",
        available=names if listed else [],
    )


@router.get("/quota", response_model=AIQuota)
def my_quota(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> AIQuota:
    """The caller's own remaining allowance.

    Deliberately unrestricted by role and scoped to `current` alone: this is
    somebody asking how much of their own quota is left, which is not a
    privileged question, and there is no way to ask it about anyone else.
    """
    kind = "teacher" if current.role == Role.teacher else "admin"
    return AIQuota(**quota_for(db, current, kind))


@router.get("/usage", response_model=AIUsageStats)
def usage(
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.super_admin, Role.school_admin)),
) -> AIUsageStats:
    # Super-admins see every school; school-admins only their own (scoped in the service).
    return AIUsageStats(**usage_stats(db, current))


@router.get("/usage/teachers", response_model=AITeacherUsageReport)
def usage_by_teacher(
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.super_admin)),
) -> AITeacherUsageReport:
    """Every teacher's assistant usage, for the platform owner's usage screen.

    Super-admin only. `/usage` above is the school-scoped rollup a school admin
    is allowed to see; this one names individual teachers across every school,
    which is the platform owner's view alone.
    """
    return AITeacherUsageReport(**teacher_usage_report(db))


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
    was rendered and the provider supports it.

    When the seeing provider drops out — a rate limit, usually — the slide does
    not stop mattering. The prompt is rebuilt without the image, which routes
    the slide through the Gemini reader instead, and a provider that cannot see
    answers from that transcription. The teacher loses the model's own eyes, not
    the contents of the slide.
    """
    provider = get_provider()
    if bundle.image_data_url is not None and getattr(provider, "supports_vision", False):
        emitted = False
        try:
            for chunk in provider.chat_stream_vision(
                bundle.system, bundle.messages, bundle.image_data_url
            ):
                emitted = True
                yield chunk
            return
        except LLMError as exc:
            if emitted:
                # Half an answer is already on the teacher's screen. Starting a
                # second one would splice two different replies together.
                raise
            logger.warning(
                "vision stream failed (%s) - rebuilding without the image", exc.kind
            )

        yield from provider.chat_stream(_without_image(bundle), bundle.messages)
        return

    yield from provider.chat_stream(bundle.system, bundle.messages)


def _without_image(bundle: PromptBundle) -> str:
    """The prompt again, for a model that cannot see. Falls back to the original
    if the rebuild fails — a prompt that overstates what the model can see is
    still better than no answer at all."""
    if bundle.text_fallback is None:
        return bundle.system
    try:
        return bundle.text_fallback()
    except Exception:
        logger.exception("could not rebuild the prompt without the slide image")
        return bundle.system


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
        # This endpoint never attaches the image — `chat()` has no vision path —
        # so it must not use a prompt that says one is attached. Rebuilding
        # routes the slide through the Gemini reader instead, which is the same
        # thing the streaming path does when its seeing provider drops out.
        system = _without_image(bundle) if bundle.image_data_url else bundle.system
        content = provider.chat(system, bundle.messages)
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

    # Held for the generator, which runs after this request's session is closed.
    teacher_id = current.id
    lesson_id = payload.lesson_id if bundle.grounded else None
    question = payload.message
    # Which class this was asked in, so the thread comes back to the right room.
    # An unrecognised class falls back to the teacher's first rather than
    # failing: the answer is already being streamed, and losing the record of it
    # would be a worse outcome than filing it one class over.
    section = ""
    if lesson_id is not None:
        grade = db.scalar(select(Lesson.grade).where(Lesson.id == lesson_id))
        if grade is not None:
            section = (
                resolve_section(current, grade, payload.section)
                or sections_for(current, grade)[0]
            )

    def event_stream():
        answer: list[str] = []
        try:
            if bundle.source_ref:
                yield f"data: {json.dumps({'sourceRef': bundle.source_ref})}\n\n"
            try:
                for delta in _stream_answer(bundle):
                    answer.append(delta)
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
            except Exception as exc:
                logger.warning("teacher chat stream failed: %s", type(exc).__name__)
                yield f"data: {json.dumps({'error': _error_text(exc)})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        finally:
            # Saved even when the teacher closes the tab mid-answer: a partial
            # reply is still what they read, and worth keeping.
            save_exchange(
                teacher_id=teacher_id,
                lesson_id=lesson_id,
                section=section,
                question=question,
                answer="".join(answer),
                source_ref=bundle.source_ref,
            )

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
# The reader already has the tables; asking for six fixed headings only ever
# produced a competent restatement of them. What a report is for is deciding
# what to do next, so that is what is asked for: the few things that need
# attention, each with the figure behind it and the person it belongs to.
_REPORT_RULES = (
    "Rules:\n"
    "- Lead with what needs attention, most urgent first. At most three items.\n"
    "- Every claim carries the number it came from. Never invent or estimate a "
    "figure; if the data does not say, do not say it.\n"
    "- Name the person or the school an item belongs to. An action nobody owns "
    "gets read once and forgotten.\n"
    "- Say what changed since last week wherever the data offers a comparison, "
    "and say plainly when it is the first week of data rather than implying "
    "growth.\n"
    "- Do NOT restate the tables. The reader has them underneath you. Summarise "
    "only to make a point.\n"
    "- If nothing needs attention, say so in one line rather than manufacturing "
    "a concern.\n"
    "- Short paragraphs and '- ' bullets. Under 400 words."
)

_REPORT_SYSTEM = (
    "You are IM-Telligence, writing the opening page of a school's report for its "
    "principal. Use ONLY the SCHOOL DATA provided.\n"
    "Structure it with these markdown headings, in this order:\n"
    "## What needs attention\n## What moved this week\n## Recommended next steps\n"
    f"{_REPORT_RULES}"
)

_PLATFORM_REPORT_SYSTEM = (
    "You are IM-Telligence, writing the opening page of the platform report for "
    "the person who runs every school on it. Use ONLY the PLATFORM DATA provided.\n"
    "Their question is comparative: which schools are moving, which have stalled, "
    "and where their attention is worth spending this week.\n"
    "Structure it with these markdown headings, in this order:\n"
    "## What needs attention\n## How the schools compare\n## What moved this week\n"
    "## Recommended next steps\n"
    f"{_REPORT_RULES}\n"
    "- Name schools explicitly when they differ from the rest, and say what is "
    "different about them rather than only that they are behind."
)

_REPORT_FALLBACK = (
    "## What needs attention\nThe automated narrative is unavailable right now, "
    "but the tables below reflect the current status."
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


@router.post("/super/report")
def super_report(
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.super_admin)),
) -> StreamingResponse:
    """The platform report, with the narrative the school admin has always had.

    The super-admin was the only reader getting tables alone — and they are the
    one deciding which school to spend the week on.
    """
    context = build_platform_context(db)
    system = f"{_PLATFORM_REPORT_SYSTEM}\n\n<PLATFORM DATA>\n{context}\n</PLATFORM DATA>"
    provider = get_provider()
    try:
        enforce_ai_limit(db, current, "admin")
    except AILimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=exc.message
        ) from exc
    try:
        narrative = provider.chat(
            system, [{"role": "user", "content": "Write the platform report now."}]
        )
    except Exception:
        narrative = _REPORT_FALLBACK

    record_ai_usage(db, current, "admin")
    buf, filename = build_super_ai_report(db, current.name, narrative)
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        buf, media_type=DOCX_MEDIA, headers={"Content-Disposition": disposition}
    )
