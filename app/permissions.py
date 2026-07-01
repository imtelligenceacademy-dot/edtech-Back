"""Server-side capability matrix — the authoritative mirror of the frontend's
`lib/permissions.ts`. The frontend uses it for UI gating; the server enforces it.
"""

from __future__ import annotations

from app.models.enums import Role

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
