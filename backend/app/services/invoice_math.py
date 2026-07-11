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


def check_invoice_lines(
    subtotal: Decimal,
    gst_amount: Decimal,
    total: Decimal,
    lines: Iterable[Any] | None,
) -> None:
    """Require one coherent monetary representation across lines and header."""
    rows = list(lines or [])
    if not rows:
        return

    subtotal_sum = Decimal("0")
    gst_sum = Decimal("0")
    total_sum = Decimal("0")
    for index, line in enumerate(rows, start=1):
        quantity = _value(line, "quantity")
        unit_price = _value(line, "unit_price")
        line_subtotal = _value(line, "line_subtotal")
        line_gst = _value(line, "line_gst")
        line_total = _value(line, "line_total")

        if line_total != line_subtotal + line_gst:
            raise GstMathError(
                f"Invoice line {index} doesn't balance: subtotal "
                f"{line_subtotal} + GST {line_gst} != total {line_total}."
            )
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

        subtotal_sum += line_subtotal
        gst_sum += line_gst
        total_sum += line_total

    expected = (
        Decimal(str(subtotal)),
        Decimal(str(gst_amount)),
        Decimal(str(total)),
    )
    actual = (subtotal_sum, gst_sum, total_sum)
    if actual != expected:
        raise GstMathError(
            "Invoice line totals do not match the header: "
            f"lines subtotal/GST/total={actual[0]}/{actual[1]}/{actual[2]}, "
            f"header={expected[0]}/{expected[1]}/{expected[2]}."
        )
