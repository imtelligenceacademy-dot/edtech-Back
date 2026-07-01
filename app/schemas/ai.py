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


class AIUsageStats(CamelModel):
    last7: int  # interactions in the last 7 days
    prev7: int  # interactions in the 7 days before that
    delta_pct: int | None = None  # week-over-week change, None with no baseline
