"""ICT Fair: sections and the project PDFs filed under them.

Schools do not share fair projects — each runs its own fair — so a section
belongs to exactly one school and a project's school is its section's. That is
what every read here is scoped by:

- **super-admin** sees every school, and filters to one when working on it.
- **school-admin** sees their own school only.
- **teacher** sees their own school only, and only with `ict_fair_access`.

A project with no section has no school either, so it is shown to super-admins
to be filed and to nobody else. Scoping fails closed rather than guessing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, require_capability
from app.models import FairProject, FairSection, School, UploadedFile, User
from app.models.enums import Role
from app.schemas.fair import (
    FairProjectOut,
    FairProjectUpdate,
    FairSectionCreate,
    FairSectionOut,
    FairSectionUpdate,
)
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


def _visible_school_id(current: User) -> str | None:
    """The single school this user is confined to, or None for super-admins.

    Returning the sentinel `""` would be clever and wrong; a teacher with no
    school is handled by the callers, which show them nothing.
    """
    return None if current.role == Role.super_admin else current.school_id


def _can_see_fair(current: User) -> bool:
    if current.role == Role.teacher and not current.ict_fair_access:
        return False
    # Everyone below super-admin is pinned to a school. Without one there is no
    # scope to apply, so there is nothing they may be shown.
    if current.role != Role.super_admin and not current.school_id:
        return False
    return True


def _serialize(section: FairSection, school_names: dict[str, str]) -> FairSectionOut:
    return FairSectionOut(
        id=section.id,
        school_id=section.school_id,
        school_name=school_names.get(section.school_id),
        title=section.title,
        blurb=section.blurb,
        grades=list(section.grades or []),
        created_at=section.created_at,
        projects=[
            FairProjectOut.model_validate(p)
            for p in sorted(section.projects, key=lambda p: p.created_at or 0)
        ],
    )


# --- Sections --------------------------------------------------------------- #
# Declared before the `/{project_id}` routes below: "sections" would otherwise
# match as a project id and the section routes would never be reached.


@router.get("/sections", response_model=list[FairSectionOut])
def list_sections(
    school_id: str | None = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[FairSectionOut]:
    if not _can_see_fair(current):
        return []

    scope = _visible_school_id(current)
    # A super-admin may filter to one school; everyone else *is* filtered, and
    # a school_id they do not own is ignored rather than honoured.
    effective = scope if scope is not None else school_id

    query = select(FairSection).options(selectinload(FairSection.projects))
    if effective:
        query = query.where(FairSection.school_id == effective)

    sections = list(db.scalars(query.order_by(FairSection.title)))
    school_names = dict(db.execute(select(School.id, School.name)).all())
    return [_serialize(s, school_names) for s in sections]


@router.post("/sections", response_model=FairSectionOut, status_code=status.HTTP_201_CREATED)
def create_section(
    payload: FairSectionCreate,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("upload-files")),
) -> FairSectionOut:
    school = db.get(School, payload.school_id)
    if school is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="School not found"
        )

    section = FairSection(
        id=new_id("fsec"),
        school_id=payload.school_id,
        title=payload.title.strip(),
        blurb=(payload.blurb or "").strip() or None,
        grades=payload.grades,
    )
    db.add(section)
    db.commit()
    db.refresh(section)
    return _serialize(section, {school.id: school.name})


@router.patch("/sections/{section_id}", response_model=FairSectionOut)
def update_section(
    section_id: str,
    payload: FairSectionUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("upload-files")),
) -> FairSectionOut:
    section = db.get(FairSection, section_id)
    if section is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Section not found"
        )

    # Only fields the caller actually sent are touched; an omitted `blurb` keeps
    # its value, while an explicit null clears it.
    sent = payload.model_fields_set
    if "title" in sent and payload.title is not None:
        section.title = payload.title.strip()
    if "blurb" in sent:
        section.blurb = (payload.blurb or "").strip() or None
    if "grades" in sent and payload.grades is not None:
        section.grades = payload.grades

    db.commit()
    db.refresh(section)
    school_names = dict(db.execute(select(School.id, School.name)).all())
    return _serialize(section, school_names)


@router.delete(
    "/sections/{section_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
def delete_section(
    section_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> Response:
    section = db.get(FairSection, section_id)
    if section is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Section not found"
        )

    # Refuses rather than cascading. Deleting a section would take its project
    # PDFs with it, and "delete this heading" should never be how a school's
    # uploads disappear — the projects are moved or deleted first, deliberately.
    count = len(section.projects)
    if count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This section still holds {count} project{'s' if count != 1 else ''}. "
                "Move or delete them first."
            ),
        )

    db.delete(section)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Projects --------------------------------------------------------------- #


@router.get("/unfiled", response_model=list[FairProjectOut])
def list_unfiled_projects(
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> list[FairProject]:
    """Projects with no section — uploaded before sections existed, or left over
    from a move. They belong to no school, so only a super-admin sees them."""
    return list(
        db.scalars(
            select(FairProject)
            .where(FairProject.section_id.is_(None))
            .order_by(FairProject.created_at.desc())
        )
    )


@router.get("", response_model=list[FairProjectOut])
def list_fair_projects(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[FairProject]:
    """Flat list of the projects this user may see, kept for the existing
    teacher screen. Scoped to their school through the section."""
    if not _can_see_fair(current):
        return []

    query = select(FairProject).order_by(FairProject.created_at.desc())
    scope = _visible_school_id(current)
    if scope is not None:
        query = query.join(FairSection, FairProject.section_id == FairSection.id).where(
            FairSection.school_id == scope
        )
    return list(db.scalars(query))


@router.post("", response_model=FairProjectOut, status_code=status.HTTP_201_CREATED)
async def upload_fair_project(
    file: UploadFile = File(...),
    section_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("upload-files")),
) -> FairProject:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed"
        )

    # Validated before the bytes are written, so a bad section never leaves a
    # stray file on disk.
    if section_id and db.get(FairSection, section_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Section not found"
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
        section_id=section_id,
        uploaded_by=current.id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.patch("/{project_id}", response_model=FairProjectOut)
def update_fair_project(
    project_id: str,
    payload: FairProjectUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("upload-files")),
) -> FairProject:
    """Rename a project, or move it between sections — which is also how an
    unfiled project is filed, and therefore how it gains a school."""
    project = db.get(FairProject, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    sent = payload.model_fields_set
    if "title" in sent and payload.title is not None:
        project.title = payload.title.strip()
    if "section_id" in sent:
        if payload.section_id and db.get(FairSection, payload.section_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Section not found"
            )
        project.section_id = payload.section_id or None

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
