"""Bank account services.

Two responsibilities:

1. Seeding. Every new company starts with one bank account so the reports
   work day-one.

2. Manual transaction entry on the bank account.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    AccountType,
    BankAccount,
    BankTransaction,
    BankTxnDirection,
    Contact,
    Invoice,
    InvoiceStatus,
)


class BankAccountMissing(Exception):
    """No active bank account configured. Router maps to HTTP 500 (config error)."""

    http_status: int = 500


class BankTxnError(Exception):
    """Manual-entry validation failure. Router maps to HTTP 400/404."""

    http_status: int = 400


class BankAccountNotFound(BankTxnError):
    http_status = 404


class CategoryAccountInvalid(BankTxnError):
    http_status = 400


class BankAccountDuplicate(BankTxnError):
    http_status = 409


class InvoicePaymentWouldDoubleCount(BankTxnError):
    http_status = 409


def get_bank_account(db: Session) -> BankAccount:
    """The company's operating bank account (single-account model)."""
    acc = (
        db.query(BankAccount)
        .filter(BankAccount.is_active.is_(True))
        .order_by(BankAccount.id.asc())
        .first()
    )
    if acc is None:
        raise BankAccountMissing("No active bank account configured for this company")
    return acc


def bank_account_balance(
    db: Session,
    bank_account: BankAccount,
    *,
    as_of: date | None = None,
) -> Decimal:
    """opening_balance + signed sum of bank_transactions on this account."""
    q = select(BankTransaction.direction, func.sum(BankTransaction.amount)).where(
        BankTransaction.bank_account_id == bank_account.id
    )
    if as_of is not None:
        q = q.where(BankTransaction.occurred_at <= as_of)
    rows = db.execute(q.group_by(BankTransaction.direction)).all()
    signed = Decimal("0")
    for direction, total in rows:
        if total is None:
            continue
        if direction == BankTxnDirection.IN or direction == "in":
            signed += Decimal(total)
        else:
            signed -= Decimal(total)
    return bank_account.opening_balance + signed


def seed_default_bank_accounts(session: Session) -> int:
    """Insert the default bank account if none exists. Returns rows inserted."""
    existing = session.query(BankAccount).count()
    if existing > 0:
        return 0
    session.add(BankAccount(name="Bank Account"))
    session.commit()
    return 1


def _normalise_name(name: str) -> str:
    return name.strip()


def _account_with_name(db: Session, name: str) -> BankAccount | None:
    return (
        db.query(BankAccount)
        .filter(func.lower(BankAccount.name) == _normalise_name(name).lower())
        .first()
    )


def _matching_open_invoice(
    db: Session,
    *,
    invoice_direction: str,
    amount: Decimal,
    memo: str | None,
    counter_party_name: str | None,
) -> tuple[Invoice, Contact] | None:
    """An open (posted, not draft/void) invoice whose outstanding balance equals
    the bank amount exactly, disambiguated by memo/counter-party text when any
    text exists. Shared by the AR and AP double-count guards."""
    candidates = (
        db.query(Invoice, Contact)
        .join(Contact, Invoice.contact_id == Contact.id)
        .filter(
            Invoice.direction == invoice_direction,
            Invoice.status.notin_([InvoiceStatus.DRAFT, InvoiceStatus.VOID]),
        )
        .all()
    )
    matches: list[tuple[Invoice, Contact]] = []
    for inv, contact in candidates:
        outstanding = Decimal(inv.total or 0) - Decimal(inv.paid_amount or 0)
        if outstanding == amount:
            matches.append((inv, contact))
    if not matches:
        return None

    haystack = f"{memo or ''} {counter_party_name or ''}".lower()
    if haystack:
        matched_by_text = [
            (inv, contact)
            for inv, contact in matches
            if (inv.invoice_number or "").lower() in haystack
            or (contact.name or "").lower() in haystack
        ]
        if not matched_by_text:
            return None
        matches = matched_by_text
    elif len(matches) != 1:
        return None

    return matches[0]


def reject_income_category_for_matching_ar_payment(
    db: Session,
    *,
    direction: BankTxnDirection | str,
    amount: Decimal,
    account: Account | None,
    memo: str | None = None,
    counter_party_name: str | None = None,
) -> None:
    if account is None or AccountType(account.type) != AccountType.INCOME:
        return
    direction_value = direction.value if hasattr(direction, "value") else str(direction)
    if direction_value != BankTxnDirection.IN.value:
        return
    match = _matching_open_invoice(
        db,
        invoice_direction="AR",
        amount=amount,
        memo=memo,
        counter_party_name=counter_party_name,
    )
    if match is None:
        return
    inv, contact = match
    raise InvoicePaymentWouldDoubleCount(
        "This money-in matches open AR invoice "
        f"{inv.invoice_number!r} for {contact.name!r}. Categorise the bank receipt "
        "to Accounts Receivable (1100) with the standard tax code, not an income "
        "account — the income and GST liability are already carried by the "
        "invoice journal; the transaction's GST keeps the sale in the cash-basis "
        "BAS."
    )


def reject_expense_category_for_matching_ap_payment(
    db: Session,
    *,
    direction: BankTxnDirection | str,
    amount: Decimal,
    account: Account | None,
    memo: str | None = None,
    counter_party_name: str | None = None,
) -> None:
    """Mirror of the AR guard: paying a posted supplier bill must not be
    categorised to an expense account (the expense is already in the invoice
    journal — it would double-count P&L and leave 2000 Accounts Payable
    uncleared forever)."""
    if account is None or AccountType(account.type) not in (
        AccountType.EXPENSE,
        AccountType.COST_OF_SALES,
    ):
        return
    direction_value = direction.value if hasattr(direction, "value") else str(direction)
    if direction_value != BankTxnDirection.OUT.value:
        return
    match = _matching_open_invoice(
        db,
        invoice_direction="AP",
        amount=amount,
        memo=memo,
        counter_party_name=counter_party_name,
    )
    if match is None:
        return
    inv, contact = match
    raise InvoicePaymentWouldDoubleCount(
        "This money-out matches open AP invoice "
        f"{inv.invoice_number!r} for {contact.name!r}. Categorise the bank payment "
        "to Accounts Payable (2000) with the standard tax code, not an expense "
        "account — the expense and GST credit are already carried by the invoice "
        "journal; the transaction's GST keeps the purchase in the cash-basis BAS."
    )


def create_account(
    db: Session,
    *,
    name: str,
    opening_balance: Decimal = Decimal("0"),
    bsb: str | None = None,
    account_number: str | None = None,
    is_active: bool = True,
) -> BankAccount:
    name = _normalise_name(name)
    if _account_with_name(db, name) is not None:
        raise BankAccountDuplicate(f"Bank account name already exists: {name}")
    account = BankAccount(
        name=name,
        opening_balance=opening_balance,
        bsb=bsb,
        account_number=account_number,
        is_active=is_active,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def update_account(
    db: Session,
    *,
    bank_account_id: int,
    name: str | None = None,
    bsb: str | None = None,
    account_number: str | None = None,
    is_active: bool | None = None,
) -> BankAccount:
    account = db.get(BankAccount, bank_account_id)
    if account is None:
        raise BankAccountNotFound(f"Bank account {bank_account_id} not found")
    if name is not None:
        new_name = _normalise_name(name)
        existing = _account_with_name(db, new_name)
        if existing is not None and existing.id != account.id:
            raise BankAccountDuplicate(f"Bank account name already exists: {new_name}")
        account.name = new_name
    if bsb is not None:
        account.bsb = bsb
    if account_number is not None:
        account.account_number = account_number
    if is_active is not None:
        account.is_active = is_active
    db.commit()
    db.refresh(account)
    return account


def record_manual_transaction(
    db: Session,
    *,
    bank_account_id: int,
    direction: BankTxnDirection,
    amount: Decimal,
    occurred_at: date,
    memo: str | None = None,
    counter_party_name: str | None = None,
    account_id: int | None = None,
    gst_amount: Decimal = Decimal("0"),
    tax_code: str = "standard",
) -> BankTransaction:
    """Record a manual movement on the bank account."""
    from ..models.company import TaxCode

    if amount <= 0:
        raise BankTxnError("Amount must be positive (direction carries the sign)")
    if gst_amount < 0 or gst_amount > amount:
        raise BankTxnError("GST amount must be between 0 and the gross amount")

    try:
        tc = TaxCode(tax_code)
    except ValueError:
        raise BankTxnError(f"Unknown tax_code: {tax_code!r}")
    # GST math sanity. STANDARD and CAPITAL can both carry positive GST (capital
    # purchases are still taxable supplies); GST_FREE / INPUT_TAXED / NONE
    # must have gst_amount = 0.
    _no_gst = {TaxCode.GST_FREE, TaxCode.INPUT_TAXED, TaxCode.NONE}
    if tc in _no_gst and gst_amount > 0:
        raise BankTxnError(
            f"tax_code={tc.value} forbids a positive gst_amount; set GST to 0 "
            f"or change tax_code to standard / capital."
        )

    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise BankAccountNotFound(f"Bank account {bank_account_id} not found")
    if not bank.is_active:
        raise BankTxnError(f"Bank account {bank.name} is inactive")

    if account_id is not None:
        acc = db.get(Account, account_id)
        if acc is None:
            raise CategoryAccountInvalid(f"Account {account_id} not found")
        if not acc.active:
            raise CategoryAccountInvalid(f"Account {acc.code} is inactive")
        reject_income_category_for_matching_ar_payment(
            db,
            direction=direction,
            amount=amount,
            account=acc,
            memo=memo,
            counter_party_name=counter_party_name,
        )
        reject_expense_category_for_matching_ap_payment(
            db,
            direction=direction,
            amount=amount,
            account=acc,
            memo=memo,
            counter_party_name=counter_party_name,
        )

    txn = BankTransaction(
        bank_account_id=bank.id,
        direction=direction,
        amount=amount,
        occurred_at=occurred_at,
        memo=memo,
        counter_party_name=counter_party_name,
        account_id=account_id,
        gst_amount=gst_amount,
        tax_code=tc,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


_UNSET = object()  # sentinel: distinguishes "field not provided" from explicit None


def recategorise_transaction(
    db: Session,
    *,
    txn_id: int,
    account_id: int | None | object = _UNSET,
    tax_code: str | None = None,
    gst_amount: Decimal | str | None = None,
) -> BankTransaction:
    """Reassign a bank transaction's category (account_id) and optionally its
    tax_code / gst_amount. Used by the reconciliation page to clean up
    uncategorised imports.

    `account_id` left at the _UNSET sentinel keeps the current category;
    an explicit None de-categorises the transaction.
    """
    from ..models.company import TaxCode

    txn = db.get(BankTransaction, txn_id)
    if txn is None:
        raise BankAccountNotFound(f"Transaction {txn_id} not found")

    if account_id is not _UNSET:
        acc = None
        if account_id is not None:
            acc = db.get(Account, account_id)
            if acc is None:
                raise CategoryAccountInvalid(f"Account {account_id} not found")
            if not acc.active:
                raise CategoryAccountInvalid(f"Account {acc.code} is inactive")
            reject_income_category_for_matching_ar_payment(
                db,
                direction=txn.direction,
                amount=Decimal(txn.amount),
                account=acc,
                memo=txn.memo,
                counter_party_name=txn.counter_party_name,
            )
            reject_expense_category_for_matching_ap_payment(
                db,
                direction=txn.direction,
                amount=Decimal(txn.amount),
                account=acc,
                memo=txn.memo,
                counter_party_name=txn.counter_party_name,
            )
        txn.account_id = account_id

    if tax_code is not None:
        try:
            tc = TaxCode(tax_code)
        except ValueError:
            raise BankTxnError(f"Unknown tax_code: {tax_code!r}")
        txn.tax_code = tc

    if gst_amount is not None:
        gst = Decimal(str(gst_amount))
        if gst < 0 or gst > txn.amount:
            raise BankTxnError(
                f"gst_amount {gst} must be between 0 and amount {txn.amount}"
            )
        txn.gst_amount = gst

    current_tax_code = txn.tax_code.value if hasattr(txn.tax_code, "value") else txn.tax_code
    try:
        tc = TaxCode(current_tax_code)
    except ValueError:
        raise BankTxnError(f"Unknown tax_code: {current_tax_code!r}")
    gst = Decimal(txn.gst_amount or 0)
    if gst < 0 or gst > txn.amount:
        raise BankTxnError(
            f"gst_amount {gst} must be between 0 and amount {txn.amount}"
        )
    _no_gst = {TaxCode.GST_FREE, TaxCode.INPUT_TAXED, TaxCode.NONE}
    if tc in _no_gst and gst > 0:
        raise BankTxnError(
            f"tax_code={tc.value} forbids a positive gst_amount."
        )

    db.commit()
    db.refresh(txn)
    return txn


def delete_manual_transaction(db: Session, *, txn_id: int) -> None:
    txn = db.get(BankTransaction, txn_id)
    if txn is None:
        raise BankAccountNotFound(f"Transaction {txn_id} not found")
    db.delete(txn)
    db.commit()
