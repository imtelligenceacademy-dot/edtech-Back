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


# Nothing here is "late". A lesson a teacher has not reached is where they
# are, not a failing, and the value it replaced is cleared by the
# d8b21c60fa73 migration.
class LessonStatus(str, enum.Enum):
    not_started = "not-started"
    in_progress = "in-progress"
    completed = "completed"


class WatchdogStatus(str, enum.Enum):
    on_track = "on-track"
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
    """What actually happened.

    The first five predate the rest. Two of them were being written for things
    they do not name — a wrong password was logged as "new-ip", and an account
    lockout as "blocked-second-device" — so the screen confidently described
    events that had not occurred. The additions below give those their own
    names, which frees "new-ip" and "foreign-device" to finally mean what they
    say: a successful sign-in from an address, or a browser and OS, this user
    has never used before.

    Rows written before that fix keep their old values; there is no way to tell
    from the row alone, so they are left as they were rather than rewritten.
    """

    normal_login = "normal-login"
    foreign_device = "foreign-device"
    new_ip = "new-ip"
    suspicious_location = "suspicious-location"
    blocked_second_device = "blocked-second-device"
    failed_login = "failed-login"
    account_locked = "account-locked"
    # Teachers cannot change their own password; only a super-admin can reset
    # one for them, and that silently ends every session the teacher had open.
    password_reset = "password-reset"
    signed_out_all = "signed-out-all"
    # A super-admin clearing what a teacher had recorded as done. Destructive
    # and not reconstructable, so it is written down with who did it and how
    # much it covered.
    progress_reset = "progress-reset"
    # A lesson PDF handed to an account. The only event here that happens in
    # the ordinary course of teaching rather than at a moment of risk, and so
    # the only one written many times a day: the question it answers is not
    # "did something go wrong" but "does this pattern look like teaching".
    # `list_security_logs` leaves it out unless it is asked for by name.
    lesson_file_served = "lesson-file-served"
    # The same request refused. Rarer and more interesting: a file id that is
    # not yours is not a thing you arrive at by using the site.
    lesson_file_refused = "lesson-file-refused"


class SecurityStatus(str, enum.Enum):
    ok = "ok"
    warning = "warning"
    blocked = "blocked"
