"""Invoice total validation shared by API and ledger posting."""

from __future__ import annotations

from decimal import Decimal


GST_TOLERANCE = Decimal("0.02")


class GstMathError(ValueError):
    pass


def check_gst_math(subtotal: Decimal, gst_amount: Decimal, total: Decimal) -> None:
    diff = Decimal(total) - (Decimal(subtotal) + Decimal(gst_amount))
    if abs(diff) > GST_TOLERANCE:
        raise GstMathError(
            f"GST math doesn't balance: subtotal {subtotal} + gst {gst_amount} "
            f"!= total {total} (diff {diff}). Check the input."
        )
