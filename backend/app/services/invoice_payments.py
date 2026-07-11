"""Explicit bank-to-invoice payment allocation invariants.

Text in a bank memo may be useful as a suggestion, but never establishes an
accounting relationship.  Only rows in ``invoice_payment_allocations`` drive
invoice paid/outstanding status.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    AccountType,
    BankTransaction,
    BankTxnDirection,
    Invoice,
    InvoiceDirection,
    InvoicePaymentAllocation,
    InvoicePaymentTaxComponent,
    InvoiceStatus,
    JournalEntry,
    JournalEntrySource,
    TaxCode,
)
from ..schemas._dates import current_date


class PaymentAllocationError(ValueError):
    pass


_UNSET = object()


def _money(value: Decimal | str | int) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_TAX_CODES = ("standard", "gst_free", "input_taxed", "capital", "none")


def _line_tax_code(line) -> str:
    value = getattr(line, "tax_code", None)
    value = value.value if hasattr(value, "value") else value
    if value in _TAX_CODES:
        return str(value)
    if value in (None, ""):
        return "standard" if _money(line.line_gst or 0) > 0 else "gst_free"
    raise PaymentAllocationError(
        f"Invoice line {getattr(line, 'id', '?')} has invalid tax_code {value!r}."
    )


def _invoice_tax_totals(invoice: Invoice) -> dict[str, tuple[Decimal, Decimal]]:
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for line in invoice.lines:
        code = _line_tax_code(line)
        gross, gst = totals.get(code, (Decimal("0.00"), Decimal("0.00")))
        totals[code] = (
            _money(gross + _money(line.line_total)),
            _money(gst + _money(line.line_gst or 0)),
        )
    gross_total = _money(sum((gross for gross, _gst in totals.values()), Decimal("0")))
    gst_total = _money(sum((gst for _gross, gst in totals.values()), Decimal("0")))
    if gross_total != _money(invoice.total) or gst_total != _money(invoice.gst_amount or 0):
        raise PaymentAllocationError(
            f"Invoice {invoice.invoice_number} tax composition does not match its "
            "posted totals; correct the invoice before allocating cash."
        )
    return totals


def _other_tax_components(
    db: Session,
    *,
    invoice_id: int,
    excluding_transaction_id: int,
) -> dict[str, tuple[Decimal, Decimal]]:
    rows = (
        db.query(InvoicePaymentTaxComponent)
        .join(
            InvoicePaymentAllocation,
            InvoicePaymentAllocation.id
            == InvoicePaymentTaxComponent.allocation_id,
        )
        .filter(
            InvoicePaymentAllocation.invoice_id == invoice_id,
            InvoicePaymentAllocation.bank_transaction_id
            != excluding_transaction_id,
        )
        .all()
    )
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for row in rows:
        gross, gst = totals.get(row.tax_code, (Decimal("0.00"), Decimal("0.00")))
        totals[row.tax_code] = (
            _money(gross + row.gross_amount),
            _money(gst + row.gst_amount),
        )
    return totals


def _apportion_gross(
    remaining: dict[str, Decimal], amount: Decimal
) -> dict[str, Decimal]:
    total = _money(sum(remaining.values(), Decimal("0")))
    if total <= 0 or amount <= 0 or amount > total:
        raise PaymentAllocationError("Invoice tax allocation exceeds the remaining total.")
    if amount == total:
        return {code: value for code, value in remaining.items() if value > 0}

    raw = {code: value * amount / total for code, value in remaining.items() if value > 0}
    result = {
        code: value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        for code, value in raw.items()
    }
    cents = int(
        ((_money(amount) - sum(result.values(), Decimal("0"))) / Decimal("0.01"))
    )
    ranked = sorted(
        raw,
        key=lambda code: (raw[code] - result[code], -_TAX_CODES.index(code)),
        reverse=True,
    )
    for index in range(cents):
        code = ranked[index % len(ranked)]
        result[code] += Decimal("0.01")
    return {code: value for code, value in result.items() if value > 0}


def _allocation_tax_components(
    db: Session,
    *,
    invoice: Invoice,
    amount: Decimal,
    other_paid: Decimal,
    excluding_transaction_id: int,
) -> list[tuple[str, Decimal, Decimal]]:
    invoice_totals = _invoice_tax_totals(invoice)
    other = _other_tax_components(
        db,
        invoice_id=invoice.id,
        excluding_transaction_id=excluding_transaction_id,
    )
    other_component_gross = _money(
        sum((gross for gross, _gst in other.values()), Decimal("0"))
    )
    if other_paid > 0 and other_component_gross != other_paid:
        raise PaymentAllocationError(
            f"Invoice {invoice.invoice_number} has a legacy payment allocation "
            "without tax composition. Remove and re-allocate that payment first."
        )

    remaining_gross: dict[str, Decimal] = {}
    remaining_gst: dict[str, Decimal] = {}
    for code, (gross, gst) in invoice_totals.items():
        other_gross, other_gst = other.get(
            code, (Decimal("0.00"), Decimal("0.00"))
        )
        remaining_gross[code] = _money(gross - other_gross)
        remaining_gst[code] = _money(gst - other_gst)
        if remaining_gross[code] < 0 or remaining_gst[code] < 0:
            raise PaymentAllocationError(
                f"Invoice {invoice.invoice_number} payment tax composition is inconsistent."
            )

    gross_parts = _apportion_gross(remaining_gross, amount)
    result: list[tuple[str, Decimal, Decimal]] = []
    for code, gross_part in gross_parts.items():
        code_remaining_gross = remaining_gross[code]
        code_remaining_gst = remaining_gst[code]
        gst_part = (
            code_remaining_gst
            if gross_part == code_remaining_gross
            else _money(code_remaining_gst * gross_part / code_remaining_gross)
        )
        result.append((code, gross_part, min(gst_part, gross_part)))
    return result


def control_invoice_direction(
    account: Account | None,
    direction: BankTxnDirection | str,
) -> InvoiceDirection | None:
    if account is None:
        return None
    value = direction.value if hasattr(direction, "value") else str(direction)
    if account.code == "1100":
        if value != BankTxnDirection.IN.value:
            raise PaymentAllocationError(
                "Accounts Receivable (1100) accepts customer receipts only. "
                "Use an explicit credit/refund workflow for money out."
            )
        return InvoiceDirection.AR
    if account.code == "2000":
        if value != BankTxnDirection.OUT.value:
            raise PaymentAllocationError(
                "Accounts Payable (2000) accepts supplier payments only. "
                "Use an explicit refund/credit workflow for money in."
            )
        return InvoiceDirection.AP
    return None


def _spec_values(spec: Any) -> tuple[int, Decimal]:
    if isinstance(spec, dict):
        invoice_id = spec.get("invoice_id")
        amount = spec.get("amount")
    else:
        invoice_id = getattr(spec, "invoice_id", None)
        amount = getattr(spec, "amount", None)
    try:
        parsed_amount = _money(amount)
        if parsed_amount <= 0:
            raise PaymentAllocationError(
                "Invoice allocation amounts must be greater than zero."
            )
        return int(invoice_id), parsed_amount
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise PaymentAllocationError("Invoice allocation is malformed.") from exc


def _other_allocated_amount(
    db: Session, *, invoice_id: int, excluding_transaction_id: int
) -> Decimal:
    value = (
        db.query(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0))
        .filter(
            InvoicePaymentAllocation.invoice_id == invoice_id,
            InvoicePaymentAllocation.bank_transaction_id
            != excluding_transaction_id,
        )
        .scalar()
    )
    return _money(value or 0)


def _other_allocated_gst(
    db: Session, *, invoice_id: int, excluding_transaction_id: int
) -> Decimal:
    value = (
        db.query(func.coalesce(func.sum(InvoicePaymentAllocation.gst_amount), 0))
        .filter(
            InvoicePaymentAllocation.invoice_id == invoice_id,
            InvoicePaymentAllocation.bank_transaction_id
            != excluding_transaction_id,
        )
        .scalar()
    )
    return _money(value or 0)


def _allocation_gst(
    invoice: Invoice,
    *,
    amount: Decimal,
    other_paid: Decimal,
    other_gst: Decimal,
) -> Decimal:
    invoice_total = _money(invoice.total)
    invoice_gst = _money(invoice.gst_amount or 0)
    remaining_gst = max(invoice_gst - other_gst, Decimal("0"))
    if invoice_total <= 0 or invoice_gst <= 0:
        return Decimal("0.00")
    if other_paid + amount == invoice_total:
        return remaining_gst
    proportional = _money(invoice_gst * amount / invoice_total)
    return min(proportional, remaining_gst, amount)


def recompute_invoice_payment_state(db: Session, invoice_ids: Iterable[int]) -> None:
    for invoice_id in set(invoice_ids):
        invoice = db.get(Invoice, invoice_id)
        if invoice is None or invoice.status in (InvoiceStatus.DRAFT, InvoiceStatus.VOID):
            continue
        allocations = (
            db.query(InvoicePaymentAllocation, BankTransaction)
            .join(
                BankTransaction,
                BankTransaction.id
                == InvoicePaymentAllocation.bank_transaction_id,
            )
            .filter(InvoicePaymentAllocation.invoice_id == invoice_id)
            .filter(BankTransaction.occurred_at <= current_date())
            .all()
        )
        paid = _money(sum((row.amount for row, _txn in allocations), Decimal("0")))
        total = _money(invoice.total)
        if paid <= 0:
            invoice.paid_amount = Decimal("0.00")
            invoice.paid_date = None
            invoice.status = InvoiceStatus.AUTHORISED
        elif paid < total:
            invoice.paid_amount = paid
            invoice.paid_date = max(txn.occurred_at for _row, txn in allocations)
            invoice.status = InvoiceStatus.PARTIAL
        else:
            invoice.paid_amount = total
            invoice.paid_date = max(txn.occurred_at for _row, txn in allocations)
            invoice.status = InvoiceStatus.PAID


def _apply_unapplied_residual(
    db: Session,
    *,
    transaction: BankTransaction,
    invoice_direction: InvoiceDirection,
    allocated_total: Decimal,
    requested_account_id: int | None | object,
) -> None:
    residual = _money(transaction.amount) - _money(allocated_total)
    if residual < 0:
        raise PaymentAllocationError("Invoice allocations exceed the bank amount.")
    if residual == 0:
        if requested_account_id not in (_UNSET, None):
            raise PaymentAllocationError(
                "unapplied_account_id is only valid when part of the bank amount "
                "remains unallocated."
            )
        transaction.unapplied_account_id = None
        transaction.unapplied_amount = Decimal("0.00")
        return

    account_id = (
        transaction.unapplied_account_id
        if requested_account_id is _UNSET
        else requested_account_id
    )
    if account_id is None:
        destination = (
            "a customer-deposit LIABILITY account"
            if invoice_direction == InvoiceDirection.AR
            else "a supplier-prepayment ASSET account"
        )
        raise PaymentAllocationError(
            f"{residual:.2f} of this bank transaction is not allocated to an "
            f"invoice. Select {destination} for the unapplied remainder."
        )
    residual_account = db.get(Account, account_id)
    if residual_account is None or not residual_account.active:
        raise PaymentAllocationError(
            "The unapplied remainder account does not exist or is inactive."
        )
    required_type = (
        AccountType.LIABILITY
        if invoice_direction == InvoiceDirection.AR
        else AccountType.ASSET
    )
    actual_type = (
        residual_account.type
        if isinstance(residual_account.type, AccountType)
        else AccountType(residual_account.type)
    )
    if actual_type != required_type or residual_account.code in {
        "1100",
        "1200",
        "2000",
        "2100",
    }:
        label = "LIABILITY" if required_type == AccountType.LIABILITY else "ASSET"
        raise PaymentAllocationError(
            f"The unapplied remainder must use a non-control {label} account."
        )
    transaction.unapplied_account_id = residual_account.id
    transaction.unapplied_amount = residual


def replace_transaction_allocations(
    db: Session,
    transaction: BankTransaction,
    specs: Iterable[Any] | None,
    *,
    require_for_control: bool = True,
    unapplied_account_id: int | None | object = _UNSET,
) -> None:
    """Atomically replace a transaction's allocation set and derived GST.

    ``specs=None`` preserves an existing valid allocation set.  An explicit
    empty list clears it, which is allowed only after moving the transaction
    away from AR/AP control accounts.
    """

    account = db.get(Account, transaction.account_id) if transaction.account_id else None
    invoice_direction = control_invoice_direction(account, transaction.direction)
    existing = list(transaction.invoice_allocations)

    if transaction.occurred_at > current_date() and (
        invoice_direction is not None or specs
    ):
        raise PaymentAllocationError(
            "A future-dated bank transaction is scheduled, not settled. Record "
            "it uncategorised now, then allocate it to invoices on or after its "
            "effective date."
        )

    if specs is None:
        if invoice_direction is None:
            if existing:
                touched = [row.invoice_id for row in existing]
                transaction.invoice_allocations.clear()
                db.flush()
                recompute_invoice_payment_state(db, touched)
            transaction.unapplied_account_id = None
            transaction.unapplied_amount = Decimal("0.00")
            return
        if existing:
            allocated_total = _money(
                sum((row.amount for row in existing), Decimal("0"))
            )
            _apply_unapplied_residual(
                db,
                transaction=transaction,
                invoice_direction=invoice_direction,
                allocated_total=allocated_total,
                requested_account_id=unapplied_account_id,
            )
            # Tax/GST edits cannot override invoice-derived settlement GST.
            transaction.gst_amount = _money(sum((row.gst_amount for row in existing), Decimal("0")))
            transaction.tax_code = (
                TaxCode.STANDARD if transaction.gst_amount > 0 else TaxCode.GST_FREE
            )
            return
        if require_for_control:
            raise PaymentAllocationError(
                f"Categorising to {account.code} requires an explicit invoice "
                "allocation. Select one or more invoices and allocate the full "
                "bank amount."
            )
        return

    parsed = [_spec_values(spec) for spec in specs]
    if invoice_direction is None:
        if parsed:
            raise PaymentAllocationError(
                "Invoice allocations require Accounts Receivable (1100) for "
                "money in or Accounts Payable (2000) for money out."
            )
        touched = [row.invoice_id for row in existing]
        transaction.invoice_allocations.clear()
        transaction.unapplied_account_id = None
        transaction.unapplied_amount = Decimal("0.00")
        db.flush()
        recompute_invoice_payment_state(db, touched)
        return

    if not parsed:
        raise PaymentAllocationError(
            f"Categorising to {account.code} requires at least one invoice allocation."
        )
    invoice_ids = [invoice_id for invoice_id, _amount in parsed]
    if len(invoice_ids) != len(set(invoice_ids)):
        raise PaymentAllocationError(
            "A bank transaction may allocate to each invoice only once."
        )
    allocated_total = _money(sum((amount for _invoice_id, amount in parsed), Decimal("0")))
    if allocated_total > _money(transaction.amount):
        raise PaymentAllocationError(
            f"Invoice allocations exceed the bank amount "
            f"({_money(transaction.amount):.2f}); got {allocated_total:.2f}."
        )

    _apply_unapplied_residual(
        db,
        transaction=transaction,
        invoice_direction=invoice_direction,
        allocated_total=allocated_total,
        requested_account_id=unapplied_account_id,
    )

    prepared: list[
        tuple[Invoice, Decimal, Decimal, list[tuple[str, Decimal, Decimal]]]
    ] = []
    for invoice_id, amount in parsed:
        invoice = db.get(Invoice, invoice_id)
        if invoice is None:
            raise PaymentAllocationError(f"Invoice {invoice_id} does not exist.")
        actual_direction = (
            invoice.direction.value
            if hasattr(invoice.direction, "value")
            else str(invoice.direction)
        )
        if actual_direction != invoice_direction.value:
            raise PaymentAllocationError(
                f"Invoice {invoice.invoice_number} is {actual_direction}; it cannot "
                f"be allocated through {account.code}."
            )
        if invoice.status in (InvoiceStatus.DRAFT, InvoiceStatus.VOID):
            raise PaymentAllocationError(
                f"Invoice {invoice.invoice_number} must be posted and not void "
                "before payment allocation."
            )
        expected_source = (
            JournalEntrySource.INVOICE_AR
            if invoice_direction == InvoiceDirection.AR
            else JournalEntrySource.INVOICE_AP
        )
        original_entry = (
            db.query(JournalEntry)
            .filter(
                JournalEntry.source_type == expected_source,
                JournalEntry.source_id == invoice.id,
            )
            .first()
        )
        if original_entry is None:
            raise PaymentAllocationError(
                f"Invoice {invoice.invoice_number} has no verified accrual journal "
                "posting and cannot accept a bank allocation. Return it to draft "
                "and post it to the ledger first."
            )
        reversal = (
            db.query(JournalEntry.id)
            .filter(JournalEntry.reverses_entry_id == original_entry.id)
            .first()
        )
        if reversal is not None:
            raise PaymentAllocationError(
                f"Invoice {invoice.invoice_number} posting has been reversed and "
                "cannot accept a bank allocation."
            )
        if transaction.occurred_at < invoice.issue_date:
            raise PaymentAllocationError(
                f"Payment date {transaction.occurred_at.isoformat()} is before "
                f"invoice {invoice.invoice_number} issue date "
                f"{invoice.issue_date.isoformat()}. Record it as an unapplied "
                "customer deposit/supplier prepayment instead."
            )
        other_paid = _other_allocated_amount(
            db,
            invoice_id=invoice.id,
            excluding_transaction_id=transaction.id,
        )
        outstanding = _money(invoice.total) - other_paid
        if amount > outstanding:
            raise PaymentAllocationError(
                f"Allocation {amount:.2f} exceeds invoice "
                f"{invoice.invoice_number} outstanding amount {outstanding:.2f}."
            )
        components = _allocation_tax_components(
            db,
            invoice=invoice,
            amount=amount,
            other_paid=other_paid,
            excluding_transaction_id=transaction.id,
        )
        prepared.append(
            (
                invoice,
                amount,
                _money(sum((gst for _code, _gross, gst in components), Decimal("0"))),
                components,
            )
        )

    touched = {row.invoice_id for row in existing} | set(invoice_ids)
    transaction.invoice_allocations.clear()
    db.flush()
    for invoice, amount, gst_amount, components in prepared:
        allocation = InvoicePaymentAllocation(
            invoice_id=invoice.id,
            amount=amount,
            gst_amount=gst_amount,
        )
        for code, gross_amount, component_gst in components:
            allocation.tax_components.append(
                InvoicePaymentTaxComponent(
                    tax_code=code,
                    gross_amount=gross_amount,
                    gst_amount=component_gst,
                )
            )
        transaction.invoice_allocations.append(allocation)
    transaction.gst_amount = _money(
        sum(
            (gst_amount for _invoice, _amount, gst_amount, _components in prepared),
            Decimal("0"),
        )
    )
    transaction.tax_code = (
        TaxCode.STANDARD if transaction.gst_amount > 0 else TaxCode.GST_FREE
    )
    db.flush()
    recompute_invoice_payment_state(db, touched)


def allocations_for_transaction(db: Session, transaction_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(InvoicePaymentAllocation)
        .filter(InvoicePaymentAllocation.bank_transaction_id == transaction_id)
        .order_by(InvoicePaymentAllocation.id)
        .all()
    )
    return [
        {
            "id": row.id,
            "invoice_id": row.invoice_id,
            "amount": row.amount,
            "gst_amount": row.gst_amount,
        }
        for row in rows
    ]


def backfill_missing_tax_components(db: Session) -> int:
    """Upgrade allocation rows created before tax components were introduced."""

    allocations = (
        db.query(InvoicePaymentAllocation)
        .order_by(InvoicePaymentAllocation.invoice_id, InvoicePaymentAllocation.id)
        .all()
    )
    if not allocations:
        return 0
    invoice_ids = sorted({row.invoice_id for row in allocations})
    needs_rebuild = False
    for row in allocations:
        component_total = _money(
            sum((part.gross_amount for part in row.tax_components), Decimal("0"))
        )
        if component_total != _money(row.amount):
            needs_rebuild = True
            break
    if not needs_rebuild:
        recompute_invoice_payment_state(db, invoice_ids)
        db.commit()
        return 0

    for row in allocations:
        row.tax_components.clear()
    db.flush()

    rebuilt = 0
    for invoice_id in invoice_ids:
        invoice = db.get(Invoice, invoice_id)
        if invoice is None:
            raise PaymentAllocationError(
                f"Payment allocation references missing invoice {invoice_id}."
            )
        invoice_rows = [row for row in allocations if row.invoice_id == invoice_id]
        invoice_rows.sort(
            key=lambda row: (
                row.bank_transaction.occurred_at,
                row.bank_transaction_id,
                row.id,
            )
        )
        prior_paid = Decimal("0.00")
        for row in invoice_rows:
            components = _allocation_tax_components(
                db,
                invoice=invoice,
                amount=_money(row.amount),
                other_paid=prior_paid,
                excluding_transaction_id=row.bank_transaction_id,
            )
            row.gst_amount = _money(
                sum((gst for _code, _gross, gst in components), Decimal("0"))
            )
            for code, gross, gst in components:
                row.tax_components.append(
                    InvoicePaymentTaxComponent(
                        tax_code=code,
                        gross_amount=gross,
                        gst_amount=gst,
                    )
                )
            prior_paid = _money(prior_paid + row.amount)
            rebuilt += 1
    db.flush()

    transaction_ids = {row.bank_transaction_id for row in allocations}
    for transaction_id in transaction_ids:
        transaction = db.get(BankTransaction, transaction_id)
        if transaction is None:
            raise PaymentAllocationError(
                f"Payment allocation references missing bank transaction {transaction_id}."
            )
        allocated_total = _money(
            sum((row.amount for row in transaction.invoice_allocations), Decimal("0"))
        )
        if allocated_total + _money(transaction.unapplied_amount or 0) != _money(
            transaction.amount
        ):
            raise PaymentAllocationError(
                f"Bank transaction {transaction.id} has an incomplete allocation "
                "and no verified unapplied remainder destination."
            )
        transaction.gst_amount = _money(
            sum(
                (row.gst_amount for row in transaction.invoice_allocations),
                Decimal("0"),
            )
        )
        transaction.tax_code = (
            TaxCode.STANDARD
            if transaction.gst_amount > 0
            else TaxCode.GST_FREE
        )
    recompute_invoice_payment_state(db, invoice_ids)
    db.commit()
    return rebuilt
