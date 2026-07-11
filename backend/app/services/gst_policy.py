"""Company-level GST registration invariants.

The accounting models deliberately keep GST amounts on their source records,
but a company that is not registered for GST must never create a new GST split.
Callers reject such input instead of silently guessing how to redistribute it
across invoice lines or ledger accounts.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    BankTransaction,
    Invoice,
    JournalLine,
    TaxCode,
)
from ..models.outgoing import OutgoingDocument


class GstRegistrationError(ValueError):
    """A non-GST-registered company attempted to persist a GST amount."""


def require_gst_registered_for_amount(
    *,
    gst_registered: bool,
    gst_amount: Decimal | str | int | None,
    context: str,
) -> None:
    """Reject a non-zero GST split for a non-registered company."""

    amount = Decimal(str(gst_amount or 0))
    if not gst_registered and amount != 0:
        raise GstRegistrationError(
            f"{context} cannot include GST because this company is not "
            "GST-registered. Set GST to 0 and include the full gross amount "
            "in the income, expense, asset, or liability amount."
        )


def has_recorded_gst(db: Session) -> bool:
    """Whether a company database contains historical GST-bearing activity.

    Used when changing a company from registered to non-registered. Refusing
    that transition is safer than silently rewriting prior invoices, bank
    transactions, receipts, or journal control-account balances.
    """

    if db.query(Invoice.id).filter(Invoice.gst_amount != 0).first() is not None:
        return True
    if (
        db.query(BankTransaction.id)
        .filter(
            or_(
                BankTransaction.gst_amount != 0,
                BankTransaction.tax_code != TaxCode.NONE.value,
            )
        )
        .first()
        is not None
    ):
        return True
    if (
        db.query(OutgoingDocument.id)
        .filter(OutgoingDocument.gst_amount != 0)
        .first()
        is not None
    ):
        return True

    gst_account_ids = [
        row[0]
        for row in db.query(Account.id)
        .filter(Account.code.in_(["1200", "2100"]))
        .all()
    ]
    if not gst_account_ids:
        return False
    return (
        db.query(JournalLine.id)
        .filter(
            JournalLine.account_id.in_(gst_account_ids),
            or_(JournalLine.debit_amount != 0, JournalLine.credit_amount != 0),
        )
        .first()
        is not None
    )
