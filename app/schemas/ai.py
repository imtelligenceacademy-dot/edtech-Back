from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel


class ChatTurn(CamelModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class AIChatRequest(CamelModel):
    message: str = Field(min_length=1, max_length=4000)
    lesson_id: str | None = None
    fair_project_id: str | None = None
    # 1-based page the teacher is currently viewing, so the assistant can look at
    # that exact slide. Desktop viewers report it; null means "no visual context".
    current_slide: int | None = Field(default=None, ge=1, le=2000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


class AdminChatRequest(CamelModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


class AIChatResponse(CamelModel):
    content: str
    source_ref: str | None = None
    provider: str


class AIHealth(CamelModel):
    provider: str
    model: str | None = None
    ready: bool  # False when falling back to the no-key mock
    # The separate model that reads slide images for the answering model.
    vision_enabled: bool = False
    vision_model: str | None = None


class VisionProbe(CamelModel):
    """Whether the configured vision model actually answers.

    Exists so "does this model name exist?" is a button rather than a guess: a
    name that does not resolve fails silently at the first slide otherwise, and
    the only symptom is slightly worse answers.
    """

    enabled: bool
    model: str
    ok: bool
    message: str
    # Every model this key can use, so a wrong name shows its neighbours.
    available: list[str] = []


class AIUsageStats(CamelModel):
    last7: int  # interactions in the last 7 days
    prev7: int  # interactions in the 7 days before that
    delta_pct: int | None = None  # week-over-week change, None with no baseline
