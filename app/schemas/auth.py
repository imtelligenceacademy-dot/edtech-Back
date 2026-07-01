from __future__ import annotations

from pydantic import EmailStr, Field

from app.models.enums import Role
from app.schemas.base import CamelModel


class LoginRequest(CamelModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class RegisterRequest(CamelModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    # Self-signup is teacher/school-admin only and lands in `pending`.
    role: Role = Role.teacher
    school_id: str | None = None


class SessionUser(CamelModel):
    user_id: str = Field(serialization_alias="userId")
    name: str
    email: EmailStr
    role: Role
    school_id: str | None = None


class MessageResponse(CamelModel):
    message: str
