"""FastAPI dependencies: master/company DB sessions and current-company resolution."""

from __future__ import annotations

from typing import Annotated, Generator

from fastapi import Depends, Header, HTTPException, Path, status
from sqlalchemy.orm import Session

from .db.master import MasterSession, require_master_db_file
from .db.company import company_lifecycle_lock, company_session
from .models.master import Company


# Integer primary keys are SQLite signed 64-bit. An id at/above 2**63 overflows
# the driver (OverflowError → 500) before it can miss-and-404. Bounding path ids
# to the valid signed-64 range turns such ids into a 422 at the routing layer.
# Apply to integer path-id params: `id: PathId` (or `id: PathId, ...`).
PathId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def get_master_db() -> Generator[Session, None, None]:
    require_master_db_file()
    db = MasterSession()
    try:
        yield db
    finally:
        # Discard any uncommitted work (no-op after a clean commit) so an
        # endpoint that raised mid-transaction never leaks state on close.
        db.rollback()
        db.close()


def require_company_generation(
    company: Company,
    x_company_generation: str | None,
) -> Company:
    """Reject a request from a missing or stale company workspace."""
    if not x_company_generation:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MISSING_COMPANY_GENERATION",
                "message": "Missing X-Company-Generation header",
            },
        )
    if x_company_generation != company.generation_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "COMPANY_GENERATION_MISMATCH",
                "message": "This company workspace is stale; reload the company list.",
            },
        )
    return company


def require_company_identity(
    company: Company,
    x_company_id: str | None,
    x_company_generation: str | None,
) -> Company:
    """Validate both halves of a company workspace identity."""
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Company-Id header",
        )
    if x_company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "COMPANY_ID_MISMATCH",
                "message": "The company header does not match the requested company.",
            },
        )
    return require_company_generation(company, x_company_generation)


def _company_from_header(
    x_company_id: str | None,
    x_company_generation: str | None,
) -> Company:
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Company-Id header",
        )
    require_master_db_file()
    with MasterSession() as db:
        company = db.get(Company, x_company_id)
        if company is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Company '{x_company_id}' not found",
            )
        require_company_identity(company, x_company_id, x_company_generation)
        db.expunge(company)
    return company


def get_current_company(
    x_company_id: str = Header(alias="X-Company-Id"),
    x_company_generation: str | None = Header(
        default=None, alias="X-Company-Generation"
    ),
) -> Company:
    return _company_from_header(x_company_id, x_company_generation)


def get_current_company_optional(
    x_company_id: str | None = Header(default=None, alias="X-Company-Id"),
    x_company_generation: str | None = Header(
        default=None, alias="X-Company-Generation"
    ),
) -> Company | None:
    if x_company_id is None:
        if x_company_generation is not None:
            return _company_from_header(None, x_company_generation)
        return None
    return _company_from_header(x_company_id, x_company_generation)


def get_company_db(
    company: Company = Depends(get_current_company),
) -> Generator[Session, None, None]:
    # The first lookup happened before dependency resolution reached here.
    # Wait for any concurrent delete/re-create, then validate again while
    # holding the same lifecycle lock used by company management. Holding it
    # through ``yield`` prevents replacement of the DB under an active request.
    with company_lifecycle_lock(company.id):
        current = _company_from_header(company.id, company.generation_id)
        # FastAPI caches get_current_company for the whole request, so route
        # handlers receive this same detached object.  The first snapshot may
        # have waited behind a concurrent company-profile update (notably a
        # GST-registration change).  Refresh every mapped field while holding
        # the lifecycle lock so policy checks in the handler cannot use stale
        # master data after the company DB has been opened under the new state.
        for column in Company.__table__.columns:
            setattr(company, column.key, getattr(current, column.key))
        db = company_session(current.id)
        try:
            yield db
        finally:
            # Same belt-and-suspenders as get_master_db.
            db.rollback()
            db.close()
