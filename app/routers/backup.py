"""Super-admin database backup: download the full DB as .db, or email it."""

from __future__ import annotations

import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import EmailStr, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_roles
from app.models import User
from app.models.enums import Role
from app.schemas.base import CamelModel
from app.services.backup import (
    EmailNotConfigured,
    InvalidBackup,
    backup_filename,
    restore_database,
    send_backup_email,
    snapshot_bytes,
    wipe_database,
)

router = APIRouter(prefix="/api/admin/db", tags=["backup"])

DB_MEDIA = "application/octet-stream"


class EmailBackupRequest(CamelModel):
    recipients: list[EmailStr] = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=2000)


class MessageResponse(CamelModel):
    message: str


@router.get("/download")
def download_db(_: User = Depends(require_roles(Role.super_admin))) -> StreamingResponse:
    data = snapshot_bytes()
    filename = backup_filename()
    return StreamingResponse(
        io.BytesIO(data),
        media_type=DB_MEDIA,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/email", response_model=MessageResponse)
def email_db(
    payload: EmailBackupRequest,
    _: User = Depends(require_roles(Role.super_admin)),
) -> MessageResponse:
    data = snapshot_bytes()
    filename = backup_filename()
    recipients = [str(r) for r in payload.recipients]
    try:
        send_backup_email(recipients, data, filename, payload.note)
    except EmailNotConfigured as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send the backup email. Check the SMTP settings and try again.",
        ) from exc
    return MessageResponse(
        message=f"Backup emailed to {len(recipients)} recipient(s): {', '.join(recipients)}"
    )


@router.post("/wipe", response_model=MessageResponse)
def wipe_db(
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.super_admin)),
) -> MessageResponse:
    # Capture the acting admin so they're re-created (never locked out).
    keep = {
        "id": current.id,
        "name": current.name,
        "email": current.email,
        "password_hash": current.password_hash,
        "role": current.role,
        "status": current.status,
    }
    db.rollback()  # release the session's read lock before the write
    try:
        wipe_database(keep)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to wipe the database.",
        ) from exc
    return MessageResponse(
        message="Database wiped. All data was cleared; your super-admin account was kept."
    )


@router.post("/restore", response_model=MessageResponse)
async def restore_db(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles(Role.super_admin)),
) -> MessageResponse:
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload a .db file")
    content = await file.read()
    db.rollback()  # release the session's read lock before the write
    try:
        restored = restore_database(content)
    except InvalidBackup as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restore the database. The file may be incompatible.",
        ) from exc
    return MessageResponse(
        message=(
            f"Database restored from backup ({len(restored)} tables). "
            "You may need to sign in again with an account from the restored backup."
        )
    )
