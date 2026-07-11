import shutil
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import settings
from ...db.company import (
    company_lifecycle_lock,
    company_session,
    dispose_company_engine,
    init_company_db,
)
from ...db.errors import DataRecoveryRequiredError
from ...deps import get_master_db, require_company_identity
from ...models.master import Company
from ...models.company import BankTransaction
from ...schemas.company import CompanyCreate, CompanyOut, CompanyUpdate
from ...services.bank_accounts import seed_default_bank_accounts
from ...services.chart_of_accounts import seed_default_coa
from ...services.gst_policy import has_recorded_gst
from ...services import trial_balance as trial_balance_svc


router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyOut])
def list_companies(db: Session = Depends(get_master_db)):
    return db.query(Company).order_by(Company.created_at.asc()).all()


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def create_company(payload: CompanyCreate, db: Session = Depends(get_master_db)):
    with company_lifecycle_lock(payload.id):
        return _create_company_locked(payload, db)


def _create_company_locked(payload: CompanyCreate, db: Session):
    if db.get(Company, payload.id) is not None:
        raise HTTPException(status_code=409, detail=f"Company id '{payload.id}' already exists")
    company_dir = settings.company_dir(payload.id)
    if company_dir.exists():
        raise DataRecoveryRequiredError(
            f"Refusing to create company '{payload.id}' over the existing "
            f"unregistered directory {company_dir}. Recover or relocate that "
            "directory before retrying."
        )
    company = Company(**payload.model_dump())
    db.add(company)

    # Provision per-company database + seed default CoA + default bank accounts
    # BEFORE committing the master row: a provisioning failure must not leave a
    # master record pointing at a half-initialised ledger (retry would 409).
    # The target directory was proven absent above. Any failure before master
    # commit can therefore clean only this attempt's files and retry from zero.
    try:
        init_company_db(company.id, allow_create=True)
        with company_session(company.id) as csession:
            seed_default_coa(csession)
            seed_default_bank_accounts(csession)
    except DataRecoveryRequiredError:
        # A directory may have appeared after our absence check (for example a
        # second process won the mkdir race). It is not proven to belong to this
        # request, so never pass it to the cleanup routine.
        db.rollback()
        dispose_company_engine(company.id)
        raise
    except Exception:
        try:
            db.rollback()
        finally:
            _cleanup_failed_provisioning(company.id, company_dir)
        raise

    try:
        db.commit()
    except IntegrityError:
        try:
            db.rollback()
        finally:
            _cleanup_failed_provisioning(company.id, company_dir)
        raise HTTPException(
            status_code=409, detail=f"Company id '{payload.id}' already exists"
        ) from None
    except Exception as commit_error:
        rollback_error: Exception | None = None
        try:
            db.rollback()
        except Exception as exc:
            rollback_error = exc
        finally:
            dispose_company_engine(company.id)
        raise DataRecoveryRequiredError(
            f"Master commit outcome is uncertain while creating company "
            f"'{company.id}'. Its newly provisioned ledger was preserved at "
            f"{company_dir}; reconcile the master registry before retrying."
        ) from (rollback_error or commit_error)
    db.refresh(company)

    return company


def _cleanup_failed_provisioning(company_id: str, company_dir) -> None:
    """Remove only the directory proven absent before this create attempt."""
    dispose_company_engine(company_id)
    if not company_dir.exists():
        return
    try:
        shutil.rmtree(company_dir)
    except Exception as cleanup_error:
        raise DataRecoveryRequiredError(
            f"Company '{company_id}' provisioning failed and its newly-created "
            f"directory could not be cleaned: {company_dir}. The master row was "
            "not committed; preserve the directory for operator recovery."
        ) from cleanup_error


@router.get("/{company_id}", response_model=CompanyOut)
def get_company(
    company_id: str,
    x_company_id: str | None = Header(default=None, alias="X-Company-Id"),
    x_company_generation: str | None = Header(
        default=None, alias="X-Company-Generation"
    ),
    db: Session = Depends(get_master_db),
):
    with company_lifecycle_lock(company_id):
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        require_company_identity(company, x_company_id, x_company_generation)
        return company


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(
    company_id: str,
    payload: CompanyUpdate,
    x_company_id: str | None = Header(default=None, alias="X-Company-Id"),
    x_company_generation: str | None = Header(
        default=None, alias="X-Company-Generation"
    ),
    db: Session = Depends(get_master_db),
):
    with company_lifecycle_lock(company_id):
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        require_company_identity(company, x_company_id, x_company_generation)
        changes = payload.model_dump(exclude_unset=True)
        if "books_locked_through" in changes:
            proposed_lock = changes["books_locked_through"]
            if proposed_lock is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The accounting period lock cannot be cleared. Restore a "
                        "backup or record an adjustment in an open period."
                    ),
                )
            if proposed_lock > date.today():
                raise HTTPException(
                    status_code=422,
                    detail="Accounting periods cannot be locked beyond today.",
                )
            if (
                company.books_locked_through is not None
                and proposed_lock < company.books_locked_through
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The accounting period lock can only move forward; it "
                        f"already covers {company.books_locked_through.isoformat()}."
                    ),
                )
            with company_session(company_id) as company_db:
                unreconciled_count = (
                    company_db.query(BankTransaction.id)
                    .filter(
                        BankTransaction.occurred_at <= proposed_lock,
                        BankTransaction.account_id.is_(None),
                    )
                    .count()
                )
                if unreconciled_count:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Cannot lock accounting periods through "
                            f"{proposed_lock.isoformat()}: {unreconciled_count} "
                            "bank transaction(s) in that period are still "
                            "uncategorised. Reconcile or delete them first."
                        ),
                    )
                trial_balance = trial_balance_svc.trial_balance(
                    company_db, as_of=proposed_lock
                )
                if not trial_balance["is_balanced"]:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Cannot lock accounting periods through "
                            f"{proposed_lock.isoformat()}: the Trial Balance is "
                            f"out by {trial_balance['diff']}. Correct the "
                            "ledger in the open period before locking it."
                        ),
                    )
        if company.books_locked_through is not None and "fy_start_month" in changes:
            if changes["fy_start_month"] != company.fy_start_month:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The financial-year start month cannot change after an "
                        "accounting period has been locked."
                    ),
                )
        if (
            company.gst_registered
            and changes.get("gst_registered") is False
        ):
            with company_session(company_id) as company_db:
                if has_recorded_gst(company_db):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Cannot mark this company as not GST-registered while "
                            "historical GST amounts or GST control-account postings "
                            "exist. Preserve the registered setting or correct the "
                            "historical records explicitly first."
                        ),
                    )
        for field, value in changes.items():
            setattr(company, field, value)
        db.commit()
        db.refresh(company)
        return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(
    company_id: str,
    confirm: str,
    x_company_id: str | None = Header(default=None, alias="X-Company-Id"),
    x_company_generation: str | None = Header(
        default=None, alias="X-Company-Generation"
    ),
    db: Session = Depends(get_master_db),
):
    """Permanently delete a company: its per-company DB, attachments, and the
    master registry row. Irreversible — there is no soft-delete or recycle bin.

    The caller must pass ?confirm=<company_id> matching the path id. This guard
    makes an accidental delete (a mistyped curl, a stray click that didn't go
    through the typed confirmation dialog) fail loudly instead of wiping books.
    """
    with company_lifecycle_lock(company_id):
        return _delete_company_locked(
            company_id,
            confirm,
            x_company_id,
            x_company_generation,
            db,
        )


def _delete_company_locked(
    company_id: str,
    confirm: str,
    x_company_id: str | None,
    x_company_generation: str | None,
    db: Session,
):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    require_company_identity(company, x_company_id, x_company_generation)
    if confirm != company_id:
        raise HTTPException(
            status_code=400,
            detail="Delete not confirmed: pass ?confirm=<company_id> matching the company id.",
        )

    # Stage the complete directory to a same-volume sibling before touching
    # master. The rename is reversible if the registry commit fails; destructive
    # cleanup happens only after the master row is durably removed.
    dispose_company_engine(company_id)
    company_dir = settings.company_dir(company_id)
    books_path = settings.company_db_path(company_id)
    if not books_path.is_file():
        raise DataRecoveryRequiredError(
            f"Cannot delete registered company '{company_id}' because its "
            f"books database is missing: {books_path}. Restore the ledger before "
            "retrying deletion so registry and data cannot diverge."
        )
    staged_dir = company_dir.with_name(
        f".{company_id}.deleting-{company.generation_id}"
    )
    if staged_dir.exists():
        raise DataRecoveryRequiredError(
            f"Cannot stage deletion for company '{company_id}': recovery path "
            f"already exists at {staged_dir}. Resolve the prior interrupted "
            "deletion first."
        )
    company_dir.replace(staged_dir)

    try:
        db.delete(company)
        _commit_company_delete(db)
    except Exception as commit_error:
        rollback_error: Exception | None = None
        try:
            db.rollback()
        except Exception as exc:
            rollback_error = exc
        try:
            if company_dir.exists() or not staged_dir.exists():
                raise RuntimeError("staged company directory is not restorable")
            staged_dir.replace(company_dir)
        except Exception as restore_error:
            raise DataRecoveryRequiredError(
                f"Master deletion failed for company '{company_id}', and its "
                f"staged directory could not be restored from {staged_dir} to "
                f"{company_dir}. Preserve both paths for operator recovery."
            ) from restore_error
        if rollback_error is not None:
            raise DataRecoveryRequiredError(
                f"Master deletion failed for company '{company_id}'. Its data "
                f"directory was restored to {company_dir}, but the registry "
                "transaction could not be rolled back cleanly; verify master.db "
                "before retrying."
            ) from rollback_error
        raise commit_error

    try:
        shutil.rmtree(staged_dir)
    except Exception as cleanup_error:
        raise DataRecoveryRequiredError(
            f"Company '{company_id}' was removed from the master registry, but "
            f"its staged data remains at {staged_dir}. Preserve it until an "
            "operator confirms backup and cleanup."
        ) from cleanup_error
    return None


def _commit_company_delete(db: Session) -> None:
    """Fault-injection seam for atomic registry/file deletion tests."""
    db.commit()
