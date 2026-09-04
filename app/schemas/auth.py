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
    ict_fair_access: bool = False
    # The grades this teacher takes. Empty for admins. Carried on the session so
    # a screen can narrow itself to the teacher's own grades without a second
    # round trip — and because "the grades I teach" is this field, not whichever
    # grades happen to have lessons assigned right now.
    grades: list[str] = []

    # Named classes per grade, e.g. {"G6": ["A", "B"]}. Carried here for the
    # same reason as grades: the teacher surface has to know, before it draws
    # anything, whether this teacher picks a class or goes straight to the
    # lessons. A grade absent here has one unnamed class and shows no section
    # anywhere.
    sections: dict[str, list[str]] = {}
    access_token: str | None = Field(default=None, serialization_alias="accessToken")


class MessageResponse(CamelModel):
    message: str
    access_token: str | None = Field(default=None, serialization_alias="accessToken")
