"""FastAPI dependencies: master/company DB sessions and current-company resolution."""

from __future__ import annotations

from typing import Annotated, Generator

from fastapi import Depends, Header, HTTPException, Path, status
from sqlalchemy.orm import Session

from .db.master import MasterSession
from .db.company import company_session
from .models.master import Company


# Integer primary keys are SQLite signed 64-bit. An id at/above 2**63 overflows
# the driver (OverflowError → 500) before it can miss-and-404. Bounding path ids
# to the valid signed-64 range turns such ids into a 422 at the routing layer.
# Apply to integer path-id params: `id: PathId` (or `id: PathId, ...`).
PathId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def get_master_db() -> Generator[Session, None, None]:
    db = MasterSession()
    try:
        yield db
    finally:
        # Discard any uncommitted work (no-op after a clean commit) so an
        # endpoint that raised mid-transaction never leaks state on close.
        db.rollback()
        db.close()


def _company_from_header(x_company_id: str | None) -> Company:
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Company-Id header",
        )
    with MasterSession() as db:
        company = db.get(Company, x_company_id)
        if company is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Company '{x_company_id}' not found",
            )
        db.expunge(company)
    return company


def get_current_company(
    x_company_id: str = Header(alias="X-Company-Id"),
) -> Company:
    return _company_from_header(x_company_id)


def get_current_company_optional(
    x_company_id: str | None = Header(default=None, alias="X-Company-Id"),
) -> Company | None:
    if x_company_id is None:
        return None
    return _company_from_header(x_company_id)


def get_company_db(
    company: Company = Depends(get_current_company),
) -> Generator[Session, None, None]:
    db = company_session(company.id)
    try:
        yield db
    finally:
        # Same belt-and-suspenders as get_master_db.
        db.rollback()
        db.close()
