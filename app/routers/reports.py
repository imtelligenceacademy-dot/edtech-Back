from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_capability, require_roles
from app.models import Report, User
from app.models.enums import ReportScope, ReportStatus, Role
from app.schemas.report import ReportCreate, ReportOut
from app.services.report_docx import build_school_report, build_super_report
from app.utils import new_id

router = APIRouter(prefix="/api/reports", tags=["reports"])

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_response(buf, filename: str) -> StreamingResponse:
    # RFC 5987 filename* handles spaces/unicode safely across browsers.
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        buf,
        media_type=DOCX_MEDIA,
        headers={"Content-Disposition": disposition},
    )


@router.get("/school/download")
def download_school_report(
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.school_admin)),
) -> StreamingResponse:
    if not current.school_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No school on account")
    buf, filename = build_school_report(db, current.school_id, current.name)
    return _docx_response(buf, filename)


@router.get("/super/download")
def download_super_report(
    school_id: str | None = None,
    db: Session = Depends(get_db),
    current: User = Depends(require_roles(Role.super_admin)),
) -> StreamingResponse:
    buf, filename = build_super_report(db, current.name, school_id=school_id)
    return _docx_response(buf, filename)


@router.get("", response_model=list[ReportOut])
def list_reports(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[Report]:
    stmt = select(Report)
    if current.role == Role.school_admin:
        stmt = stmt.where(Report.school_id == current.school_id)
    elif current.role == Role.teacher:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
    return list(db.scalars(stmt.order_by(Report.created_at.desc())))


@router.post("", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
def request_report(
    payload: ReportCreate,
    db: Session = Depends(get_db),
    current: User = Depends(require_capability("request-reports")),
) -> Report:
    # School admins can only request reports scoped to their own school.
    school_id = payload.school_id
    if current.role == Role.school_admin:
        school_id = current.school_id
        if payload.scope == ReportScope.global_:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="School admins cannot request global reports",
            )

    # Reports are generated on demand at download time, so a request is
    # immediately fulfillable — mark it ready rather than leaving it "pending".
    report = Report(
        id=new_id("rep"),
        title=payload.title.strip(),
        scope=payload.scope,
        school_id=school_id,
        requested_by=current.id,
        status=ReportStatus.ready,
        ready_at=datetime.now(timezone.utc),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report
