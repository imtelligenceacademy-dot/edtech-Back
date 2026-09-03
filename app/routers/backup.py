"""Super-admin database backup, email, wipe, and restore."""

from __future__ import annotations

import io
import os
from pathlib import PurePath
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import EmailStr, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.database import get_db
from app.deps import require_roles
from app.models import User
from app.models.enums import Role
from app.schemas.base import CamelModel
from app.services.backup import (
    EmailNotConfigured,
    EmailDeliveryFailed,
    InvalidBackup,
    backup_filename,
    backup_upload_hint,
    build_files_archive,
    files_archive_filename,
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


@router.get("/files-archive")
def download_files_archive(
    _: User = Depends(require_roles(Role.super_admin)),
) -> FileResponse:
    """Download every stored lesson/fair PDF as a zip.

    The DB backup holds only metadata and paths, so this is the companion piece
    needed for a full restore. Streamed from a temp file that is deleted once the
    response has been sent.
    """
    try:
        path, included, missing = build_files_archive()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build the PDF archive.",
        ) from exc

    filename = files_archive_filename()
    return FileResponse(
        path,
        media_type="application/zip",
        background=BackgroundTask(lambda: os.unlink(path)),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Files-Included": str(included),
            "X-Files-Missing": str(missing),
        },
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
    except EmailDeliveryFailed as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send the backup email. Check the email provider settings.",
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
    expected = backup_upload_hint()
    suffix = PurePath(file.filename or "").suffix.lower()
    if suffix and suffix != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload a {expected} backup file",
        )
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
