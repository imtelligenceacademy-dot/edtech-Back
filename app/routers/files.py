"""Lesson file storage. Accepts PDF uploads, persists the bytes to disk under
``settings.upload_dir``, and serves them back through a scoped download
endpoint (super-admin: any file; others: only files linked to a lesson in
their scope).
"""

from __future__ import annotations

from pathlib import Path

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_capability
from app.models import Lesson, LessonAssignment, UploadedFile, User
from app.models.enums import Role
from app.schemas.file import UploadedFileOut, UploadResult
from app.services.auto_assign import assign_uploaded_file
from app.services.lesson_access import is_lesson_available
from app.utils import new_id

router = APIRouter(prefix="/api/files", tags=["files"])

PDF_CONTENT_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"
_UPLOAD_ROOT = Path(settings.upload_dir)


def _max_bytes() -> int:
    return settings.max_upload_mb * 1024 * 1024


@router.get("", response_model=list[UploadedFileOut])
def list_files(
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> list[UploadedFile]:
    return list(db.scalars(select(UploadedFile).order_by(UploadedFile.created_at.desc())))


@router.post("", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    language: Literal["en", "fr"] = Form("en"),
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("upload-files")),
) -> UploadResult:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed"
        )

    content = await file.read()
    if len(content) > _max_bytes():
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_upload_mb} MB",
        )
    # Defense in depth: verify it is really a PDF, not just a renamed file.
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File is not a valid PDF"
        )

    file_id = new_id("file")
    stored_name = f"{file_id}.pdf"
    _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    (_UPLOAD_ROOT / stored_name).write_bytes(content)

    uploaded = UploadedFile(
        id=file_id,
        filename=file.filename,
        content_type=PDF_CONTENT_TYPE,
        size_bytes=len(content),
        storage_path=stored_name,
        uploaded_by=current.id,
    )
    db.add(uploaded)
    db.flush()

    # Auto-create the lesson (named as the PDF) and assign matching teachers.
    result = assign_uploaded_file(db, uploaded, language=language, uploader_id=current.id)

    # Record the lesson's slide count (PDF page count) for progress %.
    if result.lesson_id:
        try:
            from io import BytesIO
            from pypdf import PdfReader

            pages = len(PdfReader(BytesIO(content)).pages)
            lesson = db.get(Lesson, result.lesson_id)
            if lesson is not None and pages:
                lesson.slide_count = pages
        except Exception:
            pass

    db.commit()
    db.refresh(uploaded)

    return UploadResult(
        file=UploadedFileOut.model_validate(uploaded),
        lesson_id=result.lesson_id,
        lesson_title=result.lesson_title,
        grade=result.grade_token,
        language=result.language,
        assigned_count=result.assigned_count,
        teacher_names=result.teacher_names,
        note=result.note,
    )


def _can_access(db: Session, user: User, uploaded: UploadedFile) -> bool:
    if user.role == Role.super_admin:
        return True
    if uploaded.linked_lesson_id is None:
        return False
    lesson = db.get(Lesson, uploaded.linked_lesson_id)
    if lesson is None:
        return False
    if user.role == Role.school_admin:
        return lesson.school_id == user.school_id
    # teacher: must be assigned to the lesson AND it must currently be unlocked
    # by the sequential-access rules (their single "current" lesson).
    assigned = db.scalar(
        select(LessonAssignment.id).where(
            LessonAssignment.lesson_id == lesson.id,
            LessonAssignment.teacher_id == user.id,
        )
    )
    if assigned is None:
        return False
    return is_lesson_available(db, user, lesson.id)


@router.get("/{file_id}/download")
def download_file(
    file_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> FileResponse:
    uploaded = db.get(UploadedFile, file_id)
    if uploaded is None or not uploaded.storage_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if not _can_access(db, current, uploaded):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")

    path = _UPLOAD_ROOT / uploaded.storage_path
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored file missing")

    return FileResponse(
        path,
        media_type=PDF_CONTENT_TYPE,
        filename=uploaded.filename,
        content_disposition_type="inline",  # view in the browser, not force-download
    )


@router.patch("/{file_id}/lesson/{lesson_id}", response_model=UploadedFileOut)
def link_file_to_lesson(
    file_id: str,
    lesson_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("assign-files")),
) -> UploadedFile:
    uploaded = db.get(UploadedFile, file_id)
    if uploaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if db.get(Lesson, lesson_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")

    uploaded.linked_lesson_id = lesson_id
    db.commit()
    db.refresh(uploaded)
    return uploaded


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_file(
    file_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> Response:
    uploaded = db.get(UploadedFile, file_id)
    if uploaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    # Remove the bytes from disk too, ignoring if already gone.
    if uploaded.storage_path:
        (_UPLOAD_ROOT / uploaded.storage_path).unlink(missing_ok=True)

    db.delete(uploaded)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
