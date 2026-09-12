"""Server-side capability matrix — the authoritative mirror of the frontend's
`lib/permissions.ts`. The frontend uses it for UI gating; the server enforces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.enums import Role
from app.services.grades import is_kindergarten

if TYPE_CHECKING:
    from app.models.user import User

Capability = str

_MATRIX: dict[Role, set[Capability]] = {
    Role.super_admin: {
        "approve-accounts",
        "create-users",
        "suspend-users",
        "upload-files",
        "assign-files",
        "view-all-schools",
        "view-own-school-teachers",
        "request-reports",
        "export-reports",
        "view-global-security",
        "view-school-security",
        "view-teacher-chats",
    },
    Role.school_admin: {
        # Monitoring-only.
        "view-own-school-teachers",
        "request-reports",
        "export-reports",
        "view-school-security",
    },
    Role.teacher: {
        "view-assigned-lessons",
        "use-ai-assistant",
    },
}


def can(role: Role, capability: Capability) -> bool:
    return capability in _MATRIX.get(role, set())


def teaches_only_kindergarten(user: "User") -> bool:
    """True for a teacher whose every grade is a kindergarten one.

    A teacher of KG2 and Grade 1 is not one of these. They still teach the
    curriculum the assistant is grounded in, and taking the assistant away from
    them because of their other class would be a loss with nothing behind it.
    """
    grades = user.grades or []
    return bool(grades) and all(is_kindergarten(g) for g in grades)


def user_can(user: "User", capability: Capability) -> bool:
    """What this particular account may do — the role matrix, then the rules
    that depend on the account rather than the role.

    The matrix answers first and can only ever be narrowed here, so no rule
    below can hand someone a capability their role does not carry.
    """
    if not can(user.role, capability):
        return False
    # Kindergarten runs MTiny, which the assistant has no grounding in: it has
    # read none of those lessons and would be answering from nothing. The
    # interface is not offered rather than offered and disappointing.
    if capability == "use-ai-assistant" and teaches_only_kindergarten(user):
        return False
    return True
