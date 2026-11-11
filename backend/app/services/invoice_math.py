"""Invoice total validation shared by API and ledger posting."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable


GST_TOLERANCE = Decimal("0.00")


class GstMathError(ValueError):
    pass


def check_gst_math(subtotal: Decimal, gst_amount: Decimal, total: Decimal) -> None:
    diff = Decimal(total) - (Decimal(subtotal) + Decimal(gst_amount))
    if abs(diff) > GST_TOLERANCE:
        raise GstMathError(
            f"GST math doesn't balance: subtotal {subtotal} + gst {gst_amount} "
            f"!= total {total} (diff {diff}). Check the input."
        )


def _value(line: Any, field: str) -> Decimal:
    raw = getattr(line, field) if hasattr(line, field) else line[field]
    return Decimal(str(raw or 0))


def _line_values(line: Any) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    return (
        _value(line, "quantity"),
        _value(line, "unit_price"),
        _value(line, "line_subtotal"),
        _value(line, "line_gst"),
        _value(line, "line_total"),
    )


def _check_line_sums(
    subtotal: Decimal,
    gst_amount: Decimal,
    total: Decimal,
    rows: list[Any],
) -> None:
    actual = tuple(
        sum((_value(line, field) for line in rows), Decimal("0"))
        for field in ("line_subtotal", "line_gst", "line_total")
    )
    expected = (Decimal(str(subtotal)), Decimal(str(gst_amount)), Decimal(str(total)))
    if actual != expected:
        raise GstMathError(
            "Invoice line totals do not match the header: "
            f"lines subtotal/GST/total={actual[0]}/{actual[1]}/{actual[2]}, "
            f"header={expected[0]}/{expected[1]}/{expected[2]}."
        )


def _check_line_balance(index: int, subtotal: Decimal, gst: Decimal, total: Decimal) -> None:
    if total != subtotal + gst:
        raise GstMathError(
            f"Invoice line {index} doesn't balance: subtotal "
            f"{subtotal} + GST {gst} != total {total}."
        )


def check_legacy_invoice_lines(
    subtotal: Decimal,
    gst_amount: Decimal,
    total: Decimal,
    lines: Iterable[Any] | None,
) -> None:
    """Preserve the pre-amount-mode create and draft-update contract."""
    rows = list(lines or [])
    if not rows:
        return

    for index, line in enumerate(rows, start=1):
        quantity, unit_price, line_subtotal, line_gst, line_total = _line_values(line)
        _check_line_balance(index, line_subtotal, line_gst, line_total)
        # Older API clients omitted unit_price while supplying line_subtotal;
        # those rows persist unit_price=0. Validate the multiplication whenever
        # a price is present (or the claimed subtotal is zero), without making
        # otherwise-consistent legacy drafts unopenable.
        if unit_price != 0 or line_subtotal == 0:
            computed = (quantity * unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if computed != line_subtotal:
                raise GstMathError(
                    f"Invoice line {index} subtotal {line_subtotal} does not "
                    f"equal quantity {quantity} × unit price {unit_price} = "
                    f"{computed}."
                )

    _check_line_sums(subtotal, gst_amount, total, rows)


def check_explicit_invoice_lines(
    subtotal: Decimal,
    gst_amount: Decimal,
    total: Decimal,
    lines: Iterable[Any] | None,
    *,
    amount_mode: str,
) -> None:
    """Strictly validate a create request that explicitly selects an amount mode."""
    rows = list(lines or [])
    if not rows:
        raise GstMathError("An explicit amount mode requires at least one invoice line.")

    for index, line in enumerate(rows, start=1):
        quantity, unit_price, line_subtotal, line_gst, line_total = _line_values(line)
        tax_code = getattr(line, "tax_code", None)
        if quantity <= 0:
            raise GstMathError(f"Invoice line {index} quantity must be greater than zero.")
        if unit_price < 0:
            raise GstMathError(f"Invoice line {index} unit price cannot be negative.")

        if amount_mode == "none" or tax_code not in {"standard", "capital"}:
            expected_subtotal = (quantity * unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            expected_gst = Decimal("0.00")
            expected_total = expected_subtotal
        elif amount_mode == "exclusive":
            expected_subtotal = (quantity * unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            expected_gst = (expected_subtotal * Decimal("0.10")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            expected_total = expected_subtotal + expected_gst
        elif amount_mode == "inclusive":
            expected_total = (quantity * unit_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            expected_gst = (expected_total / Decimal("11")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            expected_subtotal = expected_total - expected_gst
        else:
            raise GstMathError(f"Unsupported amount mode {amount_mode!r}.")

        expected_line = (expected_subtotal, expected_gst, expected_total)
        actual_line = (line_subtotal, line_gst, line_total)
        if actual_line != expected_line:
            raise GstMathError(
                f"Invoice line {index} amounts {actual_line[0]}/{actual_line[1]}/"
                f"{actual_line[2]} do not match {amount_mode} calculation "
                f"{expected_line[0]}/{expected_line[1]}/{expected_line[2]}."
            )

    check_gst_math(subtotal, gst_amount, total)
    _check_line_sums(subtotal, gst_amount, total, rows)


def check_invoice_lines(
    subtotal: Decimal,
    gst_amount: Decimal,
    total: Decimal,
    lines: Iterable[Any] | None,
) -> None:
    """Validate persisted lines without changing their stored amounts."""
    rows = list(lines or [])
    if not rows:
        return

    for index, line in enumerate(rows, start=1):
        quantity, unit_price, line_subtotal, line_gst, line_total = _line_values(line)
        _check_line_balance(index, line_subtotal, line_gst, line_total)
        # Historical rows without a unit price retain their original acceptance
        # rule. New rows with a price must match either stored representation.
        if unit_price == 0 and line_subtotal > 0:
            continue
        computed = (quantity * unit_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if computed not in {line_subtotal, line_total}:
            raise GstMathError(
                f"Invoice line {index} extended amount {computed} does not match "
                f"exclusive subtotal {line_subtotal} or inclusive total {line_total}."
            )

    _check_line_sums(subtotal, gst_amount, total, rows)
