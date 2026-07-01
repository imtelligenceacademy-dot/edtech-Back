from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.models.enums import Language, Role, UserStatus
from app.schemas.base import CamelModel

# Canonical grade tokens a teacher may be assigned, in display order.
VALID_GRADES: tuple[str, ...] = (
    "KG1",
    "KG2",
    *(f"G{i}" for i in range(1, 13)),
)


def _clean_grades(value: list[str] | None) -> list[str]:
    if not value:
        return []
    seen: list[str] = []
    for g in value:
        if g in VALID_GRADES and g not in seen:
            seen.append(g)
    # Return in canonical order.
    return [g for g in VALID_GRADES if g in seen]


class UserOut(CamelModel):
    id: str
    name: str
    email: EmailStr
    role: Role
    status: UserStatus
    school_id: str | None = None
    grades: list[str] = Field(default_factory=list)
    language: Language | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class UserCreate(CamelModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    role: Role
    school_id: str | None = None
    grades: list[str] = Field(default_factory=list)
    language: Language | None = None

    @field_validator("grades")
    @classmethod
    def _validate_grades(cls, v: list[str]) -> list[str]:
        return _clean_grades(v)


class UserStatusUpdate(CamelModel):
    status: UserStatus


class UserUpdate(CamelModel):
    """Partial edit of a user's profile fields (not password or status)."""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: EmailStr | None = None
    role: Role | None = None
    school_id: str | None = None
    grades: list[str] | None = None
    language: Language | None = None

    @field_validator("grades")
    @classmethod
    def _validate_grades(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_grades(v)


class PasswordReset(CamelModel):
    """Admin-initiated password reset. The old password is never required or
    revealed — it is stored only as a one-way hash and cannot be recovered.
    """

    password: str = Field(min_length=8, max_length=200)
