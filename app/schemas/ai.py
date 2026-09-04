from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel


class ChatTurn(CamelModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class AIChatRequest(CamelModel):
    message: str = Field(min_length=1, max_length=4000)
    lesson_id: str | None = None
    # The class being taught. Threads are per class, so the same lesson taught
    # to 6A and 6B keeps two conversations. Omitted by teachers with one class.
    section: str | None = None
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
    # The whole chain, primary first, as actually assembled — providers whose
    # key is missing are already dropped. This is how a deployment is checked:
    # a fallback you meant to configure and did not simply is not in the list.
    fallback_chain: list[str] = []
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


class AIUsageDay(CamelModel):
    """One calendar day in the report timezone, and what was asked that day."""

    date: str  # YYYY-MM-DD
    count: int


class AITeacherUsage(CamelModel):
    """One teacher's AI-assistant usage.

    Every field is a count or a timestamp. There is no score and no band: the
    screen showing this is meant to report what happened, and a teacher who
    asked three questions is three questions, not "light use".
    """

    teacher_id: str
    name: str
    email: str
    status: str
    school_id: str | None = None
    school_name: str | None = None
    grades: list[str] = []

    total: int  # all time
    today: int  # since midnight in the report timezone
    last_hour: int  # rolling 60 minutes — the hourly quota's own window
    # Pinned alias: the camelCase generator reads the "h" as a new word after a
    # digit and emits "last24H", which the frontend does not ask for.
    last24h: int = Field(alias="last24h")  # rolling 24h — the daily quota's window
    last7: int
    prev7: int  # the 7 days before that, for a like-for-like comparison
    last30: int
    active_days30: int  # days with at least one question, of the last 30

    first_used_at: datetime | None = None
    last_used_at: datetime | None = None

    hourly_used: int
    daily_used: int

    daily: list[AIUsageDay] = []


class AITeacherUsageReport(CamelModel):
    """Per-teacher usage plus the exact boundaries every count was taken from.

    The boundaries are part of the payload so the screen can say "since 2:00 PM"
    rather than "recently". A window the reader cannot see the edges of is a
    number they cannot check.
    """

    generated_at: datetime
    timezone: str  # IANA name the day buckets were cut in

    today_start: datetime
    hour_start: datetime
    day_start: datetime
    week_start: datetime
    prev_week_start: datetime
    window_start: datetime
    daily_days: int

    hourly_limit: int  # 0 means no limit is enforced
    daily_limit: int

    teachers: list[AITeacherUsage] = []


class AIQuota(CamelModel):
    """What the signed-in user has left, and when the next slot frees up.

    `remaining` is null when no limit is configured (a limit of 0 disables it),
    which the screen says outright rather than rendering as "0 left".
    `resetsAt` is null until the window is actually full — below the limit there
    is nothing to wait for.
    """

    kind: str  # "teacher" or "admin"

    hourly_limit: int
    hourly_used: int
    hourly_remaining: int | None = None
    hourly_resets_at: datetime | None = None

    daily_limit: int
    daily_used: int
    daily_remaining: int | None = None
    daily_resets_at: datetime | None = None
