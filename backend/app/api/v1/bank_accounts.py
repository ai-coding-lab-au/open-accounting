"""Bank accounts router (M3).

The bank account accepts manual entries (POST/PATCH/DELETE on
.../transactions) so the operator can record rent, salary, supplier
payments, etc. without an invoice round-trip.
"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from sqlalchemy.orm import Session, selectinload

from ...db.company import begin_sqlite_immediate
from ...deps import PathId, get_company_db, get_current_company
from ...models.company import (
    BankAccount,
    BankTransaction,
    BankTxnDirection,
    InvoicePaymentAllocation,
)
from ...models.master import Company
from ...schemas.bank_import import (
    BankImportCommitIn,
    BankImportCommitOut,
    BankImportPreviewOut,
)
from ...schemas._dates import current_date
from ...schemas.bank import (
    BankAccountOut,
    BankAccountCreate,
    BankAccountUpdate,
    BankAccountWithBalance,
    BankTransactionIn,
    BankTransactionOut,
    BankTransactionRecategorise,
)
from ...services import bank_accounts as bank_accounts_svc
from ...services import bank_import as bank_import_svc
from ...services import period_lock
from ...services.bank_accounts import bank_account_balance


def _map_bank_error(e: bank_accounts_svc.BankTxnError) -> HTTPException:
    return HTTPException(status_code=e.http_status, detail=str(e))


def _require_open_period(company: Company, value, *, operation: str) -> None:
    try:
        period_lock.require_open_date(company, value, operation=operation)
    except period_lock.AccountingPeriodLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


router = APIRouter(prefix="/bank-accounts", tags=["bank-accounts"])

_MAX_IMPORT_UPLOAD_BYTES = 25 * 1024 * 1024
_IMPORT_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_import_upload_capped(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total_size = 0
    while True:
        chunk = await file.read(_IMPORT_UPLOAD_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total_size += len(chunk)
        if total_size > _MAX_IMPORT_UPLOAD_BYTES:
            raise HTTPException(
                413,
                "Bank statement is too large. Maximum upload size is "
                f"{_MAX_IMPORT_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
        chunks.append(chunk)


@router.get("", response_model=list[BankAccountWithBalance])
def list_bank_accounts(
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    accounts = db.query(BankAccount).order_by(BankAccount.id.asc()).all()
    return [
        BankAccountWithBalance(
            **BankAccountOut.model_validate(a).model_dump(),
            current_balance=bank_account_balance(db, a, as_of=current_date()),
        )
        for a in accounts
    ]


@router.post("", response_model=BankAccountOut, status_code=201)
def create_bank_account(
    payload: BankAccountCreate,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    if payload.opening_balance and company.books_locked_through is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "A non-zero opening balance cannot be introduced after an "
                "accounting period has been locked. Record a dated journal "
                "adjustment in an open period instead."
            ),
        )
    try:
        return bank_accounts_svc.create_account(
            db,
            name=payload.name,
            opening_balance=payload.opening_balance,
            bsb=payload.bsb,
            account_number=payload.account_number,
            is_active=payload.is_active,
        )
    except bank_accounts_svc.BankTxnError as e:
        raise _map_bank_error(e)


@router.patch("/{bank_account_id}", response_model=BankAccountOut)
def update_bank_account(
    bank_account_id: PathId,
    payload: BankAccountUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return bank_accounts_svc.update_account(
            db,
            bank_account_id=bank_account_id,
            **payload.model_dump(exclude_unset=True),
        )
    except bank_accounts_svc.BankTxnError as e:
        raise _map_bank_error(e)


@router.get("/{bank_account_id}/transactions", response_model=list[BankTransactionOut])
def list_transactions(
    bank_account_id: PathId,
    limit: int | None = Query(default=None, ge=1, le=10000, description="Optional row cap; default returns all transactions for the account"),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    if db.get(BankAccount, bank_account_id) is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    query = (
        db.query(BankTransaction)
        .options(
            selectinload(BankTransaction.invoice_allocations).selectinload(
                InvoicePaymentAllocation.tax_components
            )
        )
        .filter(BankTransaction.bank_account_id == bank_account_id)
        .order_by(BankTransaction.occurred_at.desc(), BankTransaction.id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


@router.post(
    "/{bank_account_id}/transactions",
    response_model=BankTransactionOut,
    status_code=201,
)
def create_manual_transaction(
    bank_account_id: PathId,
    payload: BankTransactionIn,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    direction = BankTxnDirection(payload.direction)
    payload_hash = bank_accounts_svc.manual_transaction_payload_hash(
        bank_account_id=bank_account_id,
        direction=direction,
        amount=payload.amount,
        occurred_at=payload.occurred_at,
        memo=payload.memo,
        counter_party_name=payload.counter_party_name,
        account_id=payload.account_id,
        gst_amount=payload.gst_amount,
        tax_code=payload.tax_code,
        invoice_allocations=payload.invoice_allocations,
        unapplied_account_id=payload.unapplied_account_id,
    )
    begin_sqlite_immediate(db)
    try:
        replay = bank_accounts_svc.replay_manual_transaction(
            db,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if replay is not None:
            return replay
        _require_open_period(
            company,
            payload.occurred_at,
            operation="record a bank transaction",
        )
        txn = bank_accounts_svc.record_manual_transaction(
            db,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            bank_account_id=bank_account_id,
            direction=direction,
            amount=payload.amount,
            occurred_at=payload.occurred_at,
            memo=payload.memo,
            counter_party_name=payload.counter_party_name,
            account_id=payload.account_id,
            gst_amount=payload.gst_amount,
            tax_code=payload.tax_code,
            invoice_allocations=payload.invoice_allocations,
            unapplied_account_id=payload.unapplied_account_id,
            gst_registered=company.gst_registered,
        )
    except bank_accounts_svc.BankTxnError as e:
        raise _map_bank_error(e)
    return txn


@router.delete("/transactions/{txn_id}", status_code=204)
def delete_manual_transaction(
    txn_id: PathId,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    txn = db.get(BankTransaction, txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    _require_open_period(
        company,
        txn.occurred_at,
        operation="delete a bank transaction",
    )
    try:
        bank_accounts_svc.delete_manual_transaction(db, txn_id=txn_id)
    except bank_accounts_svc.BankTxnError as e:
        raise _map_bank_error(e)


@router.patch(
    "/transactions/{txn_id}/categorise",
    response_model=BankTransactionOut,
)
def recategorise_transaction(
    txn_id: PathId,
    payload: BankTransactionRecategorise,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    txn = db.get(BankTransaction, txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    _require_open_period(
        company,
        txn.occurred_at,
        operation="recategorise a bank transaction",
    )
    begin_sqlite_immediate(db)
    try:
        # exclude_unset: an omitted account_id keeps the current category;
        # an explicit account_id=null de-categorises.
        return bank_accounts_svc.recategorise_transaction(
            db,
            txn_id=txn_id,
            gst_registered=company.gst_registered,
            **payload.model_dump(exclude_unset=True),
        )
    except bank_accounts_svc.BankTxnError as e:
        raise _map_bank_error(e)


@router.get(
    "/transactions/uncategorised",
    response_model=list[BankTransactionOut],
)
def list_uncategorised_transactions(
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    """Bank txns with no account_id set yet, sorted oldest first."""
    rows = (
        db.query(BankTransaction)
        .options(
            selectinload(BankTransaction.invoice_allocations).selectinload(
                InvoicePaymentAllocation.tax_components
            )
        )
        .filter(BankTransaction.account_id.is_(None))
        .order_by(BankTransaction.occurred_at.asc(), BankTransaction.id.asc())
        .limit(500)
        .all()
    )
    return rows


# ---------------------------------------------------------------------------
# Bank statement import (M3)
# ---------------------------------------------------------------------------


@router.post(
    "/{bank_account_id}/import/preview",
    response_model=BankImportPreviewOut,
)
async def import_preview(
    bank_account_id: PathId,
    file: UploadFile = File(...),
    bank_format: str | None = Form(default=None),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    content = await _read_import_upload_capped(file)
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        return bank_import_svc.preview_import(
            db,
            bank_account_id=bank_account_id,
            content=content,
            filename=file.filename or "upload.csv",
            bank_format=bank_format,
            gst_registered=company.gst_registered,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post(
    "/{bank_account_id}/import/commit",
    response_model=BankImportCommitOut,
)
def import_commit(
    bank_account_id: PathId,
    payload: BankImportCommitIn,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    for row in payload.rows:
        _require_open_period(
            company,
            row.occurred_at,
            operation="import a bank transaction",
        )
    begin_sqlite_immediate(db)
    try:
        return bank_import_svc.commit_import(
            db,
            bank_account_id=bank_account_id,
            rows=[r.model_dump() for r in payload.rows],
            gst_registered=company.gst_registered,
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
