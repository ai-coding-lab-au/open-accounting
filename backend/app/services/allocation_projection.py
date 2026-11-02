from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.company import begin_sqlite_immediate
from .allocation_batches import parse_exact_money


@dataclass(frozen=True, slots=True)
class AllocationProjectionLine:
    source_kind: str
    invoice_or_bill_id: int
    gross_cents: int
    gst_cents: int
    tax_components: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class AllocationProjection:
    transaction_id: int
    certainty: str = "uncertain"
    source: str = "uncertain"
    uncertainty_reason: str | None = None
    baseline_boundary: date | None = None
    selected_batch_id: int | None = None
    selected_batch_version: int | None = None
    effective_state_date: date | None = None
    ordered_allocation_lines: tuple[AllocationProjectionLine, ...] = field(default_factory=tuple)
    ordered_tax_components: tuple[tuple[str, int, int], ...] = field(default_factory=tuple)
    residual_cents: int = 0
    residual_account_id: int | None = None
    total_allocated_cents: int = 0
    total_gst_cents: int = 0
    bank_cash_cents: int = 0


class TransactionNotFoundError(KeyError):
    pass


class BaselineMissingError(ValueError):
    pass


class BaselineIncompleteError(ValueError):
    pass


class UncertainProjectionError(ValueError):
    pass


_CANONICAL_INTEGER_TEXT = re.compile(r"(?:0|[1-9][0-9]*|-[1-9][0-9]*)\Z")
_ISO_OFFSET_SUFFIX = re.compile(
    r"(?P<sign>[+-])(?P<hours>\d{2}):?(?P<minutes>\d{2})"
    r"(?::?(?P<seconds>\d{2}(?:\.\d+)?))?\Z"
)


def _validate_stored_cents(value: Any, *, label: str, allow_zero: bool = True, allow_negative: bool = False) -> int:
    if isinstance(value, bool):
        raise UncertainProjectionError(f"{label} must be an integer cent value, not a bool")
    if isinstance(value, float):
        raise UncertainProjectionError(f"{label} must be an integer cent value, not a float")
    if value is None:
        raise UncertainProjectionError(f"{label} is required")
    if isinstance(value, int):
        cents = value
    elif isinstance(value, Decimal):
        if "e" in str(value).lower() or not value.is_finite():
            raise UncertainProjectionError(f"{label} must be a finite canonical integer-cent value")
        if value != value.to_integral_value():
            raise UncertainProjectionError(f"{label} must be an exact integer-cent value")
        if value < -(2**63) or value > (2**63) - 1:
            raise UncertainProjectionError(f"{label} exceeds the integer-cent storage range")
        cents = int(value)
    elif isinstance(value, str):
        if _CANONICAL_INTEGER_TEXT.fullmatch(value) is None:
            raise UncertainProjectionError(f"{label} must be a canonical integer-cent string")
        digits = value[1:] if value.startswith("-") else value
        if len(digits) > 19:
            raise UncertainProjectionError(f"{label} exceeds the integer-cent storage range")
        try:
            cents = int(value)
        except ValueError as exc:
            raise UncertainProjectionError(f"{label} exceeds the integer-cent storage range") from exc
    else:
        raise UncertainProjectionError(f"{label} is not a supported integer-cent value")
    if not allow_negative and cents < 0:
        raise UncertainProjectionError(f"{label} cannot be negative")
    if not allow_zero and cents == 0:
        raise UncertainProjectionError(f"{label} cannot be zero")
    if cents < -(2**63) or cents > (2**63) - 1:
        raise UncertainProjectionError(f"{label} exceeds the integer-cent storage range")
    return cents


def _normalize_datetime(value: datetime, *, label: str) -> datetime:
    """Normalize aware instants to UTC and preserve naive UTC-scale values."""
    if value.tzinfo is None:
        return value
    try:
        if value.utcoffset() is None:
            return value.replace(tzinfo=None)
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    except (OverflowError, TypeError, ValueError) as exc:
        raise UncertainProjectionError(f"{label} has an invalid timezone offset") from exc


def _parse_datetime_string(value: str, *, label: str) -> datetime:
    if not value or value != value.strip():
        raise UncertainProjectionError(f"{label} must be a nonblank ISO date or datetime")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    offset_match = _ISO_OFFSET_SUFFIX.search(normalized[10:]) if len(normalized) > 10 else None
    if offset_match is not None:
        hours = int(offset_match.group("hours"))
        minutes = int(offset_match.group("minutes"))
        seconds = Decimal(offset_match.group("seconds") or "0")
        if hours > 23 or minutes > 59 or seconds >= 60:
            raise UncertainProjectionError(f"{label} has an invalid timezone offset")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise UncertainProjectionError(f"{label} is not a valid ISO datetime") from exc
    return _normalize_datetime(parsed, label=label)


def _coerce_date(
    value: date | datetime | str | None,
    *,
    label: str = "stored date",
    allow_missing: bool = False,
) -> date | None:
    if value is None:
        if allow_missing:
            return None
        raise UncertainProjectionError(f"{label} is required")
    if isinstance(value, datetime):
        return _normalize_datetime(value, label=label).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if not value or value != value.strip():
            raise UncertainProjectionError(f"{label} must be a nonblank ISO date or datetime")
        try:
            return date.fromisoformat(value)
        except ValueError:
            return _parse_datetime_string(value, label=label).date()
    raise UncertainProjectionError(f"{label} has an unsupported value type")


def _coerce_datetime(
    value: date | datetime | str | None,
    *,
    label: str = "stored timestamp",
    allow_missing: bool = False,
) -> datetime | None:
    if value is None:
        if allow_missing:
            return None
        raise UncertainProjectionError(f"{label} is required")
    if isinstance(value, datetime):
        return _normalize_datetime(value, label=label)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        return _parse_datetime_string(value, label=label)
    raise UncertainProjectionError(f"{label} has an unsupported value type")


def _coerce_knowledge_datetime(value: date | datetime | str | None) -> datetime:
    if value is None:
        raise UncertainProjectionError("known_as_of is required")
    if isinstance(value, datetime):
        return _normalize_datetime(value, label="known_as_of")
    elif isinstance(value, date):
        return datetime.combine(value, time.max)
    elif isinstance(value, str):
        if not value or value != value.strip():
            raise UncertainProjectionError("known_as_of must be a nonblank ISO date or datetime")
        if len(value) == 10:
            try:
                parsed_date = date.fromisoformat(value)
            except ValueError:
                pass
            else:
                return datetime.combine(parsed_date, time.max)
        return _parse_datetime_string(value, label="known_as_of")
    else:
        raise UncertainProjectionError("known_as_of has an unsupported value type")


def _selected_batch_values(db: Session, bank_transaction_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
                     SELECT b.id, b.transaction_version, b.operation, b.effective_date, b.recorded_at,
                     b.manual_review_status, f.finalized_at, f.allocation_line_count,
                     f.component_count, f.total_allocated_cents, f.total_gst_cents,
                     f.residual_cents AS finalized_residual_cents, f.bank_cash_cents,
                         f.batch_id AS finalization_batch_id,
                     b.residual_amount_cents, b.residual_account_id,
                     b.baseline_finalization_id
            FROM allocation_batches b
                 LEFT JOIN allocation_batch_finalizations f ON f.batch_id = b.id
            WHERE b.bank_transaction_id = :bank_transaction_id
            ORDER BY b.transaction_version ASC, b.id ASC
            """
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _batch_lines(db: Session, batch_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
                 SELECT l.id AS line_id, l.line_order, l.source_kind, l.invoice_or_bill_id,
                   l.gross_cents, l.gst_cents,
                     c.id AS component_id, c.tax_code, c.gross_cents AS component_gross_cents,
                   c.gst_cents AS component_gst_cents
            FROM allocation_batch_lines l
            LEFT JOIN allocation_batch_tax_components c
                ON c.allocation_batch_line_id = l.id
            WHERE l.batch_id = :batch_id
            ORDER BY l.line_order ASC, l.id ASC, c.id ASC
            """
        ),
        {"batch_id": batch_id},
    ).mappings().all()
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = grouped.setdefault(
            row["line_id"],
            {
                "source_kind": row["source_kind"],
                "invoice_or_bill_id": row["invoice_or_bill_id"],
                "gross_cents": row["gross_cents"],
                "gst_cents": row["gst_cents"],
                "components": [],
            },
        )
        if row["component_id"] is not None:
            if row["tax_code"] is None:
                raise UncertainProjectionError("Batch tax component has no tax code")
            item["components"].append(
                (row["tax_code"], row["component_gross_cents"], row["component_gst_cents"])
            )
    validated = [
        {
            "source_kind": item["source_kind"],
            "invoice_or_bill_id": item["invoice_or_bill_id"],
            "gross_cents": _validate_stored_cents(item["gross_cents"], label="batch line gross cents", allow_zero=False),
            "gst_cents": _validate_stored_cents(item["gst_cents"], label="batch line gst cents"),
            "components": tuple(
                (
                    tax_code,
                    _validate_stored_cents(gross_cents, label=f"batch component gross cents for {tax_code}", allow_zero=False),
                    _validate_stored_cents(gst_cents, label=f"batch component gst cents for {tax_code}"),
                )
                for tax_code, gross_cents, gst_cents in sorted(item["components"])
            ),
        }
        for item in grouped.values()
    ]
    for line in validated:
        if line["gst_cents"] > line["gross_cents"]:
            raise UncertainProjectionError("Batch line GST cents exceed gross cents")
        if any(component_gst > component_gross for _code, component_gross, component_gst in line["components"]):
            raise UncertainProjectionError("Batch component GST cents exceed gross cents")
    return validated


def _raw_current_state(db: Session, bank_transaction_id: int) -> AllocationProjection:
    rows = db.execute(
        text(
            """
                 SELECT a.id AS allocation_id, a.invoice_id, a.amount, a.gst_amount,
                     i.direction AS document_direction,
                   t.id AS bank_transaction_id, t.amount AS bank_cash_cents,
                   t.gst_amount AS tx_gst_amount
            FROM invoice_payment_allocations a
            JOIN bank_transactions t ON t.id = a.bank_transaction_id
                 JOIN invoices i ON i.id = a.invoice_id
            WHERE a.bank_transaction_id = :bank_transaction_id
            ORDER BY a.id ASC
            """
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).mappings().all()
    lines: list[AllocationProjectionLine] = []
    total_allocated = 0
    total_gst = 0
    for row in rows:
        line_gross = parse_exact_money(row["amount"])
        line_gst = parse_exact_money(row["gst_amount"] or 0)
        components = db.execute(
            text(
                "SELECT tax_code, gross_amount, gst_amount FROM invoice_payment_tax_components WHERE allocation_id = :allocation_id ORDER BY tax_code ASC, id ASC"
            ),
            {"allocation_id": int(row["allocation_id"])},
        ).all()
        tax_tuple = tuple(
            (tax_code, parse_exact_money(gross), parse_exact_money(gst))
            for tax_code, gross, gst in components
        )
        lines.append(
            AllocationProjectionLine(
                source_kind="bill" if row["document_direction"] == "AP" else "invoice",
                invoice_or_bill_id=int(row["invoice_id"]),
                gross_cents=line_gross,
                gst_cents=line_gst,
                tax_components=tax_tuple,
            )
        )
        total_allocated += line_gross
        total_gst += line_gst
    tx = db.execute(
        text(
            "SELECT amount FROM bank_transactions WHERE id = :bank_transaction_id"
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).scalar_one_or_none()
    bank_cash_cents = parse_exact_money(tx or 0)
    return AllocationProjection(
        transaction_id=bank_transaction_id,
        certainty="complete",
        baseline_boundary=None,
        selected_batch_id=None,
        selected_batch_version=None,
        effective_state_date=None,
        ordered_allocation_lines=tuple(lines),
        ordered_tax_components=tuple(
            sorted(
                {
                    (tax_code, gross, gst)
                    for line in lines for tax_code, gross, gst in line.tax_components
                }
            )
        ),
        residual_cents=max(bank_cash_cents - total_allocated, 0),
        residual_account_id=None,
        total_allocated_cents=total_allocated,
        total_gst_cents=total_gst,
        bank_cash_cents=bank_cash_cents,
    )


def _baseline_projection(db: Session, bank_transaction_id: int, effective_date: date | None, known_at: datetime | None) -> AllocationProjection:
    baseline = db.execute(
        text(
            """
            SELECT baseline_event_id, observed_payment_date, observed_at, status,
                   observed_allocation_count, observed_component_count,
                   gross_allocation_total_cents, gst_total_cents
            FROM allocation_history_baseline_finalizations
            WHERE bank_transaction_id = :bank_transaction_id
            ORDER BY baseline_event_id DESC
            LIMIT 1
            """
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).mappings().one_or_none()
    if baseline is None:
        return AllocationProjection(
            transaction_id=bank_transaction_id,
            certainty="uncertain",
            source="uncertain",
            uncertainty_reason="missing_baseline",
            baseline_boundary=None,
            effective_state_date=effective_date,
        )
    observed_payment_date = _coerce_date(
        baseline["observed_payment_date"],
        label="baseline observed_payment_date",
        allow_missing=True,
    )
    observed_at = _coerce_datetime(
        baseline["observed_at"],
        label="baseline observed_at",
        allow_missing=True,
    )
    if baseline["status"] != "complete":
        return AllocationProjection(
            transaction_id=bank_transaction_id,
            certainty="uncertain",
            source="uncertain",
            uncertainty_reason="baseline_incomplete",
            baseline_boundary=observed_payment_date,
            effective_state_date=effective_date,
        )

    if observed_payment_date is None or observed_at is None:
        return AllocationProjection(
            transaction_id=bank_transaction_id,
            certainty="uncertain",
            source="uncertain",
            uncertainty_reason="incomplete_baseline",
            baseline_boundary=observed_payment_date,
            effective_state_date=effective_date,
        )

    if effective_date is not None and effective_date < observed_payment_date:
        return AllocationProjection(
            transaction_id=bank_transaction_id,
            certainty="uncertain",
            source="uncertain",
            uncertainty_reason="pre_baseline_operation",
            baseline_boundary=observed_payment_date,
            effective_state_date=effective_date,
        )
    if known_at is not None and observed_at > known_at:
        return AllocationProjection(
            transaction_id=bank_transaction_id,
            certainty="uncertain",
            source="uncertain",
            uncertainty_reason="pre_observation_knowledge",
            baseline_boundary=observed_payment_date,
            effective_state_date=effective_date,
        )

    rows = db.execute(
        text(
            """
            SELECT b.id, b.source_kind, b.invoice_or_bill_id,
                   b.allocated_gross_cents, b.allocated_gst_cents
            FROM allocation_history_baseline_bindings b
            WHERE b.baseline_event_id = :baseline_event_id
            ORDER BY b.id ASC
            """
        ),
        {"baseline_event_id": int(baseline["baseline_event_id"])},
    ).mappings().all()
    lines: list[AllocationProjectionLine] = []
    total_allocated = 0
    total_gst = 0
    observed_allocation_count = _validate_stored_cents(
        baseline["observed_allocation_count"], label="baseline allocation count"
    )
    observed_component_count = _validate_stored_cents(
        baseline["observed_component_count"], label="baseline component count"
    )
    for row in rows:
        components = db.execute(
            text(
                """
                SELECT tax_code, component_gross_cents, component_gst_cents
                FROM allocation_history_baseline_tax_bindings
                WHERE allocation_binding_id = :allocation_binding_id
                ORDER BY tax_code ASC, id ASC
                """
            ),
            {"allocation_binding_id": int(row["id"])},
        ).all()
        if any(tax_code is None for tax_code, _gross, _gst in components):
            raise UncertainProjectionError("Baseline tax component has no tax code")
        tax_tuple = tuple(
            (
                str(tax_code),
                _validate_stored_cents(
                    gross,
                    label=f"baseline component gross cents for {tax_code}",
                    allow_zero=False,
                ),
                _validate_stored_cents(
                    gst,
                    label=f"baseline component gst cents for {tax_code}",
                ),
            )
            for tax_code, gross, gst in components
        )
        gross_cents = _validate_stored_cents(row["allocated_gross_cents"], label="baseline allocated gross cents", allow_zero=False)
        gst_cents = _validate_stored_cents(row["allocated_gst_cents"], label="baseline allocated gst cents")
        if gst_cents > gross_cents:
            raise UncertainProjectionError("Baseline line GST cents exceed gross cents")
        if any(component_gst > component_gross for _code, component_gross, component_gst in tax_tuple):
            raise UncertainProjectionError("Baseline component GST cents exceed gross cents")
        lines.append(
            AllocationProjectionLine(
                source_kind=str(row["source_kind"]),
                invoice_or_bill_id=int(row["invoice_or_bill_id"]),
                gross_cents=gross_cents,
                gst_cents=gst_cents,
                tax_components=tax_tuple,
            )
        )
        total_allocated += gross_cents
        total_gst += gst_cents

    total_allocated_cents = _validate_stored_cents(baseline["gross_allocation_total_cents"], label="baseline total allocated cents")
    total_gst_cents = _validate_stored_cents(baseline["gst_total_cents"], label="baseline total gst cents")
    if len(lines) != observed_allocation_count or sum(len(line.tax_components) for line in lines) != observed_component_count:
        raise UncertainProjectionError("Baseline allocation or component counts do not match the stored finalization")
    for line in lines:
        if sum(component_gross for _code, component_gross, _gst in line.tax_components) != line.gross_cents:
            raise UncertainProjectionError("Baseline line components do not reconcile to the stored line gross cents")
        if sum(component_gst for _code, _gross, component_gst in line.tax_components) != line.gst_cents:
            raise UncertainProjectionError("Baseline line components do not reconcile to the stored line GST cents")
    if total_allocated != total_allocated_cents or total_gst != total_gst_cents:
        raise UncertainProjectionError("Baseline totals do not reconcile to the stored baseline finalization")

    bank_cash = db.execute(
        text("SELECT CAST(amount AS TEXT) FROM bank_transactions WHERE id = :bank_transaction_id"),
        {"bank_transaction_id": bank_transaction_id},
    ).scalar_one()
    bank_cash_cents = parse_exact_money(bank_cash)
    residual_cents = bank_cash_cents - total_allocated
    if residual_cents < 0 or bank_cash_cents != total_allocated + residual_cents:
        raise UncertainProjectionError("Baseline allocation totals do not reconcile to bank cash")

    certainty = "complete" if (effective_date is None or effective_date >= observed_payment_date) and (known_at is None or known_at >= observed_at) else "uncertain"
    if certainty == "complete":
        return AllocationProjection(
            transaction_id=bank_transaction_id,
            certainty="complete",
            source="complete_baseline",
            uncertainty_reason=None,
            baseline_boundary=observed_payment_date,
            selected_batch_id=None,
            selected_batch_version=None,
            effective_state_date=effective_date,
            ordered_allocation_lines=tuple(lines),
            ordered_tax_components=tuple(sorted({(tax_code, gross, gst) for line in lines for tax_code, gross, gst in line.tax_components})),
            residual_cents=residual_cents,
            residual_account_id=None,
            total_allocated_cents=total_allocated,
            total_gst_cents=total_gst,
            bank_cash_cents=bank_cash_cents,
        )
    return AllocationProjection(
        transaction_id=bank_transaction_id,
        certainty="uncertain",
        source="uncertain",
        uncertainty_reason="pre_baseline_operation" if effective_date is not None and effective_date < observed_payment_date else "pre_observation_knowledge",
        baseline_boundary=observed_payment_date,
        effective_state_date=effective_date,
        ordered_allocation_lines=tuple(lines),
        ordered_tax_components=tuple(sorted({(tax_code, gross, gst) for line in lines for tax_code, gross, gst in line.tax_components})),
        residual_cents=residual_cents,
        total_allocated_cents=total_allocated,
        total_gst_cents=total_gst,
        bank_cash_cents=bank_cash_cents,
    )


def project_allocation_state(
    db: Session,
    bank_transaction_id: int,
    effective_as_of: date | datetime | str,
    known_as_of: date | datetime | str,
) -> AllocationProjection:
    """Project the transaction-wide allocation state for a given observation point.

    The implementation is intentionally conservative: it only returns a complete
    state when the baseline or selected batch is visible as of the supplied
    observation point, and it never mixes raw live rows with historical batch or
    baseline data for the same transaction.
    """
    effective_date = _coerce_date(effective_as_of, label="effective_as_of")
    known_at = _coerce_knowledge_datetime(known_as_of)
    begin_sqlite_immediate(db)
    transaction = db.execute(
        text(
            "SELECT id, occurred_at, CAST(amount AS TEXT) AS amount FROM bank_transactions WHERE id = :bank_transaction_id"
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).mappings().one_or_none()
    if transaction is None:
        raise TransactionNotFoundError(f"Bank transaction {bank_transaction_id} does not exist")

    transaction_date = _coerce_date(
        transaction["occurred_at"], label="bank transaction occurred_at"
    )
    if baseline_exists := db.execute(
        text(
            "SELECT 1 FROM allocation_history_baseline_finalizations WHERE bank_transaction_id = :bank_transaction_id LIMIT 1"
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).first():
        baseline_projection = _baseline_projection(db, bank_transaction_id, effective_date, known_at)

    candidate_batches = _selected_batch_values(db, bank_transaction_id)
    visible_batches = []
    for row in candidate_batches:
        batch_effective = _coerce_date(
            row["effective_date"], label="batch effective_date", allow_missing=True
        )
        recorded_at = _coerce_datetime(
            row["recorded_at"], label="batch recorded_at", allow_missing=True
        )
        if batch_effective is None or recorded_at is None:
            raise UncertainProjectionError("Batch effective_date and recorded_at are required")
        if row["finalization_batch_id"] is None:
            finalized_at = None
        else:
            finalized_at = _coerce_datetime(
                row["finalized_at"], label="batch finalization finalized_at"
            )
        if finalized_at is not None and finalized_at < recorded_at:
            raise UncertainProjectionError("Batch finalization predates its recorded_at timestamp")
        row["_parsed_effective_date"] = batch_effective
        row["_parsed_recorded_at"] = recorded_at
        row["_parsed_finalized_at"] = finalized_at
        row["_state_lines"] = _batch_lines(db, int(row["id"]))
        row["_residual_cents"] = _validate_stored_cents(
            row["residual_amount_cents"], label="batch residual_amount_cents"
        )
        if row["finalization_batch_id"] is not None:
            row["_bank_cash_cents"] = _validate_stored_cents(
                row["bank_cash_cents"], label="batch finalization bank cash cents"
            )
            row["_finalization_line_count"] = _validate_stored_cents(
                row["allocation_line_count"], label="batch finalization allocation line count"
            )
            row["_finalization_component_count"] = _validate_stored_cents(
                row["component_count"], label="batch finalization component count"
            )
            row["_finalization_residual_cents"] = _validate_stored_cents(
                row["finalized_residual_cents"], label="batch finalization residual cents"
            )
            row["_finalization_total_allocated_cents"] = _validate_stored_cents(
                row["total_allocated_cents"], label="batch finalization total allocated cents"
            )
            row["_finalization_total_gst_cents"] = _validate_stored_cents(
                row["total_gst_cents"], label="batch finalization total GST cents"
            )
            lines = row["_state_lines"]
            allocated = sum(line["gross_cents"] for line in lines)
            gst = sum(line["gst_cents"] for line in lines)
            component_count = sum(len(line["components"]) for line in lines)
            if row["_finalization_line_count"] != len(lines):
                raise UncertainProjectionError("Batch line count does not match the stored finalization")
            if row["_finalization_component_count"] != component_count:
                raise UncertainProjectionError("Batch component count does not match the stored finalization")
            if row["_finalization_residual_cents"] != row["_residual_cents"]:
                raise UncertainProjectionError("Batch residual does not match the stored finalization")
            if allocated != row["_finalization_total_allocated_cents"]:
                raise UncertainProjectionError("Batch line totals do not reconcile to the stored finalization")
            if gst != row["_finalization_total_gst_cents"]:
                raise UncertainProjectionError("Batch GST totals do not reconcile to the stored finalization")
            if any(
                sum(component_gross for _code, component_gross, _component_gst in line["components"]) != line["gross_cents"]
                or sum(component_gst for _code, _component_gross, component_gst in line["components"]) != line["gst_cents"]
                for line in lines
            ):
                raise UncertainProjectionError("Batch line components do not reconcile to the stored line totals")
            if row["_residual_cents"] + allocated != row["_bank_cash_cents"]:
                raise UncertainProjectionError("Batch residual does not reconcile to bank cash")
        if finalized_at is None:
            continue
        if row["manual_review_status"] != "not_required":
            continue
        if batch_effective > effective_date:
            continue
        if finalized_at > known_at:
            continue
        if recorded_at > known_at:
            continue
        visible_batches.append(row)

    if visible_batches:
        latest = visible_batches[-1]
        state_lines = latest["_state_lines"]
        bank_cash = latest["_bank_cash_cents"]
        residual = latest["_residual_cents"]
        total_allocated = sum(item["gross_cents"] for item in state_lines)
        total_gst = sum(item["gst_cents"] for item in state_lines)
        ordered_tax = tuple(
            sorted(
                {
                    (tax_code, gross, gst)
                    for line in state_lines for tax_code, gross, gst in line["components"]
                }
            )
        )
        effective_state_date = latest["_parsed_effective_date"]
        return AllocationProjection(
            transaction_id=bank_transaction_id,
            certainty="complete",
            source="finalized_batch",
            uncertainty_reason=None,
            baseline_boundary=None,
            selected_batch_id=int(latest["id"]),
            selected_batch_version=int(latest["transaction_version"]),
            effective_state_date=effective_state_date,
            ordered_allocation_lines=tuple(
                AllocationProjectionLine(
                    source_kind=item["source_kind"],
                    invoice_or_bill_id=int(item["invoice_or_bill_id"]),
                    gross_cents=item["gross_cents"],
                    gst_cents=item["gst_cents"],
                    tax_components=item["components"],
                )
                for item in state_lines
            ),
            ordered_tax_components=ordered_tax,
            residual_cents=residual,
            residual_account_id=latest["residual_account_id"],
            total_allocated_cents=total_allocated,
            total_gst_cents=total_gst,
            bank_cash_cents=bank_cash,
        )

    if baseline_exists and "baseline_projection" in locals():
        return baseline_projection

    has_batches = db.execute(
        text(
            "SELECT 1 FROM allocation_batches WHERE bank_transaction_id = :bank_transaction_id LIMIT 1"
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).first()
    has_history = db.execute(
        text(
            "SELECT 1 FROM allocation_history_events WHERE bank_transaction_id = :bank_transaction_id LIMIT 1"
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).first()
    if not baseline_exists and not has_batches and not has_history:
        if effective_date < transaction_date:
            return AllocationProjection(
                transaction_id=bank_transaction_id,
                certainty="uncertain",
                source="uncertain",
                uncertainty_reason="historical_evidence_unavailable",
                effective_state_date=effective_date,
            )
        transaction_datetime = _coerce_datetime(
            transaction["occurred_at"], label="bank transaction occurred_at"
        )
        if known_at < transaction_datetime:
            return AllocationProjection(
                transaction_id=bank_transaction_id,
                certainty="uncertain",
                source="uncertain",
                uncertainty_reason="historical_evidence_unavailable",
                effective_state_date=effective_date,
            )
        raw = _raw_current_state(db, bank_transaction_id)
        return replace(raw, source="legacy_current", uncertainty_reason="historical_evidence_unavailable", certainty="uncertain")

    return AllocationProjection(
        transaction_id=bank_transaction_id,
        certainty="uncertain",
        source="uncertain",
        uncertainty_reason="historical_evidence_unavailable" if has_history else "missing_baseline",
        baseline_boundary=None,
        effective_state_date=effective_date,
    )


project_transaction_allocation_state = project_allocation_state
project_bank_transaction_allocation = project_allocation_state


__all__ = [
    "AllocationProjection",
    "AllocationProjectionLine",
    "BaselineIncompleteError",
    "BaselineMissingError",
    "TransactionNotFoundError",
    "UncertainProjectionError",
    "project_allocation_state",
    "project_transaction_allocation_state",
    "project_bank_transaction_allocation",
]
