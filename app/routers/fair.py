"""ICT Fair projects: super-admin uploads project PDFs; teachers who have the
`ict_fair_access` flag can list and view them. Deliberately separate from the
lesson pipeline — no grades, no sequencing, no completion.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_capability
from app.models import FairProject, UploadedFile, User
from app.models.enums import Role
from app.schemas.fair import FairProjectOut
from app.services.file_storage import resolve_stored_file, upload_root
from app.utils import new_id

router = APIRouter(prefix="/api/fair", tags=["fair"])

PDF_CONTENT_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"


def _max_bytes() -> int:
    return settings.max_upload_mb * 1024 * 1024


def _title_from_filename(filename: str) -> str:
    base = filename
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return base.strip() or "Untitled project"


@router.get("", response_model=list[FairProjectOut])
def list_fair_projects(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[FairProject]:
    # Teachers only see the fair if they've been granted access; admins always do.
    if current.role == Role.teacher and not current.ict_fair_access:
        return []
    return list(db.scalars(select(FairProject).order_by(FairProject.created_at.desc())))


@router.post("", response_model=FairProjectOut, status_code=status.HTTP_201_CREATED)
async def upload_fair_project(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("upload-files")),
) -> FairProject:
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

    project = FairProject(
        id=new_id("fair"),
        title=_title_from_filename(file.filename),
        file_id=file_id,
        uploaded_by=current.id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_fair_project(
    project_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> Response:
    project = db.get(FairProject, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # Remove the backing file (bytes + row), then the project.
    if project.file_id:
        uploaded = db.get(UploadedFile, project.file_id)
        if uploaded is not None:
            if uploaded.storage_path:
                path = resolve_stored_file(uploaded.storage_path)
                if path is not None:
                    path.unlink(missing_ok=True)
            db.delete(uploaded)
    db.delete(project)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
