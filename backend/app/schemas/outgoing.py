"""Schemas for outgoing documents (Receipt only)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._money import Money

_MONEY_MAX = Decimal("99999999999999.99")
_MONEY_MIN = Decimal("-99999999999999.99")


class OutgoingLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(default=Decimal("1"), ge=0, le=Decimal("999999.9999"), max_digits=12, decimal_places=4)
    # Line money is non-negative: a receipt line records a positive amount
    # received. (Discounts/credits aren't modelled as negative lines here.)
    unit_price: Decimal = Field(
        default=Decimal("0"), ge=Decimal("0"), le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    amount: Decimal | None = Field(
        default=None, ge=Decimal("0"), le=_MONEY_MAX, max_digits=16, decimal_places=2
    )  # if None, server computes quantity * unit_price


class OutgoingLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_no: int
    description: str
    quantity: Decimal
    unit_price: Money
    amount: Money


class OutgoingCreate(BaseModel):
    # Reject unknown fields instead of silently dropping them — e.g. a caller
    # sending `doc_number` (the override field is `doc_number_override`) gets a
    # 422 rather than a document with an unexpected auto-number.
    model_config = ConfigDict(extra="forbid")

    # Receipt is the only outgoing document type; kept as a field (defaulted) so
    # existing callers that send it continue to work.
    doc_type: str = Field(default="receipt", pattern="^receipt$")
    issue_date: date

    customer_id: int | None = None
    client_ref_id: int | None = None
    customer_name: str | None = Field(default=None, min_length=1, max_length=200)
    customer_address: str | None = Field(default=None, max_length=500)
    customer_email: str | None = Field(default=None, max_length=200)
    customer_phone: str | None = Field(default=None, max_length=50)

    currency: str = Field(default="AUD", min_length=3, max_length=3)
    lines: list[OutgoingLineIn] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=1000)
    payment_method: str | None = Field(default=None, max_length=100)
    paid_date: date | None = None

    # When the user wants to override the auto-generated doc number.
    # Restricted character set: used directly in filename + URL.
    doc_number_override: str | None = Field(
        default=None, max_length=40, pattern=r"^[A-Za-z0-9._-]+$"
    )


class OutgoingUpdate(BaseModel):
    # Reject unknown fields (e.g. an attempt to PATCH `doc_number`, which is
    # immutable) instead of returning 200 while silently ignoring them.
    model_config = ConfigDict(extra="forbid")

    issue_date: date | None = None
    customer_id: int | None = None
    client_ref_id: int | None = None
    customer_name: str | None = Field(default=None, min_length=1, max_length=200)
    customer_address: str | None = Field(default=None, max_length=500)
    customer_email: str | None = Field(default=None, max_length=200)
    customer_phone: str | None = Field(default=None, max_length=50)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    lines: list[OutgoingLineIn] | None = None
    notes: str | None = Field(default=None, max_length=1000)
    status: str | None = Field(default=None, pattern="^(draft|issued|void)$")
    payment_method: str | None = Field(default=None, max_length=100)
    paid_date: date | None = None

    # issue_date / customer_name / currency map to NOT-NULL columns; they're
    # Optional only to allow omission (= no change). An explicit JSON null would
    # 500 (IntegrityError on commit). Reject explicit null here as a 422.
    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in ("issue_date", "customer_name", "currency"):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data


class OutgoingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    doc_type: str
    doc_number: str
    issue_date: date
    customer_id: int | None
    client_ref_id: int | None = None
    customer_name: str
    customer_address: str | None
    customer_abn: str | None = None
    customer_email: str | None
    customer_phone: str | None
    currency: str
    subtotal: Money
    gst_amount: Money
    total: Money
    status: str
    paid_date: date | None
    payment_method: str | None
    notes: str | None
    pdf_rel_path: str | None
    created_at: datetime
    updated_at: datetime
    lines: list[OutgoingLineOut]


class CounterOut(BaseModel):
    doc_type: str
    year: int
    last_number: int
    next_preview: str


class CounterSet(BaseModel):
    doc_type: str = Field(default="receipt", pattern="^receipt$")
    year: int = Field(ge=2000, le=3000)
    last_number: int = Field(ge=0)
