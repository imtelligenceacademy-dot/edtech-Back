"""Super-admin database backup, email, wipe, and restore."""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import PurePath
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import EmailStr, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db
from app.deps import require_roles
from app.models import User
from app.models.enums import Role
from app.schemas.base import CamelModel
from app.services import object_storage
from app.services.backup import (
    backup_database_to_storage,
    backup_files_to_storage,
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


# --- Off-box backups -------------------------------------------------------- #


class StoredBackup(CamelModel):
    key: str
    size_bytes: int
    stored_at: datetime


class StorageStatus(CamelModel):
    """Whether off-box backups are configured, and what is actually up there.

    The listing is the point. "Backups are enabled" is a claim about config; a
    list of objects with sizes and dates is evidence, and it is the only way to
    notice that the scheduler has been silently failing for a month.
    """

    enabled: bool
    bucket: str | None = None
    endpoint: str | None = None
    prefix: str | None = None
    database_interval_hours: int
    files_interval_hours: int
    keep: int
    database_backups: list[StoredBackup] = []
    file_backups: list[StoredBackup] = []
    error: str | None = None


@router.get("/storage", response_model=StorageStatus)
def storage_status(
    _: User = Depends(require_roles(Role.super_admin)),
) -> StorageStatus:
    base = StorageStatus(
        enabled=object_storage.enabled(),
        bucket=settings.backup_storage_bucket or None,
        endpoint=settings.backup_storage_endpoint_url or None,
        prefix=settings.backup_storage_prefix or None,
        database_interval_hours=settings.backup_interval_hours,
        files_interval_hours=settings.backup_files_interval_hours,
        keep=settings.backup_storage_keep,
    )
    if not base.enabled:
        return base

    def rows(kind: str) -> list[StoredBackup]:
        return [
            StoredBackup(key=o.key, size_bytes=o.size, stored_at=o.modified)
            for o in object_storage.list_backups(kind)
        ]

    try:
        base.database_backups = rows("database")
        base.file_backups = rows("files")
    except Exception as exc:
        # Reported rather than raised: a screen that says why it cannot reach
        # the bucket is more useful than a 500 with the reason in a log.
        base.error = str(exc)
    return base


@router.post("/storage/run", response_model=MessageResponse)
def run_storage_backup(
    include_files: bool = True,
    _: User = Depends(require_roles(Role.super_admin)),
) -> MessageResponse:
    """Back up now, rather than waiting for the schedule.

    Exists so the configuration can be proved rather than assumed. A backup you
    have never seen succeed is not a backup, and the alternative is finding out
    on the day you need it.
    """
    if not object_storage.enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Off-box backup storage is not configured. Set BACKUP_STORAGE_ENABLED, "
                "BACKUP_STORAGE_BUCKET, BACKUP_STORAGE_ACCESS_KEY_ID and "
                "BACKUP_STORAGE_SECRET_ACCESS_KEY."
            ),
        )

    try:
        db_key = backup_database_to_storage()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Database backup failed: {exc}",
        ) from exc

    if not include_files:
        return MessageResponse(message=f"Database backed up to {db_key}.")

    try:
        files_key, included, missing = backup_files_to_storage()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Database backed up to {db_key}, but the file archive failed: {exc}",
        ) from exc

    tail = f" {missing} file(s) are recorded but missing on disk." if missing else ""
    return MessageResponse(
        message=(
            f"Database backed up to {db_key}. "
            f"{included} PDF(s) archived to {files_key}.{tail}"
        )
    )
