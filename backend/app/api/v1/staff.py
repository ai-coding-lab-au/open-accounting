"""Staff list API — the people selectable as document signers.

Thin HTTP wrapper over services.staff. Per-company table; soft-delete only
(rows stay referenced by issued Service Agreements).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ...deps import PathId, get_company_db, get_current_company
from ...models.company import StaffMember
from ...models.master import Company
from ...schemas.staff import StaffOut, StaffUpsert
from ...services import staff as staff_service

router = APIRouter(prefix="/staff", tags=["staff"])


def _staff_out(row: StaffMember) -> StaffOut:
    out = StaffOut.model_validate(row)
    out.display_label = staff_service.display_label(row)
    return out


def _staff_error(e: staff_service.StaffServiceError) -> HTTPException:
    return HTTPException(status_code=e.http_status, detail=e.detail)


@router.get("", response_model=list[StaffOut])
def list_staff(
    include_inactive: bool = False,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return [_staff_out(r) for r in staff_service.list_staff(db, include_inactive=include_inactive)]


@router.post("", response_model=StaffOut, status_code=201)
def create_staff(
    payload: StaffUpsert,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        row = staff_service.create_staff(
            db,
            full_name=payload.full_name,
            registration_type=payload.registration_type,
            registration_number=payload.registration_number,
        )
    except staff_service.StaffServiceError as e:
        raise _staff_error(e) from e
    return _staff_out(row)


@router.put("/{staff_id}", response_model=StaffOut)
def update_staff(
    staff_id: PathId,
    payload: StaffUpsert,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        row = staff_service.update_staff(
            db,
            staff_id,
            full_name=payload.full_name,
            registration_type=payload.registration_type,
            registration_number=payload.registration_number,
            active=payload.active,
        )
    except staff_service.StaffServiceError as e:
        raise _staff_error(e) from e
    return _staff_out(row)


@router.delete("/{staff_id}", status_code=204)
def soft_delete_staff(
    staff_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    ok = staff_service.soft_delete_staff(db, staff_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Staff member not found")
    return Response(status_code=204)
