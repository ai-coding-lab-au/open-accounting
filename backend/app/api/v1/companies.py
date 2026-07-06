import shutil

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import settings
from ...db.company import company_session, dispose_company_engine, init_company_db
from ...deps import get_master_db
from ...models.master import Company
from ...schemas.company import CompanyCreate, CompanyOut, CompanyUpdate
from ...services.bank_accounts import seed_default_bank_accounts
from ...services.chart_of_accounts import seed_default_coa


router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyOut])
def list_companies(db: Session = Depends(get_master_db)):
    return db.query(Company).order_by(Company.created_at.asc()).all()


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def create_company(payload: CompanyCreate, db: Session = Depends(get_master_db)):
    if db.get(Company, payload.id) is not None:
        raise HTTPException(status_code=409, detail=f"Company id '{payload.id}' already exists")
    company = Company(**payload.model_dump())
    db.add(company)

    # Provision per-company database + seed default CoA + default bank accounts
    # BEFORE committing the master row: a provisioning failure must not leave a
    # master record pointing at a half-initialised ledger (retry would 409).
    # init_company_db + the seeders are idempotent, so a retry after a partial
    # provision simply completes it.
    try:
        init_company_db(company.id)
        with company_session(company.id) as csession:
            seed_default_coa(csession)
            seed_default_bank_accounts(csession)
        db.commit()
    except IntegrityError:
        # Concurrent create of the same id: the loser's commit hits the PK —
        # surface the same 409 as the sequential duplicate check above.
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"Company id '{payload.id}' already exists"
        ) from None
    except Exception:
        db.rollback()
        raise
    db.refresh(company)

    return company


@router.get("/{company_id}", response_model=CompanyOut)
def get_company(company_id: str, db: Session = Depends(get_master_db)):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(
    company_id: str,
    payload: CompanyUpdate,
    db: Session = Depends(get_master_db),
):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(
    company_id: str,
    confirm: str,
    db: Session = Depends(get_master_db),
):
    """Permanently delete a company: its per-company DB, attachments, and the
    master registry row. Irreversible — there is no soft-delete or recycle bin.

    The caller must pass ?confirm=<company_id> matching the path id. This guard
    makes an accidental delete (a mistyped curl, a stray click that didn't go
    through the typed confirmation dialog) fail loudly instead of wiping books.
    """
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    if confirm != company_id:
        raise HTTPException(
            status_code=400,
            detail="Delete not confirmed: pass ?confirm=<company_id> matching the company id.",
        )

    # Order matters. Release the cached engine (and its WAL file handle) first,
    # then remove files, then the registry row LAST: if the file removal fails,
    # the master row still points at the (partially-present) data instead of
    # leaving an orphaned folder with no registry entry.
    dispose_company_engine(company_id)
    company_dir = settings.company_dir(company_id)
    if company_dir.exists():
        shutil.rmtree(company_dir)
    db.delete(company)
    db.commit()
    return None
