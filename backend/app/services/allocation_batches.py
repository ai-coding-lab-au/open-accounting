from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from ..db.company import begin_sqlite_immediate, rollback_session_on_error
from ..db.migrations import allocation_baseline_integrity_hash
from .period_lock import AccountingPeriodLockedError, require_open_date
from .invoice_payments import refresh_projected_paid_state_cache
from ..schemas._dates import current_date


_MISSING_LOCK_CONTEXT = object()


@dataclass(frozen=True, slots=True)
class _PeriodLockSnapshot:
    books_locked_through: date | None


class TransactionNotFoundError(KeyError):
    pass


class BaselineMissingError(ValueError):
    pass


class BaselineIncompleteError(ValueError):
    pass


class PreBaselineOperationError(ValueError):
    pass


class StaleVersionError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class InvalidExactMoneyInputError(ValueError):
    pass


class InvalidAllocationStateError(ValueError):
    pass


class InvalidResidualAccountError(ValueError):
    pass


PeriodLockedError = AccountingPeriodLockedError


class InternalLodgedPeriodApprovalMissingError(ValueError):
    pass


class ReversalInsideMarkedPeriodError(ValueError):
    pass


class ManualReviewRequiredError(ValueError):
    pass


class UncertainProjectionError(ValueError):
    pass


class ConcurrentWriteConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AllocationLineSpec:
    source_kind: str
    invoice_or_bill_id: int
    gross_cents: int
    gst_cents: int
    tax_components: tuple[tuple[str, int, int], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BatchWriteResult:
    batch_id: int
    transaction_version: int
    expected_prior_version: int
    operation: str
    manual_review_status: str
    bank_cash_cents: int
    bank_transaction_id: int
    baseline_finalization_id: int
    finalization_integrity_hash: str


def parse_exact_money(value: Decimal | str | int, *, allow_negative: bool = False) -> int:
    """Parse a money value to exact integer cents.

    Accepts Decimal, canonical decimal strings, and integer whole-dollar values;
    rejects booleans, floats, scientific notation, NaN/infinity, negatives,
    sub-cent values, and values beyond the signed integer-cent bound.
    """
    if isinstance(value, bool):
        raise InvalidExactMoneyInputError("bool is not valid money input")
    if isinstance(value, float):
        raise InvalidExactMoneyInputError("float is not valid money input")

    if isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, Decimal):
        dec = value
    elif isinstance(value, str):
        if value is None or value == "":
            raise InvalidExactMoneyInputError("empty money input")
        stripped = value.strip()
        if stripped != value or stripped.startswith("+") or stripped.startswith("-") and not allow_negative:
            raise InvalidExactMoneyInputError("money input cannot contain leading or trailing whitespace or a leading plus sign")
        if not stripped or any(ch in stripped.lower() for ch in ("e", "n", "i")):
            raise InvalidExactMoneyInputError("scientific notation and non-finite values are rejected")
        try:
            dec = Decimal(stripped)
        except InvalidOperation as exc:  # pragma: no cover - defensive guard
            raise InvalidExactMoneyInputError(f"invalid money input: {value!r}") from exc
    else:
        raise InvalidExactMoneyInputError(f"unsupported money type: {type(value)!r}")

    if not dec.is_finite():
        raise InvalidExactMoneyInputError("money input must be finite")
    if dec.is_nan() or dec in (Decimal("Infinity"), Decimal("-Infinity")):
        raise InvalidExactMoneyInputError("money input must be finite")
    if str(dec).upper().endswith("E") or "E" in str(dec).upper() or "e" in str(dec):
        raise InvalidExactMoneyInputError("exponent-form money input is rejected")
    if dec < 0 and not allow_negative:
        raise InvalidExactMoneyInputError("negative money is not permitted")
    cents_value = dec * Decimal("100")
    if cents_value != cents_value.to_integral_value():
        raise InvalidExactMoneyInputError("money input must be exact to the cent")
    cents = int(cents_value)
    if cents < -(2**63) or cents > (2**63) - 1:
        raise InvalidExactMoneyInputError("money input exceeds the signed integer-cent bound")
    return cents


def _require_cents(value: Any, label: str, *, allow_zero: bool = True) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < (0 if allow_zero else 1)
        or value > (2**63) - 1
    ):
        raise InvalidAllocationStateError(f"{label} must be a nonnegative integer-cent value")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_line(line: AllocationLineSpec | dict[str, Any]) -> AllocationLineSpec:
    if isinstance(line, AllocationLineSpec):
        source_kind = str(line.source_kind).strip()
        invoice_or_bill_id = line.invoice_or_bill_id
        raw_gross_cents = line.gross_cents
        raw_gst_cents = line.gst_cents
        raw_components = line.tax_components
    else:
        data = dict(line)
        source_kind = str(data["source_kind"]).strip()
        invoice_or_bill_id = int(data["invoice_or_bill_id"])
        raw_gross_cents = data["gross_cents"]
        raw_gst_cents = data["gst_cents"]
        raw_components = data.get("tax_components") or ()

    gross_cents = _require_cents(raw_gross_cents, "gross_cents", allow_zero=False)
    gst_cents = _require_cents(raw_gst_cents, "gst_cents")
    if gst_cents > gross_cents:
        raise InvalidAllocationStateError("gst_cents cannot exceed gross_cents")
    components = tuple(sorted(
        (
            str(code).strip(),
            _require_cents(gross, "component gross_cents", allow_zero=False),
            _require_cents(gst, "component gst_cents"),
        )
        for code, gross, gst in raw_components
    ))
    if any(gst > gross for _code, gross, gst in components):
        raise InvalidAllocationStateError("component gst_cents cannot exceed gross_cents")
    if not components:
        raise InvalidAllocationStateError("each allocation line requires tax components")
    if sum(gross for _code, gross, _gst in components) != gross_cents:
        raise InvalidAllocationStateError("component gross cents must reconcile to allocation gross cents")
    if sum(gst for _code, _gross, gst in components) != gst_cents:
        raise InvalidAllocationStateError("component GST cents must reconcile to allocation GST cents")
    return AllocationLineSpec(source_kind, invoice_or_bill_id, gross_cents, gst_cents, components)


def _vector_for_lines(lines: Sequence[AllocationLineSpec | dict[str, Any]]) -> tuple[tuple[str, str, int, int], ...]:
    items: list[tuple[str, str, int, int]] = []
    for item in lines:
        line = item if isinstance(item, AllocationLineSpec) else _normalize_line(item)
        for code, gross, gst in line.tax_components:
            items.append((line.source_kind, code, gross, gst))
    return _normalize_vector(items)


def _effective_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return value


def _recorded_at(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise InvalidAllocationStateError("recorded_at must be a datetime or ISO string")


def _require_open_effective_date(
    effective_date: date | datetime | str,
    books_locked_through: date | None | object,
    *,
    operation: str,
) -> None:
    if books_locked_through is _MISSING_LOCK_CONTEXT:
        raise InvalidAllocationStateError(
            "books_locked_through is required as detached accounting lock context"
        )
    if isinstance(books_locked_through, datetime):
        books_locked_through = books_locked_through.date()
    if books_locked_through is not None and not isinstance(books_locked_through, date):
        raise InvalidAllocationStateError("books_locked_through must be a date or None")
    require_open_date(
        _PeriodLockSnapshot(books_locked_through),
        _effective_date(effective_date),
        operation=operation,
    )


def _timestamp_text(value: date | datetime | str) -> str:
    return _recorded_at(value).isoformat(sep=" ", timespec="microseconds")


def _require_reason(value: str | None, label: str = "reason") -> str:
    if value is None or not str(value).strip():
        raise InvalidAllocationStateError(f"{label} is required")
    return str(value).strip()


def _canonical_idempotency_key(value: str | None) -> str:
    if value is None or not str(value).strip():
        raise InvalidAllocationStateError("idempotency_key is required")
    return str(value).strip()


def _batch_payload(
    *,
    operation: str,
    bank_transaction_id: int,
    expected_prior_version: int,
    effective_date: date | datetime | str,
    recorded_at: date | datetime | str,
    idempotency_key: str,
    reason: str,
    evidence_reference: str,
    evidence_sha256: str | None,
    actor_placeholder: str,
    linked_prior_batch_id: int | None,
    internal_correction_approval_id: int | None,
    residual_cents: int,
    residual_account_id: int | None,
    baseline_finalization_id: int,
) -> dict[str, Any]:
    return {
        "bank_transaction_id": bank_transaction_id,
        "operation": operation,
        "transaction_version": expected_prior_version + 1,
        "expected_prior_version": expected_prior_version,
        "effective_date": _effective_date(effective_date).isoformat(),
        "recorded_at": _timestamp_text(recorded_at),
        "idempotency_key": idempotency_key,
        "reason": reason,
        "evidence_reference": evidence_reference,
        "evidence_sha256": evidence_sha256,
        "actor_placeholder": actor_placeholder,
        "linked_prior_batch_id": linked_prior_batch_id,
        "internal_correction_approval_id": internal_correction_approval_id,
        "manual_review_status": "not_required",
        "residual_amount_cents": residual_cents,
        "residual_account_id": residual_account_id,
        "baseline_finalization_id": baseline_finalization_id,
    }


def _payload_rows(
    batch_payload: dict[str, Any], lines: Sequence[AllocationLineSpec]
) -> list[dict[str, Any]]:
    rows = [{"kind": "batch", **batch_payload}]
    for index, line in enumerate(lines):
        rows.append({
            "kind": "line",
            "line_order": index,
            "source_kind": line.source_kind,
            "invoice_or_bill_id": line.invoice_or_bill_id,
            "gross_cents": line.gross_cents,
            "gst_cents": line.gst_cents,
        })
        rows.extend(
            {
                "kind": "component",
                "line_order": index,
                "tax_code": tax_code,
                "gross_cents": gross_cents,
                "gst_cents": gst_cents,
            }
            for tax_code, gross_cents, gst_cents in line.tax_components
        )
    return rows


def _stored_request_payload_rows(db: Session, batch_id: int) -> list[dict[str, Any]]:
    batch = dict(db.execute(
        text(
            "SELECT bank_transaction_id, operation, transaction_version, "
            "expected_prior_version, effective_date, recorded_at, idempotency_key, "
            "reason, evidence_reference, evidence_sha256, actor_placeholder, "
            "linked_prior_batch_id, internal_correction_approval_id, "
            "manual_review_status, residual_amount_cents, residual_account_id, "
            "baseline_finalization_id FROM allocation_batches WHERE id=:batch_id"
        ),
        {"batch_id": batch_id},
    ).mappings().one())
    batch["recorded_at"] = _timestamp_text(batch["recorded_at"])
    rows = [{"kind": "batch", **batch}]
    lines = db.execute(
        text(
            "SELECT id, line_order, source_kind, invoice_or_bill_id, gross_cents, gst_cents "
            "FROM allocation_batch_lines WHERE batch_id=:batch_id ORDER BY line_order, id"
        ),
        {"batch_id": batch_id},
    ).mappings().all()
    for line in lines:
        rows.append({
            "kind": "line",
            "line_order": line["line_order"],
            "source_kind": line["source_kind"],
            "invoice_or_bill_id": line["invoice_or_bill_id"],
            "gross_cents": line["gross_cents"],
            "gst_cents": line["gst_cents"],
        })
        components = db.execute(
            text(
                "SELECT tax_code, gross_cents, gst_cents FROM allocation_batch_tax_components "
                "WHERE allocation_batch_line_id=:line_id ORDER BY tax_code, id"
            ),
            {"line_id": line["id"]},
        ).mappings().all()
        rows.extend({
            "kind": "component",
            "line_order": line["line_order"],
            "tax_code": component["tax_code"],
            "gross_cents": component["gross_cents"],
            "gst_cents": component["gst_cents"],
        } for component in components)
    return rows


def _projected_vector(lines: Sequence[Any]) -> tuple[tuple[str, str, int, int], ...]:
    return _normalize_vector(
        [
            (line.source_kind, tax_code, gross_cents, gst_cents)
            for line in lines
            for tax_code, gross_cents, gst_cents in line.tax_components
        ]
    )


def _derive_before_projection(
    db: Session,
    *,
    bank_transaction_id: int,
    expected_prior_version: int,
    effective_date: date | datetime | str,
    recorded_at: date | datetime | str,
    linked_prior_batch_id: int | None,
) -> Any:
    from .allocation_projection import project_allocation_state

    projection = project_allocation_state(
        db,
        bank_transaction_id,
        _effective_date(effective_date),
        _recorded_at(recorded_at),
    )
    if projection.certainty != "complete":
        raise UncertainProjectionError("Allocation before-state is not completely projectable")
    if expected_prior_version == 0:
        if projection.source != "complete_baseline":
            raise UncertainProjectionError("Version-zero allocation state requires a complete baseline")
    elif (
        projection.source != "finalized_batch"
        or projection.selected_batch_version != expected_prior_version
        or projection.selected_batch_id != linked_prior_batch_id
    ):
        raise UncertainProjectionError("Allocation before-state is not the immediately prior finalized batch")
    return projection


def _canonical_request(
    *,
    operation: str,
    bank_transaction_id: int,
    expected_prior_version: int,
    effective_date: date | datetime | str,
    recorded_at: date | datetime | str,
    idempotency_key: str,
    reason: str,
    evidence_reference: str,
    evidence_sha256: str | None,
    actor_placeholder: str,
    linked_prior_batch_id: int | None,
    internal_correction_approval_id: int | None,
    lines: Sequence[AllocationLineSpec | dict[str, Any]],
    residual_cents: int,
    residual_account_id: int | None,
    baseline_finalization_id: int,
) -> tuple[dict[str, Any], list[AllocationLineSpec], str]:
    normalized_lines = [_normalize_line(item) for item in lines]
    batch_payload = _batch_payload(
        operation=operation,
        bank_transaction_id=bank_transaction_id,
        expected_prior_version=expected_prior_version,
        effective_date=effective_date,
        recorded_at=recorded_at,
        idempotency_key=idempotency_key,
        reason=reason,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha256,
        actor_placeholder=actor_placeholder,
        linked_prior_batch_id=linked_prior_batch_id,
        internal_correction_approval_id=internal_correction_approval_id,
        residual_cents=residual_cents,
        residual_account_id=residual_account_id,
        baseline_finalization_id=baseline_finalization_id,
    )
    request_hash = _hash_payload(_payload_rows(batch_payload, normalized_lines))
    return batch_payload, normalized_lines, request_hash


def _finalization_integrity_hash(db: Session, batch_id: int) -> str:
    return db.execute(
        text(
            "SELECT allocation_baseline_sha256(payload) FROM ("
            "SELECT json_object('kind','batch',"
            "'bank_transaction_id',b.bank_transaction_id,'operation',b.operation,"
            "'transaction_version',b.transaction_version,'expected_prior_version',b.expected_prior_version,"
            "'effective_date',b.effective_date,'recorded_at',b.recorded_at,"
            "'idempotency_key',b.idempotency_key,'reason',b.reason,"
            "'evidence_reference',b.evidence_reference,'evidence_sha256',b.evidence_sha256,"
            "'actor_placeholder',b.actor_placeholder,'linked_prior_batch_id',b.linked_prior_batch_id,"
            "'internal_correction_approval_id',b.internal_correction_approval_id,"
            "'manual_review_status',b.manual_review_status,"
            "'residual_amount_cents',b.residual_amount_cents,'residual_account_id',b.residual_account_id,"
            "'baseline_finalization_id',b.baseline_finalization_id) AS payload "
            "FROM allocation_batches b WHERE b.id=:batch_id "
            "UNION ALL SELECT json_object('kind','line','line_order',l.line_order,"
            "'source_kind',l.source_kind,'invoice_or_bill_id',l.invoice_or_bill_id,"
            "'gross_cents',l.gross_cents,'gst_cents',l.gst_cents) "
            "FROM allocation_batch_lines l WHERE l.batch_id=:batch_id "
            "UNION ALL SELECT json_object('kind','component','line_order',l.line_order,"
            "'tax_code',c.tax_code,'gross_cents',c.gross_cents,'gst_cents',c.gst_cents) "
            "FROM allocation_batch_lines l JOIN allocation_batch_tax_components c "
            "ON c.allocation_batch_line_id=l.id WHERE l.batch_id=:batch_id) AS manifest"
        ),
        {"batch_id": batch_id},
    ).scalar_one()


def _normalize_vector(
    values: Sequence[tuple[str, str, int, int]],
) -> tuple[tuple[str, str, int, int], ...]:
    totals: dict[tuple[str, str], list[int]] = {}
    for source_kind, tax_code, gross_cents, gst_cents in values:
        source_key = (str(source_kind).strip(), str(tax_code).strip())
        gross = _require_cents(gross_cents, "vector gross_cents", allow_zero=False)
        gst = _require_cents(gst_cents, "vector gst_cents")
        if gst > gross:
            raise InvalidAllocationStateError("vector gst_cents cannot exceed gross_cents")
        totals.setdefault(source_key, [0, 0])
        totals[source_key][0] += gross
        totals[source_key][1] += gst
    return tuple(sorted(
        (source_kind, tax_code, gross, gst)
        for (source_kind, tax_code), (gross, gst) in totals.items()
    ))


def _current_latest_batch(db: Session, bank_transaction_id: int) -> dict[str, Any] | None:
    return db.execute(
        text(
            "SELECT * FROM allocation_batches WHERE bank_transaction_id = :bank_transaction_id ORDER BY transaction_version DESC, id DESC LIMIT 1"
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).mappings().first()


def _existing_idempotency_batch(db: Session, key: str) -> dict[str, Any] | None:
    return db.execute(
        text(
            "SELECT * FROM allocation_batches WHERE idempotency_key = :idempotency_key ORDER BY transaction_version DESC, id DESC LIMIT 1"
        ),
        {"idempotency_key": key},
    ).mappings().first()


def _validate_priors(db: Session, bank_transaction_id: int, expected_prior_version: int) -> None:
    latest = _current_latest_batch(db, bank_transaction_id)
    if latest is None:
        if expected_prior_version != 0:
            raise StaleVersionError("expected_prior_version does not match the current transaction state")
        return
    current_version = int(latest["transaction_version"])
    if expected_prior_version != current_version:
        raise StaleVersionError("expected_prior_version does not match the current latest version")


def _manual_review_status(before_vector: Sequence[tuple[str, str, int, int]], after_vector: Sequence[tuple[str, str, int, int]]) -> str:
    """Allow unchanged BAS vectors only; this checkpoint has no approval path."""
    if tuple(before_vector) == tuple(after_vector):
        return "not_required"
    raise ManualReviewRequiredError("Allocation batch changes BAS-box vectors and requires manual review; no batch created")


def _refresh_paid_state_cache(
    db: Session,
    *,
    baseline_finalization_id: int,
    before_lines: Sequence[Any],
    after_lines: Sequence[AllocationLineSpec],
) -> None:
    invoice_ids = {
        int(row[0])
        for row in db.execute(
            text(
                "SELECT DISTINCT invoice_or_bill_id "
                "FROM allocation_history_baseline_bindings "
                "WHERE baseline_event_id=:baseline_id AND source_kind IN ('invoice','bill')"
            ),
            {"baseline_id": baseline_finalization_id},
        ).all()
    }
    invoice_ids.update(
        int(line.invoice_or_bill_id)
        for line in before_lines
        if line.source_kind in {"invoice", "bill"}
    )
    invoice_ids.update(
        int(line.invoice_or_bill_id)
        for line in after_lines
        if line.source_kind in {"invoice", "bill"}
    )
    if not invoice_ids:
        return

    invoice_filter = bindparam("invoice_ids", expanding=True)
    contributing_transactions = db.execute(
        text(
            "SELECT DISTINCT bank_transaction_id FROM invoice_payment_allocations "
            "WHERE invoice_id IN :invoice_ids UNION "
            "SELECT DISTINCT bank_transaction_id FROM allocation_history_baseline_bindings "
            "WHERE invoice_or_bill_id IN :invoice_ids AND source_kind IN ('invoice','bill') "
            "UNION SELECT DISTINCT b.bank_transaction_id FROM allocation_batches b "
            "JOIN allocation_batch_lines l ON l.batch_id=b.id "
            "WHERE l.invoice_or_bill_id IN :invoice_ids AND l.source_kind IN ('invoice','bill')"
        ).bindparams(invoice_filter),
        {"invoice_ids": sorted(invoice_ids)},
    ).scalars().all()

    today = current_date()
    known_at = datetime.combine(today, datetime.max.time())
    paid_cents: dict[int, int] = {invoice_id: 0 for invoice_id in invoice_ids}
    paid_dates: dict[int, date] = {}
    from .allocation_projection import project_allocation_state

    for transaction_id in contributing_transactions:
        transaction_date_row = db.execute(
            text("SELECT occurred_at FROM bank_transactions WHERE id=:transaction_id"),
            {"transaction_id": transaction_id},
        ).scalar_one_or_none()
        settlement_date = _effective_date(transaction_date_row) if transaction_date_row else None
        if settlement_date is None or settlement_date > today:
            continue
        projection = project_allocation_state(db, int(transaction_id), today, known_at)
        if projection.source == "legacy_current":
            lines = projection.ordered_allocation_lines
        elif projection.certainty == "complete":
            lines = projection.ordered_allocation_lines
            projected_settlement_date = (
                projection.effective_state_date
                if projection.source == "finalized_batch"
                else projection.baseline_boundary
            )
            if projected_settlement_date is not None:
                settlement_date = projected_settlement_date
        else:
            raise UncertainProjectionError(
                f"Payment state for bank transaction {transaction_id} is not completely projectable"
            )
        if settlement_date is None or settlement_date > today:
            continue
        for line in lines:
            invoice_id = int(line.invoice_or_bill_id)
            if line.source_kind not in {"invoice", "bill"} or invoice_id not in paid_cents:
                continue
            paid_cents[invoice_id] += int(line.gross_cents)
            paid_dates[invoice_id] = max(paid_dates.get(invoice_id, settlement_date), settlement_date)

    refresh_projected_paid_state_cache(
        db,
        {
            invoice_id: (cents, paid_dates.get(invoice_id))
            for invoice_id, cents in paid_cents.items()
        },
    )


def _insert_batch_rows(
    db: Session,
    *,
    batch_payload: dict[str, Any],
    lines: Sequence[AllocationLineSpec],
    residual_cents: int,
    bank_cash_cents: int,
    canonical_payload_hash: str,
) -> tuple[int, str]:
    ordered_lines = lines
    row = db.execute(
        text(
            """
            INSERT INTO allocation_batches (
                bank_transaction_id, operation, transaction_version,
                expected_prior_version, effective_date, recorded_at,
                idempotency_key, canonical_payload_hash, reason,
                evidence_reference, evidence_sha256, actor_placeholder,
                linked_prior_batch_id, internal_correction_approval_id,
                manual_review_status, residual_amount_cents,
                residual_account_id, baseline_finalization_id
            ) VALUES (
                :bank_transaction_id, :operation, :transaction_version,
                :expected_prior_version, :effective_date, :recorded_at,
                :idempotency_key, :canonical_payload_hash, :reason,
                :evidence_reference, :evidence_sha256, :actor_placeholder,
                :linked_prior_batch_id, :internal_correction_approval_id,
                :manual_review_status, :residual_amount_cents,
                :residual_account_id, :baseline_finalization_id
            ) RETURNING id
            """
        ),
        {
            **batch_payload,
            "canonical_payload_hash": canonical_payload_hash,
            "manual_review_status": "not_required",
        },
    ).scalar_one()

    for index, line in enumerate(ordered_lines):
        line_id = db.execute(
            text(
                "INSERT INTO allocation_batch_lines (batch_id, line_order, source_kind, invoice_or_bill_id, gross_cents, gst_cents) VALUES (:batch_id, :line_order, :source_kind, :invoice_or_bill_id, :gross_cents, :gst_cents) RETURNING id"
            ),
            {
                "batch_id": row,
                "line_order": index,
                "source_kind": line.source_kind,
                "invoice_or_bill_id": line.invoice_or_bill_id,
                "gross_cents": line.gross_cents,
                "gst_cents": line.gst_cents,
            },
        ).scalar_one()
        for code, gross, gst in line.tax_components:
            db.execute(
                text(
                    "INSERT INTO allocation_batch_tax_components (allocation_batch_line_id, tax_code, gross_cents, gst_cents) VALUES (:allocation_batch_line_id, :tax_code, :gross_cents, :gst_cents)"
                ),
                {
                    "allocation_batch_line_id": line_id,
                    "tax_code": code,
                    "gross_cents": gross,
                    "gst_cents": gst,
                },
            )
    finalization_hash = _finalization_integrity_hash(db, row)
    if finalization_hash != canonical_payload_hash:
        raise InvalidAllocationStateError(
            "Persisted allocation batch does not match its canonical trigger manifest"
        )
    total_allocated = sum(line.gross_cents for line in ordered_lines)
    total_gst = sum(line.gst_cents for line in ordered_lines)
    db.execute(
        text(
            "INSERT INTO allocation_batch_finalizations (batch_id, allocation_line_count, component_count, total_allocated_cents, total_gst_cents, residual_cents, bank_cash_cents, canonical_integrity_hash, finalized_at) VALUES (:batch_id, :allocation_line_count, :component_count, :total_allocated_cents, :total_gst_cents, :residual_cents, :bank_cash_cents, :canonical_integrity_hash, :finalized_at)"
        ),
        {
            "batch_id": row,
            "allocation_line_count": len(ordered_lines),
            "component_count": sum(len(line.tax_components) for line in ordered_lines),
            "total_allocated_cents": total_allocated,
            "total_gst_cents": total_gst,
            "residual_cents": residual_cents,
            "bank_cash_cents": bank_cash_cents,
            "canonical_integrity_hash": finalization_hash,
            "finalized_at": batch_payload["recorded_at"],
        },
    )
    return row, finalization_hash


def _replay_existing_batch(
    db: Session,
    existing: Any,
    *,
    bank_cash_cents: int,
) -> BatchWriteResult:
    batch_id = int(existing["id"])
    finalization = db.execute(
        text(
            "SELECT allocation_line_count, component_count, total_allocated_cents, "
            "total_gst_cents, residual_cents, bank_cash_cents, canonical_integrity_hash "
            "FROM allocation_batch_finalizations WHERE batch_id=:batch_id"
        ),
        {"batch_id": batch_id},
    ).mappings().one_or_none()
    if finalization is None:
        raise InvalidAllocationStateError("Existing allocation batch is not finalized")

    line_state = db.execute(
        text(
            "SELECT "
            "(SELECT COUNT(*) FROM allocation_batch_lines WHERE batch_id=:batch_id) line_count, "
            "(SELECT COUNT(*) FROM allocation_batch_lines l JOIN allocation_batch_tax_components c "
            "ON c.allocation_batch_line_id=l.id WHERE l.batch_id=:batch_id) component_count, "
            "(SELECT COALESCE(SUM(gross_cents),0) FROM allocation_batch_lines WHERE batch_id=:batch_id) gross_total, "
            "(SELECT COALESCE(SUM(gst_cents),0) FROM allocation_batch_lines WHERE batch_id=:batch_id) gst_total, "
            "(SELECT COUNT(*) FROM (SELECT l.id FROM allocation_batch_lines l "
            "LEFT JOIN allocation_batch_tax_components c ON c.allocation_batch_line_id=l.id "
            "WHERE l.batch_id=:batch_id GROUP BY l.id HAVING COUNT(c.id)=0 "
            "OR l.gross_cents!=COALESCE(SUM(c.gross_cents),-1) "
            "OR l.gst_cents!=COALESCE(SUM(c.gst_cents),-1))) invalid_lines"
        ),
        {"batch_id": batch_id},
    ).mappings().one()
    persisted_hash = _finalization_integrity_hash(db, batch_id)
    baseline_valid = db.execute(
        text(
            "SELECT 1 FROM allocation_history_baseline_finalizations f "
            "WHERE f.baseline_event_id=:baseline_id "
            "AND f.bank_transaction_id=:bank_transaction_id AND f.status='complete' "
            "AND :recorded_at>=f.observed_at AND :effective_date>=f.observed_payment_date"
        ),
        {
            "baseline_id": existing["baseline_finalization_id"],
            "bank_transaction_id": existing["bank_transaction_id"],
            "recorded_at": existing["recorded_at"],
            "effective_date": existing["effective_date"],
        },
    ).first()
    prior_valid = (
        existing["linked_prior_batch_id"] is None
        if int(existing["expected_prior_version"]) == 0
        else db.execute(
            text(
                "SELECT 1 FROM allocation_batches p JOIN allocation_batch_finalizations f "
                "ON f.batch_id=p.id WHERE p.id=:prior_id "
                "AND p.bank_transaction_id=:bank_transaction_id "
                "AND p.transaction_version=:expected_prior_version"
            ),
            {
                "prior_id": existing["linked_prior_batch_id"],
                "bank_transaction_id": existing["bank_transaction_id"],
                "expected_prior_version": existing["expected_prior_version"],
            },
        ).first() is not None
    )
    if (
        int(existing["transaction_version"]) != int(existing["expected_prior_version"]) + 1
        or existing["manual_review_status"] != "not_required"
        or not prior_valid
        or baseline_valid is None
        or line_state["invalid_lines"]
        or finalization["allocation_line_count"] != line_state["line_count"]
        or finalization["component_count"] != line_state["component_count"]
        or finalization["total_allocated_cents"] != line_state["gross_total"]
        or finalization["total_gst_cents"] != line_state["gst_total"]
        or finalization["residual_cents"] != existing["residual_amount_cents"]
        or finalization["bank_cash_cents"] != bank_cash_cents
        or finalization["total_allocated_cents"] + finalization["residual_cents"] != bank_cash_cents
        or finalization["canonical_integrity_hash"] != existing["canonical_payload_hash"]
        or finalization["canonical_integrity_hash"] != persisted_hash
    ):
        raise InvalidAllocationStateError(
            "Existing allocation batch or finalization is inconsistent"
        )

    return BatchWriteResult(
        batch_id=batch_id,
        transaction_version=int(existing["transaction_version"]),
        expected_prior_version=int(existing["expected_prior_version"]),
        operation=str(existing["operation"]),
        manual_review_status=str(existing["manual_review_status"]),
        bank_cash_cents=bank_cash_cents,
        bank_transaction_id=int(existing["bank_transaction_id"]),
        baseline_finalization_id=int(existing["baseline_finalization_id"]),
        finalization_integrity_hash=str(finalization["canonical_integrity_hash"]),
    )


@rollback_session_on_error
def _build_shared_writer(
    db: Session,
    *,
    operation: str,
    bank_transaction_id: int,
    lines: Sequence[AllocationLineSpec | dict[str, Any]],
    effective_date: date | datetime | str,
    recorded_at: date | datetime | str,
    idempotency_key: str,
    reason: str,
    evidence_reference: str,
    evidence_sha256: str | None,
    actor_placeholder: str,
    residual_cents: int,
    residual_account_id: int | None,
    baseline_finalization_id: int,
    linked_prior_batch_id: int | None,
    expected_prior_version: int,
    books_locked_through: date | None | object,
) -> BatchWriteResult:
    _require_open_effective_date(
        effective_date,
        books_locked_through,
        operation=f"write an {operation.replace('_', ' ')}",
    )
    begin_sqlite_immediate(db)
    idempotency_key = _canonical_idempotency_key(idempotency_key)
    existing = _existing_idempotency_batch(db, idempotency_key)
    if existing is not None and (
        existing["bank_transaction_id"] != bank_transaction_id
        or existing["operation"] != operation
    ):
        raise IdempotencyConflictError(
            "This idempotency key has already been used with a different transaction or operation"
        )
    try:
        batch_payload, normalized_lines, request_payload_hash = _canonical_request(
            operation=operation,
            bank_transaction_id=bank_transaction_id,
            expected_prior_version=expected_prior_version,
            effective_date=effective_date,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
            reason=reason,
            evidence_reference=evidence_reference,
            evidence_sha256=evidence_sha256,
            actor_placeholder=actor_placeholder,
            linked_prior_batch_id=linked_prior_batch_id,
            internal_correction_approval_id=None,
            lines=lines,
            residual_cents=residual_cents,
            residual_account_id=residual_account_id,
            baseline_finalization_id=baseline_finalization_id,
        )
    except (InvalidAllocationStateError, InvalidExactMoneyInputError, TypeError, ValueError, KeyError) as exc:
        if existing is not None:
            raise IdempotencyConflictError(
                "This idempotency key has already been used with a different payload"
            ) from exc
        raise InvalidAllocationStateError("Allocation request is malformed") from exc

    if existing is not None:
        try:
            existing_request_hash = _hash_payload(
                _stored_request_payload_rows(db, int(existing["id"]))
            )
        except (KeyError, TypeError, ValueError, NoResultFound) as exc:
            raise InvalidAllocationStateError(
                "Existing allocation batch request state is malformed"
            ) from exc
        if existing_request_hash != request_payload_hash:
            raise IdempotencyConflictError(
                "This idempotency key has already been used with a different payload"
            )
        transaction_row = db.execute(
            text(
                "SELECT CAST(amount AS TEXT) FROM bank_transactions "
                "WHERE id=:bank_transaction_id"
            ),
            {"bank_transaction_id": bank_transaction_id},
        ).one_or_none()
        if transaction_row is None:
            raise InvalidAllocationStateError("Existing allocation batch has no bank transaction")
        bank_cash_cents = parse_exact_money(transaction_row[0])
        return _replay_existing_batch(
            db,
            existing,
            bank_cash_cents=bank_cash_cents,
        )

    if not normalized_lines and operation != "allocation_reversal":
        raise InvalidAllocationStateError("A batch must contain a complete after-state allocation line set")
    transaction_row = db.execute(
        text(
            "SELECT CAST(amount AS TEXT) FROM bank_transactions WHERE id = :bank_transaction_id"
        ),
        {"bank_transaction_id": bank_transaction_id},
    ).one_or_none()
    if transaction_row is None:
        raise TransactionNotFoundError(f"Bank transaction {bank_transaction_id} does not exist")
    bank_cash_cents = parse_exact_money(transaction_row[0])
    residual_cents = _require_cents(residual_cents, "residual_cents")
    if (
        not isinstance(expected_prior_version, int)
        or isinstance(expected_prior_version, bool)
        or expected_prior_version < 0
    ):
        raise StaleVersionError("expected_prior_version must be a non-negative integer")
    if existing is None:
        _validate_priors(db, bank_transaction_id, expected_prior_version)

    total_allocated = sum(line.gross_cents for line in normalized_lines)
    if bank_cash_cents != total_allocated + residual_cents:
        raise InvalidAllocationStateError(
            "Allocation total and residual must reconcile to exact bank cash cents"
        )
    after_vector = _vector_for_lines(normalized_lines)
    before_projection = _derive_before_projection(
        db,
        bank_transaction_id=bank_transaction_id,
        expected_prior_version=expected_prior_version,
        effective_date=effective_date,
        recorded_at=recorded_at,
        linked_prior_batch_id=linked_prior_batch_id,
    )
    manual_review_status = _manual_review_status(
        _projected_vector(before_projection.ordered_allocation_lines),
        after_vector,
    )
    canonical_payload_hash = allocation_baseline_integrity_hash(
        _payload_rows(batch_payload, normalized_lines)
    )

    try:
        with db.begin_nested():
            batch_id, finalization_hash = _insert_batch_rows(
                db,
                batch_payload=batch_payload,
                lines=normalized_lines,
                residual_cents=residual_cents,
                bank_cash_cents=bank_cash_cents,
                canonical_payload_hash=canonical_payload_hash,
            )
    except IntegrityError as exc:
        original = str(getattr(exc, "orig", exc))
        if original in {
            "Invalid allocation batch",
            "Invalid or finalized allocation batch line",
            "Invalid or finalized allocation batch component",
            "Allocation batch does not reconcile or requires manual GST review",
        }:
            raise InvalidAllocationStateError(
                "Allocation batch failed database validation"
            ) from exc
        raise
    _refresh_paid_state_cache(
        db,
        baseline_finalization_id=baseline_finalization_id,
        before_lines=before_projection.ordered_allocation_lines,
        after_lines=normalized_lines,
    )
    db.flush()
    return BatchWriteResult(
        batch_id=batch_id,
        transaction_version=expected_prior_version + 1,
        expected_prior_version=expected_prior_version,
        operation=operation,
        manual_review_status=manual_review_status,
        bank_cash_cents=bank_cash_cents,
        bank_transaction_id=bank_transaction_id,
        baseline_finalization_id=baseline_finalization_id,
        finalization_integrity_hash=finalization_hash,
    )


def write_allocation_correction(
    db: Session,
    *,
    bank_transaction_id: int,
    effective_date: date | datetime | str,
    recorded_at: date | datetime | str,
    idempotency_key: str,
    reason: str,
    evidence_reference: str,
    actor_placeholder: str,
    expected_prior_version: int,
    lines: Sequence[AllocationLineSpec | dict[str, Any]],
    residual_cents: int,
    residual_account_id: int | None,
    baseline_finalization_id: int,
    linked_prior_batch_id: int | None = None,
    evidence_sha256: str | None = None,
    books_locked_through: date | None | object = _MISSING_LOCK_CONTEXT,
) -> BatchWriteResult:
    return _build_shared_writer(
        db=db,
        operation="allocation_correction",
        bank_transaction_id=bank_transaction_id,
        lines=lines,
        effective_date=effective_date,
        recorded_at=recorded_at,
        idempotency_key=idempotency_key,
        reason=reason,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha256,
        actor_placeholder=actor_placeholder,
        residual_cents=residual_cents,
        residual_account_id=residual_account_id,
        baseline_finalization_id=baseline_finalization_id,
        linked_prior_batch_id=linked_prior_batch_id,
        expected_prior_version=expected_prior_version,
        books_locked_through=books_locked_through,
    )


def write_allocation_reversal(
    db: Session,
    *,
    bank_transaction_id: int,
    effective_date: date | datetime | str,
    recorded_at: date | datetime | str,
    idempotency_key: str,
    reason: str,
    evidence_reference: str,
    actor_placeholder: str,
    expected_prior_version: int,
    lines: Sequence[AllocationLineSpec | dict[str, Any]],
    residual_cents: int,
    residual_account_id: int | None,
    baseline_finalization_id: int,
    linked_prior_batch_id: int | None = None,
    evidence_sha256: str | None = None,
    books_locked_through: date | None | object = _MISSING_LOCK_CONTEXT,
) -> BatchWriteResult:
    return _build_shared_writer(
        db=db,
        operation="allocation_reversal",
        bank_transaction_id=bank_transaction_id,
        lines=lines,
        effective_date=effective_date,
        recorded_at=recorded_at,
        idempotency_key=idempotency_key,
        reason=reason,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha256,
        actor_placeholder=actor_placeholder,
        residual_cents=residual_cents,
        residual_account_id=residual_account_id,
        baseline_finalization_id=baseline_finalization_id,
        linked_prior_batch_id=linked_prior_batch_id,
        expected_prior_version=expected_prior_version,
        books_locked_through=books_locked_through,
    )


def write_allocation_reallocation(
    db: Session,
    *,
    bank_transaction_id: int,
    effective_date: date | datetime | str,
    recorded_at: date | datetime | str,
    idempotency_key: str,
    reason: str,
    evidence_reference: str,
    actor_placeholder: str,
    expected_prior_version: int,
    lines: Sequence[AllocationLineSpec | dict[str, Any]],
    residual_cents: int,
    residual_account_id: int | None,
    baseline_finalization_id: int,
    linked_prior_batch_id: int | None = None,
    evidence_sha256: str | None = None,
    books_locked_through: date | None | object = _MISSING_LOCK_CONTEXT,
) -> BatchWriteResult:
    return _build_shared_writer(
        db=db,
        operation="allocation_reallocation",
        bank_transaction_id=bank_transaction_id,
        lines=lines,
        effective_date=effective_date,
        recorded_at=recorded_at,
        idempotency_key=idempotency_key,
        reason=reason,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha256,
        actor_placeholder=actor_placeholder,
        residual_cents=residual_cents,
        residual_account_id=residual_account_id,
        baseline_finalization_id=baseline_finalization_id,
        linked_prior_batch_id=linked_prior_batch_id,
        expected_prior_version=expected_prior_version,
        books_locked_through=books_locked_through,
    )


create_allocation_correction = write_allocation_correction
create_allocation_reversal = write_allocation_reversal
create_allocation_reallocation = write_allocation_reallocation


__all__ = [
    "AllocationLineSpec",
    "BatchWriteResult",
    "BaselineIncompleteError",
    "BaselineMissingError",
    "ConcurrentWriteConflictError",
    "IdempotencyConflictError",
    "InternalLodgedPeriodApprovalMissingError",
    "InvalidAllocationStateError",
    "InvalidExactMoneyInputError",
    "InvalidResidualAccountError",
    "ManualReviewRequiredError",
    "PeriodLockedError",
    "PreBaselineOperationError",
    "ReversalInsideMarkedPeriodError",
    "StaleVersionError",
    "TransactionNotFoundError",
    "UncertainProjectionError",
    "parse_exact_money",
    "write_allocation_correction",
    "write_allocation_reversal",
    "write_allocation_reallocation",
    "create_allocation_correction",
    "create_allocation_reversal",
    "create_allocation_reallocation",
]
