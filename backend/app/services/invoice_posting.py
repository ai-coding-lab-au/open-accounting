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
    Invoice,
    InvoiceDirection,
    InvoiceStatus,
    JournalEntry,
    JournalEntrySource,
    JournalLine,
)
from ..schemas.journal import JournalLineCreate
from .invoice_math import GstMathError, check_gst_math
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


class MissingControlAccount(InvoicePostingError):
    http_status = 422


class OriginalEntryNotFound(InvoicePostingError):
    http_status = 409


class InvalidInvoiceMath(InvoicePostingError):
    http_status = 422


def post_invoice(session: Session, invoice_id: int) -> JournalEntry:
    invoice = _get_invoice(session, invoice_id)
    existing = _original_entry(session, invoice_id)
    if existing is not None:
        raise AlreadyPosted(f"Invoice {invoice_id} is already posted to journal entry {existing.id}.")
    if invoice.status != InvoiceStatus.DRAFT and invoice.status != InvoiceStatus.DRAFT.value:
        raise InvalidTransition(f"Invoice {invoice_id} must be draft before posting (status={invoice.status}).")

    try:
        check_gst_math(invoice.subtotal, invoice.gst_amount, invoice.total)
    except GstMathError as exc:
        raise InvalidInvoiceMath(str(exc)) from exc

    # check_gst_math tolerates ±$0.02 of header drift, but the journal entry is
    # exact (AR/AP leg = total vs line subtotals + GST). Any drift would raise
    # UnbalancedEntry below — catch it here as an operator-fixable 422 instead.
    if invoice.lines:
        line_sum = sum((Decimal(line.line_subtotal) for line in invoice.lines), Decimal("0"))
        expected = line_sum + Decimal(invoice.gst_amount or 0)
        if Decimal(invoice.total) != expected:
            raise InvalidInvoiceMath(
                f"Invoice total {invoice.total} does not equal line subtotals "
                f"{line_sum} + GST {invoice.gst_amount or 0} = {expected}. "
                "Correct the invoice before posting."
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
    for idx, line in enumerate(invoice.lines, start=1):
        if line.account_id is None:
            raise MissingAccount(f"Invoice line {idx} is missing account_id.")

    if not invoice.lines:
        raise MissingAccount("Invoice has no account-coded lines to post.")

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
    account = session.query(Account).filter(Account.code == code, Account.active.is_(True)).first()
    if account is None:
        raise MissingControlAccount(f"Missing control account {code} ({label}).")
    return account


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
