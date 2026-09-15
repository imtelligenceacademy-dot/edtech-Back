from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_capability
from app.models import Lesson, School, User
from app.models.enums import Role
from app.schemas.school import SchoolCreate, SchoolOut, SchoolUpdate
from app.services.auto_assign import resync_school_teachers
from app.utils import new_id

logger = logging.getLogger("app.schools")

router = APIRouter(prefix="/api/schools", tags=["schools"])


def _to_out(db: Session, school: School) -> SchoolOut:
    teacher_count = db.scalar(
        select(func.count(User.id)).where(User.school_id == school.id, User.role == Role.teacher)
    )
    admin_count = db.scalar(
        select(func.count(User.id)).where(
            User.school_id == school.id, User.role == Role.school_admin
        )
    )
    return SchoolOut(
        id=school.id,
        name=school.name,
        country=school.country,
        city=school.city,
        program_year=school.program_year,
        teacher_count=teacher_count or 0,
        admin_count=admin_count or 0,
        created_at=school.created_at,
    )


@router.get("", response_model=list[SchoolOut])
def list_schools(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[SchoolOut]:
    stmt = select(School)
    if current.role != Role.super_admin:
        stmt = stmt.where(School.id == current.school_id)
    return [_to_out(db, s) for s in db.scalars(stmt.order_by(School.name))]


@router.post("", response_model=SchoolOut, status_code=status.HTTP_201_CREATED)
def create_school(
    payload: SchoolCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("view-all-schools")),
) -> SchoolOut:
    school = School(
        id=new_id("sch"),
        name=payload.name.strip(),
        country=payload.country,
        city=payload.city,
        program_year=payload.program_year,
    )
    db.add(school)
    db.commit()
    db.refresh(school)
    return _to_out(db, school)


@router.patch("/{school_id}", response_model=SchoolOut)
def update_school(
    school_id: str,
    payload: SchoolUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("view-all-schools")),
) -> SchoolOut:
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="School not found")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        school.name = data["name"].strip()
    if "country" in data and data["country"] is not None:
        school.country = data["country"]
    if "city" in data and data["city"] is not None:
        school.city = data["city"]

    # Checked before the write, and only when the value actually moves: renaming
    # a school must not churn a year of curriculum across all of its teachers.
    year_changed = (
        data.get("program_year") is not None
        and data["program_year"] != school.program_year
    )
    if year_changed:
        school.program_year = data["program_year"]
        # The field's own help text promises this: "Determines which year's
        # lessons this school's teachers receive". Nothing was re-running the
        # rule, so the promotion changed the label and nothing else — the
        # teachers kept the year they were leaving and never saw the new one.
        db.flush()
        assigned, removed = resync_school_teachers(db, school)
        logger.info(
            "School %s moved to year %s - %s assignment(s) added, %s removed",
            school.id,
            school.program_year,
            assigned,
            removed,
        )

    db.commit()
    db.refresh(school)
    return _to_out(db, school)


@router.delete("/{school_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_school(
    school_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_capability("view-all-schools")),
) -> Response:
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="School not found")

    # Guard against silently orphaning users / cascading away lessons.
    user_count = db.scalar(select(func.count(User.id)).where(User.school_id == school_id)) or 0
    lesson_count = (
        db.scalar(select(func.count(Lesson.id)).where(Lesson.school_id == school_id)) or 0
    )
    if user_count or lesson_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete: school still has {user_count} user(s) and "
                f"{lesson_count} lesson(s). Reassign or remove them first."
            ),
        )

    db.delete(school)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
