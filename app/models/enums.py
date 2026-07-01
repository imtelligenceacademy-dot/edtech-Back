"""Domain enums, kept in sync with the frontend's `types/index.ts`."""

from __future__ import annotations

import enum


class Role(str, enum.Enum):
    super_admin = "super-admin"
    school_admin = "school-admin"
    teacher = "teacher"


class Language(str, enum.Enum):
    """Language of instruction a teacher delivers in (and a lesson is written in)."""

    en = "en"
    fr = "fr"
    both = "both"


class UserStatus(str, enum.Enum):
    active = "active"
    pending = "pending"
    suspended = "suspended"
    rejected = "rejected"


class LessonStatus(str, enum.Enum):
    not_started = "not-started"
    in_progress = "in-progress"
    completed = "completed"
    late = "late"


class WatchdogStatus(str, enum.Enum):
    on_track = "on-track"
    late = "late"
    not_opened = "not-opened"
    completed = "completed"
    needs_attention = "needs-attention"


class ReportStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class ReportScope(str, enum.Enum):
    global_ = "global"
    school = "school"


class SecurityEvent(str, enum.Enum):
    normal_login = "normal-login"
    foreign_device = "foreign-device"
    new_ip = "new-ip"
    suspicious_location = "suspicious-location"
    blocked_second_device = "blocked-second-device"


class SecurityStatus(str, enum.Enum):
    ok = "ok"
    warning = "warning"
    blocked = "blocked"
