"""First-run super-admin bootstrap.

A fresh production database has no accounts, so nobody can log in. When
BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD are configured and no
super-admin exists yet, create one active super-admin at startup. This is a
no-op as soon as any super-admin is present, so it is safe to leave configured.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User
from app.models.enums import Role, UserStatus
from app.security import hash_password
from app.utils import new_id

logger = logging.getLogger("app.bootstrap")


def ensure_bootstrap_admin(db: Session) -> None:
    email = settings.bootstrap_admin_email.strip().lower()
    password = settings.bootstrap_admin_password
    if not email or not password:
        return

    # Only ever runs while there are zero super-admins.
    existing_super = db.scalar(
        select(func.count(User.id)).where(User.role == Role.super_admin)
    )
    if existing_super:
        return

    # If the email is taken by a non-super-admin, don't clobber it.
    if db.scalar(select(User.id).where(User.email == email)):
        logger.warning(
            "Bootstrap admin skipped: %s already exists as a non-super-admin.", email
        )
        return

    db.add(
        User(
            id=new_id("u"),
            name=settings.bootstrap_admin_name.strip() or "Super Admin",
            email=email,
            password_hash=hash_password(password),
            role=Role.super_admin,
            status=UserStatus.active,
        )
    )
    db.commit()
    logger.info("Bootstrap super-admin created: %s", email)
