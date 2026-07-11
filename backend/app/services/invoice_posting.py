"""Invoice ↔ general-ledger posting service.

Bridges the Invoice document model and the JournalEntry/JournalLine
ledger. The only writer of journal entries with source_type != MANUAL.

Two operations:
  post_invoice(session, invoice_id)  — DRAFT → AUTHORISED, generates entry
  void_invoice(session, invoice_id)  — any posted state → VOID, generates reversal

Both are idempotent against the (source_type, source_id) pair: posting
an already-posted invoice raises AlreadyPosted; voiding an already-
voided invoice raises AlreadyVoided. Neither silently no-ops.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from ..models.company import (
    Account,
    AccountType,
    BankTransaction,
    BankTxnDirection,
    Invoice,
    InvoiceDirection,
    InvoicePaymentAllocation,
    InvoiceStatus,
    JournalEntry,
    JournalEntrySource,
    JournalLine,
)
from ..schemas.journal import JournalLineCreate
from ..schemas._dates import check_reportable_date
from .account_invariants import protected_control_type
from .bank_accounts import _named_direction_invoices
from .invoice_math import GstMathError, check_gst_math, check_invoice_lines
from .journal import _validate_lines


POSTED_STATUSES = {
    InvoiceStatus.AUTHORISED,
    InvoiceStatus.UNPAID,
    InvoiceStatus.PARTIAL,
    InvoiceStatus.PAID,
    InvoiceStatus.AUTHORISED.value,
    InvoiceStatus.UNPAID.value,
    InvoiceStatus.PARTIAL.value,
    InvoiceStatus.PAID.value,
}
ORIGINAL_SOURCE_TYPES = (JournalEntrySource.INVOICE_AR, JournalEntrySource.INVOICE_AP)


class InvoicePostingError(Exception):
    http_status: int = 400


class AlreadyPosted(InvoicePostingError):
    http_status = 409


class AlreadyVoided(InvoicePostingError):
    http_status = 409


class InvalidTransition(InvoicePostingError):
    http_status = 409


class MissingAccount(InvoicePostingError):
    http_status = 422


class InvalidLineAccount(InvoicePostingError):
    http_status = 422


class MissingControlAccount(InvoicePostingError):
    http_status = 422


class OriginalEntryNotFound(InvoicePostingError):
    http_status = 409


class InvoiceHasSettlement(InvoicePostingError):
    http_status = 409


class InvoiceCashAlreadyRecognised(InvoicePostingError):
    http_status = 409


class InvalidInvoiceMath(InvoicePostingError):
    http_status = 422


class InvalidInvoiceDate(InvoicePostingError):
    http_status = 422


def post_invoice(session: Session, invoice_id: int) -> JournalEntry:
    invoice = _get_invoice(session, invoice_id)
    existing = _original_entry(session, invoice_id)
    if existing is not None:
        raise AlreadyPosted(f"Invoice {invoice_id} is already posted to journal entry {existing.id}.")
    if invoice.status != InvoiceStatus.DRAFT and invoice.status != InvoiceStatus.DRAFT.value:
        raise InvalidTransition(f"Invoice {invoice_id} must be draft before posting (status={invoice.status}).")

    try:
        check_reportable_date(invoice.issue_date, field_name="issue_date")
    except ValueError as exc:
        raise InvalidInvoiceDate(str(exc)) from exc

    try:
        check_gst_math(invoice.subtotal, invoice.gst_amount, invoice.total)
        check_invoice_lines(
            invoice.subtotal, invoice.gst_amount, invoice.total, invoice.lines
        )
    except GstMathError as exc:
        raise InvalidInvoiceMath(str(exc)) from exc

    duplicate_cash = _matching_primary_cash_event(session, invoice)
    if duplicate_cash is not None:
        raise InvoiceCashAlreadyRecognised(
            f"Invoice {invoice.invoice_number} matches an existing bank transaction "
            f"already categorised directly to income/expense (id "
            f"{duplicate_cash.id}). Reclassify that cash row to the appropriate "
            "AR/AP control account before posting this accrual invoice."
        )

    lines = _posting_lines(session, invoice)
    _validate_lines(session, lines)

    source_type = (
        JournalEntrySource.INVOICE_AR
        if invoice.direction == InvoiceDirection.AR or invoice.direction == InvoiceDirection.AR.value
        else JournalEntrySource.INVOICE_AP
    )
    contact_name = invoice.contact.name if invoice.contact else "Unknown contact"
    entry = JournalEntry(
        entry_date=invoice.issue_date,
        memo=f"Invoice {invoice.invoice_number} — {contact_name}",
        reference=invoice.invoice_number,
        source_type=source_type,
        source_id=invoice.id,
    )
    for line in lines:
        entry.lines.append(
            JournalLine(
                account_id=line.account_id,
                debit_amount=line.debit_amount or Decimal("0"),
                credit_amount=line.credit_amount or Decimal("0"),
                description=line.description,
            )
        )
    invoice.status = InvoiceStatus.AUTHORISED
    invoice.authorised_at = datetime.now(timezone.utc)
    session.add(entry)
    session.flush()
    return entry


def void_invoice(session: Session, invoice_id: int) -> JournalEntry:
    invoice = _get_invoice(session, invoice_id)
    if invoice.status not in POSTED_STATUSES:
        if invoice.status == InvoiceStatus.VOID or invoice.status == InvoiceStatus.VOID.value:
            raise AlreadyVoided(f"Invoice {invoice_id} is already void.")
        raise InvalidTransition(f"Invoice {invoice_id} must be posted before voiding (status={invoice.status}).")

    if Decimal(invoice.paid_amount or 0) > 0:
        raise InvoiceHasSettlement(
            f"Invoice {invoice.invoice_number} records a payment and cannot be voided. "
            "Reverse or reallocate the payment first."
        )

    allocation = (
        session.query(InvoicePaymentAllocation)
        .filter(InvoicePaymentAllocation.invoice_id == invoice.id)
        .first()
    )
    if allocation is not None:
        raise InvoiceHasSettlement(
            f"Invoice {invoice.invoice_number} has an explicit bank payment "
            "allocation and cannot be voided. Unallocate or reverse the payment "
            "through the bank workflow first."
        )

    settlement = _matching_control_settlement(session, invoice)
    if settlement is not None:
        raise InvoiceHasSettlement(
            f"Invoice {invoice.invoice_number} has a bank settlement transaction "
            f"({settlement.occurred_at.isoformat()}, id {settlement.id}) and cannot "
            "be voided while that receipt/payment remains categorised to its "
            "AR/AP control account. Reallocate or decategorise the bank transaction "
            "first, then record any refund or credit explicitly."
        )

    original = _original_entry(session, invoice_id)
    if original is None:
        raise OriginalEntryNotFound(f"Original posting entry for invoice {invoice_id} was not found.")

    reversal = (
        session.query(JournalEntry)
        .filter(JournalEntry.reverses_entry_id == original.id)
        .first()
    )
    if reversal is not None:
        raise AlreadyVoided(
            f"Invoice {invoice_id} is already voided by journal entry {reversal.id}."
        )

    lines = [
        JournalLineCreate(
            account_id=line.account_id,
            debit_amount=line.credit_amount or Decimal("0"),
            credit_amount=line.debit_amount or Decimal("0"),
            description=line.description,
        )
        for line in original.lines
    ]
    _validate_lines(session, lines)

    entry = JournalEntry(
        # Date the reversal to the ORIGINAL posting's entry_date, not "now", so the
        # reversal nets the original to zero at every as_of on/after the issue date.
        # A "now"-dated reversal of a prior-period invoice would leave that period's
        # P&L/AR overstated for any as_of between issue and void (audit round-3 P1).
        entry_date=original.entry_date,
        memo=f"Void invoice {invoice.invoice_number}",
        reference=invoice.invoice_number,
        source_type=JournalEntrySource.INVOICE_REVERSAL,
        source_id=invoice.id,
        reverses_entry_id=original.id,
    )
    for line in lines:
        entry.lines.append(
            JournalLine(
                account_id=line.account_id,
                debit_amount=line.debit_amount or Decimal("0"),
                credit_amount=line.credit_amount or Decimal("0"),
                description=line.description,
            )
        )
    invoice.status = InvoiceStatus.VOID
    session.add(entry)
    session.flush()
    return entry


def _matching_control_settlement(
    session: Session,
    invoice: Invoice,
) -> BankTransaction | None:
    """Conservatively detect an unallocated legacy bank settlement.

    New transactions carry explicit allocation rows and are handled before
    this fallback. Memo/contact inference remains only for historical control
    rows created before the allocation ledger existed.
    """

    is_ar = invoice.direction in {InvoiceDirection.AR, InvoiceDirection.AR.value}
    control_code = "1100" if is_ar else "2000"
    bank_direction = BankTxnDirection.IN if is_ar else BankTxnDirection.OUT
    control = session.query(Account).filter(Account.code == control_code).first()
    if control is None:
        return None

    candidates = (
        session.query(BankTransaction)
        .filter(
            BankTransaction.account_id == control.id,
            BankTransaction.direction == bank_direction,
        )
        .order_by(BankTransaction.occurred_at, BankTransaction.id)
        .all()
    )
    direction_value = (
        invoice.direction.value
        if hasattr(invoice.direction, "value")
        else str(invoice.direction)
    )

    for txn in candidates:
        if txn.invoice_allocations:
            # Explicit allocations are accounting fact. A transaction allocated
            # to another invoice cannot conservatively "spill" onto this one.
            if any(row.invoice_id == invoice.id for row in txn.invoice_allocations):
                return txn
            continue
        named = _named_direction_invoices(
            session,
            invoice_direction=direction_value,
            memo=txn.memo,
            counter_party_name=txn.counter_party_name,
        )
        if named:
            if any(candidate.id == invoice.id for candidate, _contact in named):
                return txn
            named_capacity = sum(
                (
                    max(
                        Decimal(candidate.total or 0)
                        - Decimal(candidate.paid_amount or 0),
                        Decimal("0"),
                    )
                    for candidate, _contact in named
                    if candidate.status not in {
                        InvoiceStatus.DRAFT,
                        InvoiceStatus.VOID,
                        InvoiceStatus.DRAFT.value,
                        InvoiceStatus.VOID.value,
                    }
                ),
                Decimal("0"),
            )
            if Decimal(txn.amount) <= named_capacity:
                # Fully allocated by explicit references to other invoices.
                continue
            # The residual is unallocated and could settle this invoice.
            return txn

        # Without an allocation table, any unallocated row in the correct
        # AR/AP control account could be a full, partial, batch, overpayment, or
        # prepayment for this invoice. Fail closed; the operator can identify it
        # or decategorise it before reversing the invoice journal.
        if Decimal(txn.amount) > 0:
            return txn
    return None


def _matching_primary_cash_event(
    session: Session,
    invoice: Invoice,
) -> BankTransaction | None:
    """Detect cash-basis P&L already recorded for the same draft invoice."""
    is_ar = invoice.direction in {InvoiceDirection.AR, InvoiceDirection.AR.value}
    bank_direction = BankTxnDirection.IN if is_ar else BankTxnDirection.OUT
    account_types = (
        (AccountType.INCOME,)
        if is_ar
        else (AccountType.EXPENSE, AccountType.COST_OF_SALES)
    )
    candidates = (
        session.query(BankTransaction)
        .join(Account, Account.id == BankTransaction.account_id)
        .filter(
            BankTransaction.direction == bank_direction,
            Account.type.in_([value.value for value in account_types]),
        )
        .order_by(BankTransaction.occurred_at, BankTransaction.id)
        .all()
    )
    invoice_number = (invoice.invoice_number or "").lower()
    contact_name = (invoice.contact.name if invoice.contact else "").lower()
    for txn in candidates:
        haystack = f"{txn.memo or ''} {txn.counter_party_name or ''}".lower()
        explicitly_named = (
            bool(invoice_number) and invoice_number in haystack
        ) or (bool(contact_name) and contact_name in haystack)
        exact_unlabelled = not haystack.strip() and Decimal(txn.amount) == Decimal(
            invoice.total
        )
        if explicitly_named or exact_unlabelled:
            return txn
    return None


def _get_invoice(session: Session, invoice_id: int) -> Invoice:
    invoice = (
        session.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.contact))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if invoice is None:
        raise InvalidTransition(f"Invoice {invoice_id} not found.")
    return invoice


def _posting_lines(session: Session, invoice: Invoice) -> list[JournalLineCreate]:
    if not invoice.lines:
        raise MissingAccount("Invoice has no account-coded lines to post.")

    validate_invoice_line_accounts(session, invoice.direction, invoice.lines)

    if invoice.direction == InvoiceDirection.AR or invoice.direction == InvoiceDirection.AR.value:
        ar = _control_account(session, "1100", "Accounts Receivable")
        gst_collected = _control_account(session, "2100", "GST Collected")
        lines = [
            JournalLineCreate(
                account_id=ar.id,
                debit_amount=invoice.total,
                description=f"Invoice {invoice.invoice_number} receivable",
            )
        ]
        for inv_line in invoice.lines:
            lines.append(
                JournalLineCreate(
                    account_id=inv_line.account_id,
                    credit_amount=inv_line.line_subtotal,
                    description=inv_line.description,
                )
            )
        if Decimal(invoice.gst_amount or 0) != 0:
            lines.append(
                JournalLineCreate(
                    account_id=gst_collected.id,
                    credit_amount=invoice.gst_amount,
                    description="GST collected",
                )
            )
        return lines

    ap = _control_account(session, "2000", "Accounts Payable")
    gst_paid = _control_account(session, "1200", "GST Paid")
    lines = []
    for inv_line in invoice.lines:
        lines.append(
            JournalLineCreate(
                account_id=inv_line.account_id,
                debit_amount=inv_line.line_subtotal,
                description=inv_line.description,
            )
        )
    if Decimal(invoice.gst_amount or 0) != 0:
        lines.append(
            JournalLineCreate(
                account_id=gst_paid.id,
                debit_amount=invoice.gst_amount,
                description="GST paid",
            )
        )
    lines.append(
        JournalLineCreate(
            account_id=ap.id,
            credit_amount=invoice.total,
            description=f"Invoice {invoice.invoice_number} payable",
        )
    )
    return lines


def _control_account(session: Session, code: str, label: str) -> Account:
    account = session.query(Account).filter(Account.code == code).first()
    if account is None or not account.active:
        raise MissingControlAccount(f"Missing control account {code} ({label}).")
    expected_type = protected_control_type(code)
    if expected_type is not None:
        try:
            actual_type = AccountType(account.type)
        except ValueError as exc:
            raise MissingControlAccount(
                f"Control account {code} ({label}) has invalid type {account.type!r}."
            ) from exc
        if actual_type != expected_type:
            raise MissingControlAccount(
                f"Control account {code} ({label}) must be active and type "
                f"{expected_type.value}."
            )
    return account


def validate_invoice_line_accounts(
    session: Session,
    direction: InvoiceDirection | str,
    invoice_lines,
) -> None:
    """Enforce the economic account class for every supplied invoice line.

    This is intentionally shared by create, update and posting.  API validation
    prevents bad drafts from being persisted, while the posting call protects
    legacy or externally-modified databases from entering an invalid journal.
    A header-only draft remains valid; once a line is supplied it must be coded.
    """

    try:
        normalised_direction = InvoiceDirection(direction)
    except ValueError as exc:
        raise InvalidLineAccount(f"Unsupported invoice direction {direction!r}.") from exc

    allowed_types = (
        {AccountType.INCOME}
        if normalised_direction == InvoiceDirection.AR
        else {AccountType.ASSET, AccountType.EXPENSE, AccountType.COST_OF_SALES}
    )
    expected_label = (
        "an active INCOME account"
        if normalised_direction == InvoiceDirection.AR
        else "an active ASSET, EXPENSE, or COST_OF_SALES account"
    )

    lines = list(invoice_lines)
    account_ids: set[int] = set()
    for idx, line in enumerate(lines, start=1):
        account_id = line.get("account_id") if isinstance(line, dict) else line.account_id
        if account_id is None:
            raise MissingAccount(f"Invoice line {idx} is missing account_id.")
        account_ids.add(account_id)

    accounts = {
        account.id: account
        for account in session.query(Account).filter(Account.id.in_(account_ids)).all()
    } if account_ids else {}
    for idx, line in enumerate(lines, start=1):
        account_id = line.get("account_id") if isinstance(line, dict) else line.account_id
        account = accounts.get(account_id)
        if account is None:
            raise InvalidLineAccount(
                f"Invoice line {idx} references account {account_id}, which does not exist."
            )
        try:
            actual_type = AccountType(account.type)
        except ValueError as exc:
            raise InvalidLineAccount(
                f"Invoice line {idx} references account {account.code} with invalid "
                f"type {account.type!r}."
            ) from exc
        if not account.active or actual_type not in allowed_types:
            raise InvalidLineAccount(
                f"Invoice line {idx} for {normalised_direction.value} must use "
                f"{expected_label}; account {account.code} is "
                f"{'active' if account.active else 'inactive'} {actual_type.value}."
            )
        if (
            normalised_direction == InvoiceDirection.AP
            and actual_type == AccountType.ASSET
            and account.code in {"1000", "1100", "1200"}
        ):
            raise InvalidLineAccount(
                f"Invoice line {idx}: account {account.code} is a bank/receivable/GST "
                "control account and cannot be used as an AP purchase line. Use "
                "an inventory, prepayment, or fixed-asset account instead."
            )
        tax_code = line.get("tax_code") if isinstance(line, dict) else getattr(
            line, "tax_code", None
        )
        line_gst = line.get("line_gst", 0) if isinstance(line, dict) else getattr(
            line, "line_gst", 0
        )
        tax_code = tax_code or ("standard" if Decimal(line_gst or 0) > 0 else "gst_free")
        if normalised_direction == InvoiceDirection.AR and tax_code == "capital":
            raise InvalidLineAccount(
                f"Invoice line {idx}: capital is a purchase tax treatment and "
                "cannot be used on an AR sale."
            )
        if tax_code == "capital" and actual_type != AccountType.ASSET:
            raise InvalidLineAccount(
                f"Invoice line {idx}: capital purchases must be coded to an ASSET account."
            )


def _original_entry(session: Session, invoice_id: int) -> JournalEntry | None:
    return (
        session.query(JournalEntry)
        .options(joinedload(JournalEntry.lines))
        .filter(
            JournalEntry.source_type.in_(ORIGINAL_SOURCE_TYPES),
            JournalEntry.source_id == invoice_id,
        )
        .first()
    )
