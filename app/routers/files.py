"""Lesson file storage. Accepts PDF uploads, persists the bytes to disk under
``settings.upload_dir``, and serves them back through a scoped download
endpoint (super-admin: any file; others: only files linked to a lesson in
their scope).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_capability
from app.models import (
    AccessRequest,
    ChatMessage,
    FairProject,
    Lesson,
    LessonAssignment,
    Progress,
    UploadedFile,
    User,
)
from app.models.enums import LessonStatus, Role
from app.schemas.file import (
    BulkDeleteResult,
    DeletionImpact,
    FileSelection,
    UploadedFileOut,
    UploadPreviewRequest,
    UploadPreviewRow,
    UploadResult,
)
from app.services.auto_assign import assign_uploaded_file, preview_uploads
from app.services.backup import build_files_archive, files_archive_filename
from app.services.file_storage import (
    UploadTooLarge,
    read_upload_capped,
    resolve_stored_file,
    upload_root,
)
from app.services.fair_access import can_open_fair_file, refuse_fair_backed
from app.services.lesson_access import is_lesson_available
from app.utils import new_id

router = APIRouter(prefix="/api/files", tags=["files"])

PDF_CONTENT_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"


def _max_bytes() -> int:
    return settings.max_upload_mb * 1024 * 1024


@router.get("", response_model=list[UploadedFileOut])
def list_files(
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> list[UploadedFile]:
    # Exclude PDFs backing ICT Fair projects — those live in their own section.
    fair_file_ids = select(FairProject.file_id).where(FairProject.file_id.isnot(None))
    return list(
        db.scalars(
            select(UploadedFile)
            .where(UploadedFile.id.notin_(fair_file_ids))
            .order_by(UploadedFile.created_at.desc())
        )
    )


@router.post("/preview", response_model=list[UploadPreviewRow])
def preview_upload(
    payload: UploadPreviewRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> list[UploadPreviewRow]:
    """What would happen if these files were uploaded — nothing is stored.

    The filename is the whole contract for the curriculum pipeline, and its only
    feedback used to arrive after the upload, as a count. This answers the same
    question first, per file, and by name.
    """
    if payload.year not in (1, 2):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Curriculum year must be 1 or 2",
        )
    if payload.language not in ("en", "fr"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Language must be en or fr"
        )

    rows = preview_uploads(db, payload.filenames[:200], payload.language, payload.year)
    return [UploadPreviewRow.model_validate(row, from_attributes=True) for row in rows]


@router.post("", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
# Deliberately not `async`. Everything this does is blocking — a disk
# write, a full scan of users and lessons to work out who the lesson
# belongs to, a pypdf parse for the page count, and a commit — and on the
# event loop all of it ran with nothing else able to make progress. A
# super-admin uploading the curriculum a few hundred files at a time
# therefore stalled every concurrent teacher request behind each one,
# including live assistant streams, which is the whole platform paused by
# one person doing something entirely ordinary. Declared sync, FastAPI
# runs it in a worker thread and the loop stays free.
def upload_file(
    file: UploadFile = File(...),
    language: Literal["en", "fr"] = Form("en"),
    year: int = Form(2),
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("upload-files")),
) -> UploadResult:
    if year not in (1, 2):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Curriculum year must be 1 or 2",
        )

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed"
        )

    try:
        content = read_upload_capped(file.file, _max_bytes())
    except UploadTooLarge:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_upload_mb} MB",
        ) from None
    # Defense in depth: verify it is really a PDF, not just a renamed file.
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File is not a valid PDF"
        )

    file_id = new_id("file")
    stored_name = f"{file_id}.pdf"
    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / stored_name).write_bytes(content)

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
    result = assign_uploaded_file(
        db, uploaded, language=language, uploader_id=current.id, year=year
    )

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


# --------------------------------------------------------------------------- #
# Working on a selection: zip it, weigh it, delete it.
#
# The Files page holds ~400 PDFs. Acting on them one row at a time is the whole
# complaint; these three endpoints take the selection the admin already made and
# do the work in one request.
# --------------------------------------------------------------------------- #
MAX_SELECTION = 2000


def _selected_ids(payload: FileSelection) -> list[str]:
    ids = list(dict.fromkeys(payload.file_ids))
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No files selected"
        )
    if len(ids) > MAX_SELECTION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Select at most {MAX_SELECTION} files at once",
        )
    return ids


def _expand_selection(
    db: Session, file_ids: list[str]
) -> tuple[list[UploadedFile], set[str], int]:
    """Resolve a selection into everything it really touches.

    Selecting one PDF of a lesson takes the lesson — and therefore every other
    PDF filed under it. Returns (files_to_delete, lesson_ids, missing_ids).
    """
    found = list(db.scalars(select(UploadedFile).where(UploadedFile.id.in_(file_ids))))
    missing = len(set(file_ids)) - len(found)

    lesson_ids = {f.linked_lesson_id for f in found if f.linked_lesson_id}
    by_id = {f.id: f for f in found}
    if lesson_ids:
        for sibling in db.scalars(
            select(UploadedFile).where(UploadedFile.linked_lesson_id.in_(lesson_ids))
        ):
            by_id.setdefault(sibling.id, sibling)
    return list(by_id.values()), lesson_ids, missing


@router.post("/deletion-impact", response_model=DeletionImpact)
def deletion_impact(
    payload: FileSelection,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> DeletionImpact:
    """What this selection would destroy, counted before it is confirmed."""
    ids = _selected_ids(payload)
    files, lesson_ids, missing = _expand_selection(db, ids)
    if not lesson_ids:
        return DeletionImpact(files=len(files), missing=missing)

    def _count(model, column) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(column.in_(lesson_ids))) or 0)

    started = set(
        db.scalars(
            select(Progress.lesson_id).where(
                Progress.lesson_id.in_(lesson_ids),
                Progress.status != LessonStatus.not_started,
            )
        )
    )
    teachers = set(
        db.scalars(
            select(LessonAssignment.teacher_id).where(
                LessonAssignment.lesson_id.in_(lesson_ids)
            )
        )
    )
    titles = list(
        db.scalars(select(Lesson.title).where(Lesson.id.in_(lesson_ids)).order_by(Lesson.title))
    )

    return DeletionImpact(
        files=len(files),
        lessons=len(lesson_ids),
        teachers=len(teachers),
        assignments=_count(LessonAssignment, LessonAssignment.lesson_id),
        progress=_count(Progress, Progress.lesson_id),
        chat_messages=_count(ChatMessage, ChatMessage.lesson_id),
        access_requests=_count(AccessRequest, AccessRequest.lesson_id),
        lessons_in_progress=len(started),
        lesson_titles=titles[:40],
        missing=missing,
    )


@router.post("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete(
    payload: FileSelection,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> BulkDeleteResult:
    """Delete a whole selection in one transaction.

    The page used to fire one DELETE per file and swallow the 404s that came
    back for siblings already removed by an earlier one. Doing it here means a
    grade either goes entirely or not at all.
    """
    ids = _selected_ids(payload)
    files, lesson_ids, _missing = _expand_selection(db, ids)
    # Checked on the expanded set, not on what was clicked: selecting one PDF
    # of a lesson pulls in its siblings, and a sibling can be the fair-backed one.
    refuse_fair_backed(db, [f.id for f in files])

    # The rows go first and the bytes second. Unlinking inside the loop put an
    # irreversible disk write ahead of the transaction meant to make this
    # all-or-nothing: a failed commit rolled the rows back and left the PDFs
    # gone, so every lesson still on screen 404'd while the admin was told the
    # delete had failed. This way a failure mid-commit costs nothing, and a
    # failure afterwards leaves an unreferenced file rather than a broken one.
    doomed_paths = [f.storage_path for f in files]
    for uploaded in files:
        db.delete(uploaded)
    # Deleting the Lesson cascades its assignments, progress, chat, access
    # requests and slides.
    deleted_lessons = 0
    for lesson_id in lesson_ids:
        lesson = db.get(Lesson, lesson_id)
        if lesson is not None:
            db.delete(lesson)
            deleted_lessons += 1

    db.commit()
    for stored in doomed_paths:
        _unlink_stored(stored)
    return BulkDeleteResult(deleted_files=len(files), deleted_lessons=deleted_lessons)


@router.post("/archive")
def download_selection(
    payload: FileSelection,
    _: User = Depends(require_capability("upload-files")),
) -> FileResponse:
    """Zip the selected PDFs, foldered by year and grade.

    Saving a grade's worth of lessons used to mean opening each PDF in a tab and
    saving it by hand.
    """
    ids = _selected_ids(payload)
    try:
        path, included, missing = build_files_archive(ids)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build the PDF archive.",
        ) from exc

    if included == 0:
        os.unlink(path)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="None of the selected PDFs are on disk.",
        )

    filename = files_archive_filename(payload.label)
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


def _can_access(db: Session, user: User, uploaded: UploadedFile) -> bool:
    if user.role == Role.super_admin:
        return True
    # ICT Fair PDFs open for teachers who have been granted fair access — and
    # only for the school and grades that teacher actually takes. Scoping the
    # section list without scoping this would hide a project from the screen and
    # still serve it to anyone holding its id.
    fair_file = db.scalar(select(FairProject.id).where(FairProject.file_id == uploaded.id))
    if fair_file is not None:
        # Only teachers have ever opened fair PDFs here; a school-admin
        # administers the fair rather than presents it. Left as it was.
        if user.role != Role.teacher:
            return False
        return can_open_fair_file(db, user, uploaded.id)
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


def _readable_file(db: Session, current: User, file_id: str) -> tuple[UploadedFile, Path]:
    """The lookup and both permission checks the two read routes share."""
    uploaded = db.get(UploadedFile, file_id)
    if uploaded is None or not uploaded.storage_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if not _can_access(db, current, uploaded):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")

    path = resolve_stored_file(uploaded.storage_path)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored file missing")
    return uploaded, path


@router.get("/{file_id}/view")
def view_file(
    file_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> FileResponse:
    """The same bytes as ``/download``, with nothing on them that says "download".

    Download-manager extensions — IDM is the one that reached us — watch for a
    URL ending in ``/download`` and for a ``Content-Disposition`` header, and when
    they see either they claim the request and cancel the page's own. The
    browser's ``fetch`` then rejects with a bare ``TypeError: Failed to fetch``,
    and a teacher mid-lesson sees a red icon over an empty reader.

    So the route the reader uses is named for reading and sends no disposition
    header at all. ``/download`` keeps its filename, because the admin file list
    offers it as a real download and the saved file should be named properly.
    """
    _uploaded, path = _readable_file(db, current, file_id)
    # No `filename=`: Starlette sends Content-Disposition only when given one,
    # and that header is half of what the extension matches on. The test pins it.
    return FileResponse(path, media_type=PDF_CONTENT_TYPE)


@router.get("/{file_id}/download")
def download_file(
    file_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> FileResponse:
    uploaded, path = _readable_file(db, current, file_id)
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


def _delete_file_bytes(uploaded: UploadedFile) -> None:
    """Remove the stored PDF bytes from disk, ignoring if already gone."""
    _unlink_stored(uploaded.storage_path)


def _unlink_stored(storage_path: str | None) -> None:
    """Delete stored bytes by path.

    Takes the path rather than the row because the callers below read it while
    the row is still live and unlink only after the commit — reaching back into
    a deleted ORM instance for it would be a quiet way to end up never
    deleting anything.
    """
    if not storage_path:
        return
    path = resolve_stored_file(storage_path)
    if path is not None:
        path.unlink(missing_ok=True)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_file(
    file_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> Response:
    uploaded = db.get(UploadedFile, file_id)
    if uploaded is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    lesson_id = uploaded.linked_lesson_id
    if lesson_id:
        # The file backs a curriculum lesson — delete the whole lesson so it also
        # disappears from teachers and Access Control. Deleting the Lesson cascades
        # its assignments, progress, access requests, and slides (all ondelete=
        # CASCADE). Remove every PDF backing it (this one + any re-uploads) too.
        doomed = list(
            db.scalars(select(UploadedFile).where(UploadedFile.linked_lesson_id == lesson_id))
        )
    else:
        # Unlinked file (e.g. an ICT Fair file is handled elsewhere; unsorted
        # uploads) — just remove the file itself.
        doomed = [uploaded]

    # Checked against everything this will actually remove rather than the file
    # that was clicked. Deleting one PDF of a lesson takes its siblings with it,
    # and a sibling can be the fair-backed one — so guarding the clicked file
    # alone let the delete reach a fair project sideways, which is the same gap
    # `bulk_delete` was corrected for and this path was not.
    refuse_fair_backed(db, [f.id for f in doomed])

    for f in doomed:
        db.delete(f)
    if lesson_id:
        lesson = db.get(Lesson, lesson_id)
        if lesson is not None:
            db.delete(lesson)
    doomed_paths = [f.storage_path for f in doomed]

    # Bytes after the commit, for the reason given in `bulk_delete`.
    db.commit()
    for stored in doomed_paths:
        _unlink_stored(stored)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
