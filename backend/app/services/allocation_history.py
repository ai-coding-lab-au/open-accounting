"""Capture immutable observations of legacy payment allocations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.company import begin_sqlite_immediate
from ..db.migrations import (
    allocation_baseline_integrity_hash,
    allocation_baseline_money_to_cents,
)
from ..models.company import (
    AllocationHistoryBaselineBinding,
    AllocationHistoryBaselineFinalization,
    AllocationHistoryBaselineTaxBinding,
    AllocationHistoryEvent,
)


class AllocationHistoryCaptureError(ValueError):
    """A legacy allocation baseline cannot safely be captured."""


class AllocationHistoryConflict(Exception):
    """A bank transaction with allocation history cannot be mutated."""

    http_status = 409


_ALLOCATION_HISTORY_TRIGGER_MESSAGES = {
    "Bank transaction has allocation history",
    "Bank transaction identity is protected by allocation history",
}


def has_allocation_history(db: Session, bank_transaction_id: int) -> bool:
    return db.execute(
        text(
            "SELECT 1 FROM allocation_history_events "
            "WHERE bank_transaction_id=:id LIMIT 1"
        ),
        {"id": bank_transaction_id},
    ).first() is not None


def guard_allocation_history_mutation(
    db: Session, bank_transaction_id: int
) -> None:
    begin_sqlite_immediate(db)
    if has_allocation_history(db, bank_transaction_id):
        db.rollback()
        raise AllocationHistoryConflict(
            "This bank transaction has allocation history and cannot be changed."
        )


def translate_allocation_history_db_error(db: Session, exc: Exception) -> None:
    original = getattr(exc, "orig", exc)
    if str(original) not in _ALLOCATION_HISTORY_TRIGGER_MESSAGES:
        return
    db.rollback()
    raise AllocationHistoryConflict(
        "This bank transaction has allocation history and cannot be changed."
    ) from exc


@dataclass(frozen=True, slots=True)
class LegacyAllocationBaselineResult:
    baseline_event_id: int
    bank_transaction_id: int
    direction_evidence: tuple[tuple[str | None, str | None], ...]
    observation_timestamp: datetime
    history_boundary_date: date | None
    status: str
    incomplete_reasons: tuple[str, ...]
    allocation_count: int
    component_count: int
    gross_total_cents: int
    gst_total_cents: int
    integrity_hash: str
    pre_baseline_history_complete: bool = False


_REASON_ORDER = (
    "missing_tax_components",
    "invalid_amount",
    "unreconciled_totals",
    "unsupported_tax_evidence",
    "insufficient_source_data",
)
_TAX_CODES = {"standard", "gst_free", "input_taxed", "capital", "none"}


def _source_cents(raw: str | None) -> int | None:
    return allocation_baseline_money_to_cents(raw)


def _date_from_source(raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.isoformat() == raw else None


def capture_legacy_allocation_baseline(
    db: Session,
    bank_transaction_id: int,
    observed_at: datetime,
) -> LegacyAllocationBaselineResult:
    """Capture and finalize one ``legacy_history_unknown`` baseline.

    The function never commits. Its savepoint keeps partial capture artifacts
    out of the caller's transaction when a source or database guard rejects it.
    """
    if not isinstance(observed_at, datetime):
        raise TypeError("observed_at must be a datetime")

    begin_sqlite_immediate(db)
    with db.begin_nested():
        transaction = db.execute(
            text(
                "SELECT id, direction, CAST(occurred_at AS TEXT) AS occurred_at "
                "FROM bank_transactions WHERE id=:id"
            ),
            {"id": bank_transaction_id},
        ).one_or_none()
        if transaction is None:
            raise AllocationHistoryCaptureError("Bank transaction does not exist")

        begin_sqlite_immediate(db)
        if db.execute(
            text("SELECT 1 FROM allocation_history_events WHERE bank_transaction_id=:id LIMIT 1"),
            {"id": bank_transaction_id},
        ).first():
            raise AllocationHistoryCaptureError("Transaction already has allocation history")

        begin_sqlite_immediate(db)
        if db.execute(
            text(
                "SELECT 1 FROM allocation_history_baseline_finalizations "
                "WHERE bank_transaction_id=:id LIMIT 1"
            ),
            {"id": bank_transaction_id},
        ).first():
            raise AllocationHistoryCaptureError("Transaction already has a baseline finalization")

        begin_sqlite_immediate(db)
        observed_rows = db.execute(
            text(
                "SELECT t.id AS transaction_id, t.direction AS transaction_direction, "
                "CAST(t.occurred_at AS TEXT) AS payment_date, "
                "a.id AS allocation_id, a.invoice_id, i.direction AS invoice_direction, "
                "CAST(a.amount AS TEXT) AS allocation_gross_source, "
                "CAST(a.gst_amount AS TEXT) AS allocation_gst_source, "
                "CAST(a.created_at AS TEXT) AS allocation_created_at, "
                "c.id AS component_id, c.tax_code, "
                "CAST(c.gross_amount AS TEXT) AS component_gross_source, "
                "CAST(c.gst_amount AS TEXT) AS component_gst_source "
                "FROM bank_transactions t "
                "LEFT JOIN invoice_payment_allocations a "
                "ON a.bank_transaction_id=t.id "
                "LEFT JOIN invoices i ON i.id=a.invoice_id "
                "LEFT JOIN invoice_payment_tax_components c ON c.allocation_id=a.id "
                "WHERE t.id=:id ORDER BY a.id, c.id"
            ),
            {"id": bank_transaction_id},
        ).mappings().all()

        grouped: dict[int, dict] = {}
        for row in observed_rows:
            if row["allocation_id"] is None:
                continue
            allocation = grouped.setdefault(
                row["allocation_id"], {"row": row, "components": []}
            )
            if row["component_id"] is not None:
                allocation["components"].append(row)
        if not grouped:
            raise AllocationHistoryCaptureError(
                "A transaction without allocations cannot have a meaningful baseline"
            )

        payment_date = _date_from_source(transaction.occurred_at)
        reasons: set[str] = set()
        if payment_date is None:
            reasons.add("insufficient_source_data")

        source_allocations = list(grouped.values())
        direction_evidence = tuple(
            (
                item["row"]["transaction_direction"],
                item["row"]["invoice_direction"],
            )
            for item in source_allocations
        )
        for item in source_allocations:
            row = item["row"]
            gross_cents = _source_cents(row["allocation_gross_source"])
            gst_cents = _source_cents(row["allocation_gst_source"])
            if not item["components"]:
                reasons.add("missing_tax_components")

            if (
                gross_cents is None
                or gst_cents is None
                or gross_cents <= 0
                or gst_cents < 0
                or (gross_cents is not None and gst_cents is not None and gst_cents > gross_cents)
            ):
                reasons.add("invalid_amount")

            if row["invoice_direction"] == "AR":
                source_kind = "invoice"
                expected_transaction_direction = "in"
            elif row["invoice_direction"] == "AP":
                source_kind = "bill"
                expected_transaction_direction = "out"
            else:
                source_kind = None
                expected_transaction_direction = None
            if (
                row["invoice_id"] is None
                or source_kind is None
                or row["transaction_direction"] != expected_transaction_direction
            ):
                reasons.add("insufficient_source_data")

            component_gross_total = 0
            component_gst_total = 0
            for component in item["components"]:
                component_gross = _source_cents(component["component_gross_source"])
                component_gst = _source_cents(component["component_gst_source"])
                if (
                    component_gross is None
                    or component_gst is None
                    or component_gross <= 0
                    or component_gst < 0
                    or (
                        component_gross is not None
                        and component_gst is not None
                        and component_gst > component_gross
                    )
                    or component["component_gross_source"] is None
                    or component["component_gst_source"] is None
                ):
                    reasons.add("invalid_amount")
                if component["tax_code"] not in _TAX_CODES:
                    reasons.add("unsupported_tax_evidence")
                component_gross_total += component_gross or 0
                component_gst_total += component_gst or 0
            if item["components"] and (
                gross_cents != component_gross_total or gst_cents != component_gst_total
            ):
                reasons.add("unreconciled_totals")

        ordered_reasons = tuple(code for code in _REASON_ORDER if code in reasons)
        status = "incomplete" if ordered_reasons else "complete"
        event = AllocationHistoryEvent(
            operation="legacy_history_unknown",
            bank_transaction_id=bank_transaction_id,
        )
        db.add(event)
        db.flush()

        bindings: dict[int, AllocationHistoryBaselineBinding] = {}
        for allocation_id, item in grouped.items():
            row = item["row"]
            binding = AllocationHistoryBaselineBinding(
                baseline_event_id=event.id,
                bank_transaction_id=bank_transaction_id,
                allocation_row_id=allocation_id,
                source_kind=(
                    "invoice" if row["invoice_direction"] == "AR"
                    else "bill" if row["invoice_direction"] == "AP"
                    else None
                ),
                invoice_or_bill_id=row["invoice_id"],
                allocated_gross_cents=_source_cents(row["allocation_gross_source"]),
                allocated_gross_source=row["allocation_gross_source"],
                allocated_gst_cents=_source_cents(row["allocation_gst_source"]),
                allocated_gst_source=row["allocation_gst_source"],
                allocation_created_at=row["allocation_created_at"],
            )
            db.add(binding)
            bindings[allocation_id] = binding
        db.flush()

        component_bindings: list[AllocationHistoryBaselineTaxBinding] = []
        for allocation_id, item in grouped.items():
            for row in item["components"]:
                component_binding = AllocationHistoryBaselineTaxBinding(
                    allocation_binding_id=bindings[allocation_id].id,
                    source_component_row_id=row["component_id"],
                    tax_code=row["tax_code"],
                    component_gross_cents=_source_cents(row["component_gross_source"]),
                    component_gross_source=row["component_gross_source"],
                    component_gst_cents=_source_cents(row["component_gst_source"]),
                    component_gst_source=row["component_gst_source"],
                )
                db.add(component_binding)
                component_bindings.append(component_binding)
        db.flush()

        gross_total_cents = sum(
            binding.allocated_gross_cents or 0 for binding in bindings.values()
        )
        gst_total_cents = sum(
            binding.component_gst_cents or 0 for binding in component_bindings
        )
        observed_at_source = observed_at.replace(tzinfo=None).isoformat(
            sep=" ", timespec="microseconds"
        )
        reason_json = json.dumps(list(ordered_reasons), separators=(",", ":"))
        manifest_rows = [[
            "meta", event.id, bank_transaction_id,
            payment_date.isoformat() if payment_date else transaction.occurred_at,
            observed_at_source, len(bindings), len(component_bindings),
            gross_total_cents, gst_total_cents, status, reason_json,
        ]]
        for binding in bindings.values():
            manifest_rows.append([
                "allocation", binding.id, binding.baseline_event_id,
                binding.bank_transaction_id, binding.allocation_row_id,
                binding.source_kind, binding.invoice_or_bill_id,
                binding.allocated_gross_cents, binding.allocated_gross_source,
                binding.allocated_gst_cents, binding.allocated_gst_source,
                binding.allocation_created_at,
            ])
        for binding in component_bindings:
            manifest_rows.append([
                "component", binding.id, binding.allocation_binding_id,
                binding.source_component_row_id, binding.tax_code,
                binding.component_gross_cents, binding.component_gross_source,
                binding.component_gst_cents, binding.component_gst_source,
            ])
        integrity_hash = allocation_baseline_integrity_hash(manifest_rows)
        finalization = AllocationHistoryBaselineFinalization(
            baseline_event_id=event.id,
            bank_transaction_id=bank_transaction_id,
            observed_payment_date=(payment_date.isoformat() if payment_date else transaction.occurred_at),
            observed_at=observed_at,
            observed_allocation_count=len(bindings),
            observed_component_count=len(component_bindings),
            gross_allocation_total_cents=gross_total_cents,
            gst_total_cents=gst_total_cents,
            integrity_hash=integrity_hash,
            status=status,
            incomplete_reason_codes=reason_json,
        )
        db.add(finalization)
        db.flush()

        begin_sqlite_immediate(db)
        persisted = db.execute(
            text(
                "SELECT f.baseline_event_id, f.bank_transaction_id, f.status, "
                "f.integrity_hash, e.bank_transaction_id AS event_transaction_id "
                "FROM allocation_history_baseline_finalizations f "
                "JOIN allocation_history_events e ON e.id=f.baseline_event_id "
                "WHERE f.baseline_event_id=:event_id"
            ),
            {"event_id": event.id},
        ).one_or_none()
        if (
            persisted is None
            or persisted.bank_transaction_id != bank_transaction_id
            or persisted.event_transaction_id != bank_transaction_id
            or persisted.status != status
            or persisted.integrity_hash != integrity_hash
        ):
            raise AllocationHistoryCaptureError("Persisted baseline differs from its observation")

        return LegacyAllocationBaselineResult(
            baseline_event_id=event.id,
            bank_transaction_id=bank_transaction_id,
            direction_evidence=direction_evidence,
            observation_timestamp=observed_at,
            history_boundary_date=payment_date,
            status=status,
            incomplete_reasons=ordered_reasons,
            allocation_count=len(bindings),
            component_count=len(component_bindings),
            gross_total_cents=gross_total_cents,
            gst_total_cents=gst_total_cents,
            integrity_hash=integrity_hash,
        )