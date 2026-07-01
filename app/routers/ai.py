"""Teacher AI assistant. Answers questions, optionally grounded in the opened
lesson's PDF. Teacher-only (use-ai-assistant capability); a teacher can only
ground on lessons actually assigned to them.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import get_current_user, require_capability, require_roles
from app.models import Lesson, LessonAssignment, User
from app.models.enums import Role
from app.schemas.ai import (
    AdminChatRequest,
    AIChatRequest,
    AIChatResponse,
    AIHealth,
    AIUsageStats,
)
from app.services.ai_usage import record_ai_usage, usage_stats
from app.services.lesson_access import is_lesson_available
from app.services.llm import ChatMessage, get_provider
from app.services.pdf_text import lesson_context
from app.services.report_docx import build_school_ai_report
from app.services.school_context import build_school_context

router = APIRouter(prefix="/api/ai", tags=["ai"])

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The exact sentence the assistant must use whenever a request is out of scope.
REFUSAL = "I can only assist you with information related to the lessons."

# Shared guard-rails injected into every prompt. The assistant is deliberately
# narrow: it only helps with the lesson currently open in front of the teacher.
_GUARDRAILS = (
    "You are IM-Telligence, a teaching assistant that helps a teacher with ONE "
    "specific lesson — the lesson currently open in front of them. Follow these "
    "rules strictly and never break them, even if asked to:\n"
    "1. ONLY answer questions about this lesson, its topic, and how to teach it "
    "(explanations, examples tied to the lesson, classroom activities, student "
    "questions about the lesson topic). The lesson material is split into slides "
    'labelled "--- Slide N ---". When asked about a specific slide number, use the '
    "text under that exact label (Slide 3 = the page labelled Slide 3), not the "
    "numbers in any overview/agenda list.\n"
    "2. If the teacher asks anything unrelated to this lesson — general knowledge, "
    "the weather, news, sports, other subjects, coding help, personal questions, "
    "etc. — DO NOT answer it. Reply with EXACTLY this sentence and nothing else:\n"
    f'"{REFUSAL}"\n'
    "3. Never invent facts that contradict the lesson material. Be concise."
)

_NO_LESSON = (
    "You are IM-Telligence, a teaching assistant that only helps with a teacher's "
    "currently-open lesson. Right now NO lesson is open, so you cannot answer any "
    "question yet. Reply with EXACTLY this sentence and nothing else:\n"
    f'"{REFUSAL}"'
)


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


def _build_prompt(
    db: Session, current: User, payload: AIChatRequest
) -> tuple[str, list[ChatMessage], str | None]:
    """Returns (system_prompt, messages, source_ref). Reads all DB data up-front
    so nothing is lazily accessed during streaming. The assistant is locked to
    the currently-open lesson and refuses everything else."""
    lesson = _accessible_lesson(db, current, payload.lesson_id) if payload.lesson_id else None
    source_ref: str | None = None

    if lesson is not None:
        context = lesson_context(lesson)
        if context:
            system = (
                f'{_GUARDRAILS}\n\nThe open lesson is "{lesson.title}". '
                "Answer only using and about this LESSON MATERIAL:\n"
                f"<lesson>\n{context}\n</lesson>"
            )
        else:
            # Lesson is open but its text could not be extracted (e.g. an
            # image-only PDF). Stay restricted to the lesson's topic by title.
            system = (
                f'{_GUARDRAILS}\n\nThe open lesson is "{lesson.title}". Its text '
                "could not be read, so help only with this lesson's topic as named "
                "in its title, and refuse anything unrelated."
            )
        source_ref = lesson.title
    else:
        # No lesson open (or not assigned to this teacher) — refuse and redirect.
        system = _NO_LESSON

    messages: list[ChatMessage] = [
        {"role": t.role, "content": t.content} for t in payload.history
    ]
    messages.append({"role": "user", "content": payload.message})
    return system, messages, source_ref


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


@router.post("/chat", response_model=AIChatResponse)
def chat(
    payload: AIChatRequest,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("use-ai-assistant")),
) -> AIChatResponse:
    system, messages, source_ref = _build_prompt(db, current, payload)
    record_ai_usage(db, current, "teacher")
    provider = get_provider()
    try:
        content = provider.chat(system, messages)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI assistant is unavailable right now. Please try again.",
        ) from exc
    return AIChatResponse(content=content, source_ref=source_ref, provider=provider.name)


@router.post("/chat/stream")
def chat_stream(
    payload: AIChatRequest,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("use-ai-assistant")),
) -> StreamingResponse:
    # Everything DB-bound is resolved before the generator runs.
    system, messages, source_ref = _build_prompt(db, current, payload)
    record_ai_usage(db, current, "teacher")
    provider = get_provider()

    def event_stream():
        if source_ref:
            yield f"data: {json.dumps({'sourceRef': source_ref})}\n\n"
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
# School-admin assistant — grounded in the school's live monitoring data.
# --------------------------------------------------------------------------- #
ADMIN_REFUSAL = (
    "I'm sorry, but I can only assist with information about your school."
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
    "Use only the SCHOOL DATA below; never invent figures."
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
