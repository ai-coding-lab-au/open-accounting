"""Bank account services.

Two responsibilities:

1. Seeding. Every new company starts with one bank account so the reports
   work day-one.

2. Manual transaction entry on the bank account.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import gst_policy
from . import invoice_payments
from .account_invariants import (
    AccountInvariantError,
    require_opening_balance_equity_account,
)

from ..models.company import (
    Account,
    AccountType,
    BankAccount,
    BankTransaction,
    BankTransactionIdempotencyKey,
    BankTxnDirection,
    Contact,
    Invoice,
    InvoicePaymentAllocation,
    InvoiceStatus,
    TaxCode,
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


class BankAccountConfigurationInvalid(BankTxnError):
    http_status = 409


class BankTransactionIdempotencyConflict(BankTxnError):
    http_status = 409


class InvoicePaymentWouldDoubleCount(BankTxnError):
    http_status = 409


class InvoiceSettlementConflict(InvoicePaymentWouldDoubleCount):
    """A control-account payment targets an invoice that is already void."""


def _canonical_money(value: Decimal | str | int) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.01")), ".2f")


def manual_transaction_payload_hash(
    *,
    bank_account_id: int,
    direction: BankTxnDirection | str,
    amount: Decimal,
    occurred_at: date,
    memo: str | None,
    counter_party_name: str | None,
    account_id: int | None,
    gst_amount: Decimal,
    tax_code: TaxCode | str,
    invoice_allocations: list | None,
    unapplied_account_id: int | None = None,
) -> str:
    """Hash the parsed economic request, independent of JSON formatting/order."""

    canonical_allocations: list[dict[str, int | str]] = []
    for spec in invoice_allocations or []:
        invoice_id = (
            spec.get("invoice_id")
            if isinstance(spec, dict)
            else getattr(spec, "invoice_id", None)
        )
        allocation_amount = (
            spec.get("amount")
            if isinstance(spec, dict)
            else getattr(spec, "amount", None)
        )
        try:
            canonical_allocations.append(
                {
                    "invoice_id": int(invoice_id),
                    "amount": _canonical_money(allocation_amount),
                }
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise BankTxnError("Invoice allocation is malformed.") from exc
    canonical_allocations.sort(
        key=lambda row: (int(row["invoice_id"]), str(row["amount"]))
    )
    direction_value = (
        direction.value if hasattr(direction, "value") else str(direction)
    )
    tax_code_value = (
        tax_code.value if hasattr(tax_code, "value") else str(tax_code)
    )
    payload = {
        "bank_account_id": int(bank_account_id),
        "direction": direction_value,
        "amount": _canonical_money(amount),
        "occurred_at": occurred_at.isoformat(),
        "memo": memo,
        "counter_party_name": counter_party_name,
        "account_id": account_id,
        "gst_amount": _canonical_money(gst_amount),
        "tax_code": tax_code_value,
        "invoice_allocations": canonical_allocations,
        "unapplied_account_id": unapplied_account_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _transaction_with_allocations(
    db: Session, transaction_id: int
) -> BankTransaction | None:
    return (
        db.query(BankTransaction)
        .options(
            selectinload(BankTransaction.invoice_allocations).selectinload(
                InvoicePaymentAllocation.tax_components
            )
        )
        .filter(BankTransaction.id == transaction_id)
        .first()
    )


def replay_manual_transaction(
    db: Session,
    *,
    idempotency_key: str,
    payload_hash: str,
) -> BankTransaction | None:
    """Return the owned transaction, or raise on key reuse/tombstone."""

    owner = db.get(BankTransactionIdempotencyKey, idempotency_key)
    if owner is None:
        return None
    if owner.payload_hash != payload_hash:
        raise BankTransactionIdempotencyConflict(
            "Idempotency-Key was already used with a different manual bank "
            "transaction payload."
        )
    if owner.bank_transaction_id is None:
        raise BankTransactionIdempotencyConflict(
            "Idempotency-Key belongs to a manual bank transaction that was deleted; "
            "use a new key for a genuinely new transaction."
        )
    transaction = _transaction_with_allocations(db, owner.bank_transaction_id)
    if transaction is None:
        raise BankTransactionIdempotencyConflict(
            "Idempotency-Key ownership is inconsistent with the bank ledger."
        )
    return transaction


def reject_capital_tax_code_on_control_account(
    account: Account | None, tax_code: TaxCode | str
) -> None:
    value = tax_code.value if hasattr(tax_code, "value") else str(tax_code)
    if account is not None and account.code in {"1100", "2000"} and value == "capital":
        raise BankTxnError(
            f"tax_code=capital is incompatible with {account.code}. AR/AP control "
            "payments must use the invoice's sale/purchase GST treatment."
        )


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


def _invoice_named_in_text(invoice: Invoice, contact: Contact, text: str) -> bool:
    return (
        bool(invoice.invoice_number)
        and invoice.invoice_number.lower() in text
    ) or (bool(contact.name) and contact.name.lower() in text)


def _named_direction_invoices(
    db: Session,
    *,
    invoice_direction: str,
    memo: str | None,
    counter_party_name: str | None,
) -> list[tuple[Invoice, Contact]]:
    haystack = f"{memo or ''} {counter_party_name or ''}".strip().lower()
    if not haystack:
        return []
    return [
        (invoice, contact)
        for invoice, contact in (
            db.query(Invoice, Contact)
            .join(Contact, Invoice.contact_id == Contact.id)
            .filter(Invoice.direction == invoice_direction)
            .all()
        )
        if _invoice_named_in_text(invoice, contact, haystack)
    ]


def _plausible_open_invoices(
    db: Session,
    *,
    invoice_direction: str,
    amount: Decimal,
    occurred_at: date | None = None,
) -> list[tuple[Invoice, Contact]]:
    """Open invoices that could accept this full or partial payment."""
    if amount <= 0:
        return []
    query = (
        db.query(Invoice, Contact)
        .join(Contact, Invoice.contact_id == Contact.id)
        .filter(
            Invoice.direction == invoice_direction,
            Invoice.status.notin_([InvoiceStatus.DRAFT, InvoiceStatus.VOID]),
        )
    )
    if occurred_at is not None:
        query = query.filter(Invoice.issue_date <= occurred_at)
    matches: list[tuple[Invoice, Contact]] = []
    for invoice, contact in query.all():
        outstanding = Decimal(invoice.total or 0) - Decimal(
            invoice.paid_amount or 0
        )
        # Include overpayments as plausible settlements too.  A real receipt
        # can settle the outstanding balance and leave an unapplied deposit;
        # excluding amount > outstanding created a direct-income escape hatch
        # that double-counted the invoice revenue.
        if outstanding > 0:
            matches.append((invoice, contact))
    return matches


def _matching_open_invoice(
    db: Session,
    *,
    invoice_direction: str,
    amount: Decimal,
    memo: str | None,
    counter_party_name: str | None,
    occurred_at: date | None = None,
) -> tuple[Invoice, Contact] | None:
    """Select a plausible full or partial settlement of an open invoice."""
    named = [
        (invoice, contact)
        for invoice, contact in _named_direction_invoices(
            db,
            invoice_direction=invoice_direction,
            memo=memo,
            counter_party_name=counter_party_name,
        )
        if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.VOID)
        and Decimal(invoice.total or 0) - Decimal(invoice.paid_amount or 0) > 0
        and (occurred_at is None or invoice.issue_date <= occurred_at)
    ]
    if named:
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            return None

    matches = _plausible_open_invoices(
        db,
        invoice_direction=invoice_direction,
        amount=amount,
        occurred_at=occurred_at,
    )
    if not matches:
        return None

    exact = [
        (invoice, contact)
        for invoice, contact in matches
        if Decimal(invoice.total or 0) - Decimal(invoice.paid_amount or 0)
        == amount
    ]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    return matches[0] if len(matches) == 1 else None


def reject_income_category_for_matching_ar_payment(
    db: Session,
    *,
    direction: BankTxnDirection | str,
    amount: Decimal,
    account: Account | None,
    memo: str | None = None,
    counter_party_name: str | None = None,
    occurred_at: date | None = None,
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
        occurred_at=occurred_at,
    )
    if match is None:
        candidates = _plausible_open_invoices(
            db,
            invoice_direction="AR",
            amount=amount,
            occurred_at=occurred_at,
        )
        if not candidates:
            return
        raise InvoicePaymentWouldDoubleCount(
            "This money-in could settle one of multiple open AR invoices. "
            "Do not record it as new income: categorise it to Accounts "
            "Receivable (1100) and provide explicit invoice allocations."
        )
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
    occurred_at: date | None = None,
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
        occurred_at=occurred_at,
    )
    if match is None:
        candidates = _plausible_open_invoices(
            db,
            invoice_direction="AP",
            amount=amount,
            occurred_at=occurred_at,
        )
        if not candidates:
            return
        raise InvoicePaymentWouldDoubleCount(
            "This money-out could settle one of multiple open AP invoices. "
            "Do not record it as a new expense: categorise it to Accounts "
            "Payable (2000) and provide explicit invoice allocations."
        )
    inv, contact = match
    raise InvoicePaymentWouldDoubleCount(
        "This money-out matches open AP invoice "
        f"{inv.invoice_number!r} for {contact.name!r}. Categorise the bank payment "
        "to Accounts Payable (2000) with the standard tax code, not an expense "
        "account — the expense and GST credit are already carried by the invoice "
        "journal; the transaction's GST keeps the purchase in the cash-basis BAS."
    )


def reject_control_category_for_void_invoice(
    db: Session,
    *,
    direction: BankTxnDirection | str,
    amount: Decimal,
    account: Account | None,
    memo: str | None = None,
    counter_party_name: str | None = None,
    occurred_at: date | None = None,
) -> None:
    """Reject an AR/AP settlement that identifies an already-void invoice.

    This is the reverse half of the settled-invoice void guard. Both write
    paths take SQLite's immediate lock, so a simultaneous settle/void has one
    safe winner: either settlement commits and void returns 409, or void commits
    and the later settlement returns 409.
    """

    if account is None:
        return
    direction_value = direction.value if hasattr(direction, "value") else str(direction)
    if account.code == "1100" and direction_value == BankTxnDirection.IN.value:
        invoice_direction = "AR"
    elif account.code == "2000" and direction_value == BankTxnDirection.OUT.value:
        invoice_direction = "AP"
    else:
        return

    candidates = (
        db.query(Invoice, Contact)
        .join(Contact, Invoice.contact_id == Contact.id)
        .filter(
            Invoice.direction == invoice_direction,
            Invoice.status == InvoiceStatus.VOID,
        )
        .all()
    )
    if not candidates:
        return

    named = _named_direction_invoices(
        db,
        invoice_direction=invoice_direction,
        memo=memo,
        counter_party_name=counter_party_name,
    )
    if named:
        named_voids = [
            (invoice, contact)
            for invoice, contact in named
            if invoice.status == InvoiceStatus.VOID
        ]
        if named_voids:
            candidates = named_voids
        else:
            # An explicit invoice/contact reference allocates the payment to a
            # non-void document only up to that document's outstanding amount.
            # A batch/overpayment residual is still unallocated and must fail
            # closed while any same-direction void exists.
            named_capacity = sum(
                (
                    max(
                        Decimal(invoice.total or 0)
                        - Decimal(invoice.paid_amount or 0),
                        Decimal("0"),
                    )
                    for invoice, _contact in named
                    if invoice.status != InvoiceStatus.VOID
                ),
                Decimal("0"),
            )
            if amount <= named_capacity:
                return

    if not candidates:
        return
    invoice, _contact = candidates[0]
    raise InvoiceSettlementConflict(
        "This control-account payment matches void invoice "
        f"{invoice.invoice_number!r}. Restore/correct the invoice or record an "
        "explicit refund, credit, or new invoice instead of settling a void document."
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
    if opening_balance != 0:
        try:
            require_opening_balance_equity_account(db)
        except AccountInvariantError as exc:
            raise BankAccountConfigurationInvalid(str(exc)) from exc
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
    idempotency_key: str,
    payload_hash: str,
    bank_account_id: int,
    direction: BankTxnDirection,
    amount: Decimal,
    occurred_at: date,
    memo: str | None = None,
    counter_party_name: str | None = None,
    account_id: int | None = None,
    gst_amount: Decimal = Decimal("0"),
    tax_code: str = "standard",
    invoice_allocations: list | None = None,
    unapplied_account_id: int | None = None,
    gst_registered: bool,
) -> BankTransaction:
    """Record a manual movement on the bank account."""
    replay = replay_manual_transaction(
        db,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
    )
    if replay is not None:
        return replay
    if amount <= 0:
        raise BankTxnError("Amount must be positive (direction carries the sign)")
    if gst_amount < 0 or gst_amount > amount:
        raise BankTxnError("GST amount must be between 0 and the gross amount")
    try:
        gst_policy.require_gst_registered_for_amount(
            gst_registered=gst_registered,
            gst_amount=gst_amount,
            context="Bank transaction",
        )
    except gst_policy.GstRegistrationError as exc:
        raise BankTxnError(str(exc)) from exc

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
    if not gst_registered:
        # tax_code drives future BAS membership. Persist NONE, not merely a
        # zero GST amount, so later registration cannot pull old turnover into
        # G1/G10/G11 retroactively.
        tc = TaxCode.NONE

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
        reject_capital_tax_code_on_control_account(acc, tc)
        reject_income_category_for_matching_ar_payment(
            db,
            direction=direction,
            amount=amount,
            account=acc,
            memo=memo,
            counter_party_name=counter_party_name,
            occurred_at=occurred_at,
        )
        reject_expense_category_for_matching_ap_payment(
            db,
            direction=direction,
            amount=amount,
            account=acc,
            memo=memo,
            counter_party_name=counter_party_name,
            occurred_at=occurred_at,
        )
        if not invoice_allocations:
            reject_control_category_for_void_invoice(
                db,
                direction=direction,
                amount=amount,
                account=acc,
                memo=memo,
                counter_party_name=counter_party_name,
                occurred_at=occurred_at,
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
    db.flush()
    try:
        invoice_payments.replace_transaction_allocations(
            db,
            txn,
            invoice_allocations,
            unapplied_account_id=unapplied_account_id,
        )
    except invoice_payments.PaymentAllocationError as exc:
        raise InvoiceSettlementConflict(str(exc)) from exc
    if not gst_registered:
        txn.gst_amount = Decimal("0.00")
        txn.tax_code = TaxCode.NONE
    db.add(
        BankTransactionIdempotencyKey(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            bank_transaction_id=txn.id,
        )
    )
    db.flush()
    db.commit()
    persisted = _transaction_with_allocations(db, txn.id)
    if persisted is None:  # pragma: no cover - commit + PK lookup invariant
        raise BankTxnError("Manual bank transaction disappeared after commit.")
    return persisted


_UNSET = object()  # sentinel: distinguishes "field not provided" from explicit None


def recategorise_transaction(
    db: Session,
    *,
    txn_id: int,
    account_id: int | None | object = _UNSET,
    tax_code: str | None = None,
    gst_amount: Decimal | str | None = None,
    invoice_allocations: list | None = None,
    unapplied_account_id: int | None | object = _UNSET,
    gst_registered: bool,
) -> BankTransaction:
    """Reassign a bank transaction's category (account_id) and optionally its
    tax_code / gst_amount. Used by the reconciliation page to clean up
    uncategorised imports.

    `account_id` left at the _UNSET sentinel keeps the current category;
    an explicit None de-categorises the transaction.
    """
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
                occurred_at=txn.occurred_at,
            )
            reject_expense_category_for_matching_ap_payment(
                db,
                direction=txn.direction,
                amount=Decimal(txn.amount),
                account=acc,
                memo=txn.memo,
                counter_party_name=txn.counter_party_name,
                occurred_at=txn.occurred_at,
            )
            if not invoice_allocations:
                reject_control_category_for_void_invoice(
                    db,
                    direction=txn.direction,
                    amount=Decimal(txn.amount),
                    account=acc,
                    memo=txn.memo,
                    counter_party_name=txn.counter_party_name,
                    occurred_at=txn.occurred_at,
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
    effective_account = db.get(Account, txn.account_id) if txn.account_id is not None else None
    reject_capital_tax_code_on_control_account(effective_account, tc)
    if gst < 0 or gst > txn.amount:
        raise BankTxnError(
            f"gst_amount {gst} must be between 0 and amount {txn.amount}"
        )
    _no_gst = {TaxCode.GST_FREE, TaxCode.INPUT_TAXED, TaxCode.NONE}
    if tc in _no_gst and gst > 0:
        raise BankTxnError(
            f"tax_code={tc.value} forbids a positive gst_amount."
        )
    try:
        gst_policy.require_gst_registered_for_amount(
            gst_registered=gst_registered,
            gst_amount=gst,
            context="Reconciliation",
        )
    except gst_policy.GstRegistrationError as exc:
        raise BankTxnError(str(exc)) from exc
    if not gst_registered:
        txn.tax_code = TaxCode.NONE

    try:
        allocation_kwargs = {}
        if unapplied_account_id is not _UNSET:
            allocation_kwargs["unapplied_account_id"] = unapplied_account_id
        invoice_payments.replace_transaction_allocations(
            db,
            txn,
            invoice_allocations,
            **allocation_kwargs,
        )
    except invoice_payments.PaymentAllocationError as exc:
        raise InvoiceSettlementConflict(str(exc)) from exc
    if not gst_registered:
        txn.gst_amount = Decimal("0.00")
        txn.tax_code = TaxCode.NONE

    db.commit()
    db.refresh(txn)
    return txn


def delete_manual_transaction(db: Session, *, txn_id: int) -> None:
    txn = db.get(BankTransaction, txn_id)
    if txn is None:
        raise BankAccountNotFound(f"Transaction {txn_id} not found")
    touched = [row.invoice_id for row in txn.invoice_allocations]
    db.delete(txn)
    db.flush()
    invoice_payments.recompute_invoice_payment_state(db, touched)
    db.commit()
