"""Shared money serialisation helpers for API output schemas."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer


def _quantise_money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _serialise_money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}"


Money = Annotated[
    Decimal,
    AfterValidator(_quantise_money),
    PlainSerializer(_serialise_money, return_type=str, when_used="json"),
]
