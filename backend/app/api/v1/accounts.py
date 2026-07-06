from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...deps import PathId, get_company_db, get_current_company
from ...models.company import Account, AccountType, BankRule, BankTransaction, InvoiceLine, JournalLine
from ...models.master import Company
from ...schemas.company import AccountCreate, AccountOut, AccountUpdate


router = APIRouter(prefix="/accounts", tags=["accounts"])


def _get_account_or_404(db: Session, account_id: int) -> Account:
    acc = db.query(Account).filter(Account.id == account_id).one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return acc


def _validate_parent(db: Session, parent_id: int | None, self_id: int | None) -> None:
    if parent_id is None:
        return
    parent = db.query(Account).filter(Account.id == parent_id).one_or_none()
    if parent is None:
        raise HTTPException(status_code=400, detail="Parent account not found")
    if self_id is not None and parent_id == self_id:
        raise HTTPException(status_code=400, detail="Account cannot be its own parent")
    # Walk up to detect cycles
    seen: set[int] = set()
    cursor: Account | None = parent
    while cursor is not None:
        if cursor.id in seen:
            raise HTTPException(status_code=400, detail="Parent cycle detected")
        if self_id is not None and cursor.id == self_id:
            raise HTTPException(status_code=400, detail="Parent cycle detected")
        seen.add(cursor.id)
        cursor = cursor.parent


@router.get("", response_model=list[AccountOut])
def list_accounts(
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return db.query(Account).order_by(Account.code.asc()).all()


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: AccountCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    if db.query(Account).filter(Account.code == payload.code).first():
        raise HTTPException(status_code=409, detail=f"Account code '{payload.code}' already exists")
    _validate_parent(db, payload.parent_id, None)
    acc = Account(
        code=payload.code,
        name=payload.name,
        type=AccountType(payload.type),
        parent_id=payload.parent_id,
        is_gst=payload.is_gst,
        active=True,
        description=payload.description,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(
    account_id: PathId,
    payload: AccountUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    acc = _get_account_or_404(db, account_id)

    if payload.code is not None and payload.code != acc.code:
        if db.query(Account).filter(Account.code == payload.code, Account.id != acc.id).first():
            raise HTTPException(
                status_code=409, detail=f"Account code '{payload.code}' already exists"
            )
        acc.code = payload.code
    if payload.name is not None:
        acc.name = payload.name
    if payload.type is not None:
        acc.type = AccountType(payload.type)
    if payload.set_parent_null:
        acc.parent_id = None
    elif payload.parent_id is not None:
        _validate_parent(db, payload.parent_id, acc.id)
        acc.parent_id = payload.parent_id
    if payload.is_gst is not None:
        acc.is_gst = payload.is_gst
    if payload.active is not None:
        acc.active = payload.active
    if payload.description is not None:
        acc.description = payload.description

    db.commit()
    db.refresh(acc)
    return acc


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    acc = _get_account_or_404(db, account_id)

    # Refuse delete if referenced anywhere.
    refs: list[str] = []
    if db.query(BankTransaction).filter(BankTransaction.account_id == account_id).first():
        refs.append("bank transactions")
    if db.query(InvoiceLine).filter(InvoiceLine.account_id == account_id).first():
        refs.append("invoice lines")
    if db.query(JournalLine).filter(JournalLine.account_id == account_id).first():
        refs.append("journal entries")
    if db.query(BankRule).filter(BankRule.set_account_id == account_id).first():
        refs.append("bank rules")
    if db.query(Account).filter(Account.parent_id == account_id).first():
        refs.append("child accounts")
    if refs:
        raise HTTPException(
            status_code=409,
            detail=(
                "Account is in use ("
                + ", ".join(refs)
                + "). Deactivate it instead of deleting."
            ),
        )

    db.delete(acc)
    db.commit()
    return None
