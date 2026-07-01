"""User administration: listing, creation, and approval/suspension.

Super-admins manage everyone; school-admins are monitoring-only and may only
*view* the teachers in their own school.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_capability
from app.models import School, User
from app.models.enums import Role, UserStatus
from app.schemas.auth import MessageResponse
from app.schemas.user import (
    PasswordReset,
    UserCreate,
    UserOut,
    UserStatusUpdate,
    UserUpdate,
)
from app.security import hash_password
from app.services.auto_assign import prune_teacher_assignments, sync_teacher_assignments
from app.utils import new_id

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[User]:
    stmt = select(User)
    if current.role == Role.super_admin:
        pass
    elif current.role == Role.school_admin:
        stmt = stmt.where(User.school_id == current.school_id, User.role == Role.teacher)
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
    return list(db.scalars(stmt.order_by(User.created_at.desc())))


@router.get("/pending", response_model=list[UserOut])
def list_pending(
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("approve-accounts")),
) -> list[User]:
    return list(
        db.scalars(select(User).where(User.status == UserStatus.pending).order_by(User.created_at))
    )


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("create-users")),
) -> User:
    if db.scalar(select(User).where(User.email == payload.email.lower())):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(
        id=new_id("u"),
        name=payload.name.strip(),
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        status=UserStatus.active,
        school_id=payload.school_id,
        grades=payload.grades if payload.role == Role.teacher else [],
        language=(
            payload.language.value
            if payload.role == Role.teacher and payload.language
            else None
        ),
    )
    db.add(user)
    db.flush()  # persist the user before assignments reference it
    sync_teacher_assignments(db, user)  # catch up on existing matching lessons
    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}/status", response_model=UserOut)
def update_status(
    user_id: str,
    payload: UserStatusUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("approve-accounts")),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot change your own status")
    user.status = payload.status
    db.commit()
    db.refresh(user)
    return user


def _apply_email_change(db: Session, user: User, user_id: str, data: dict) -> None:
    if data.get("email") is None:
        return
    new_email = data["email"].lower()
    clash = db.scalar(select(User).where(User.email == new_email, User.id != user_id))
    if clash:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")
    user.email = new_email


def _apply_role_and_school(db: Session, user: User, data: dict) -> Role:
    """Apply a role change (if any) and reconcile the school link, returning the
    effective role. Super-admins never have a school; other roles may set one."""
    if data.get("role") is not None:
        user.role = data["role"]
    effective_role = data.get("role", user.role)
    if effective_role == Role.super_admin:
        user.school_id = None
    elif "school_id" in data:
        if data["school_id"] is not None and db.get(School, data["school_id"]) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="School not found")
        user.school_id = data["school_id"]
    return effective_role


def _apply_teacher_fields(
    user: User, payload: UserUpdate, data: dict, effective_role: Role
) -> None:
    """Grades and language only apply to teachers; clear them for other roles."""
    if effective_role != Role.teacher:
        user.grades = []
        user.language = None
        return
    if "grades" in data and data["grades"] is not None:
        user.grades = data["grades"]
    if "language" in data and payload.language is not None:
        user.language = payload.language.value


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("create-users")),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    data = payload.model_dump(exclude_unset=True)

    if data.get("name") is not None:
        user.name = data["name"].strip()
    _apply_email_change(db, user, user_id, data)
    effective_role = _apply_role_and_school(db, user, data)
    _apply_teacher_fields(user, payload, data, effective_role)

    # Re-sync rule-based assignments to the teacher's current grades/language:
    # add newly-matching lessons, and strip untouched ones that no longer match.
    if user.role == Role.teacher:
        db.flush()
        sync_teacher_assignments(db, user)
        prune_teacher_assignments(db, user)

    db.commit()
    db.refresh(user)
    return user


@router.post("/{user_id}/reset-password", response_model=MessageResponse)
def reset_password(
    user_id: str,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("create-users")),
) -> MessageResponse:
    """Set a new password for a user. The previous password is irrecoverable by
    design (stored only as an Argon2id hash). Resetting revokes the user's
    refresh tokens so existing sessions must re-authenticate.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.password_hash = hash_password(payload.password)
    for token in user.refresh_tokens:
        token.revoked = True
    db.commit()
    return MessageResponse(message="Password updated")


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("suspend-users")),
) -> Response:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own account")
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
