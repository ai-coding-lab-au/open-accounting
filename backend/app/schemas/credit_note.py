from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._dates import check_reportable_date
from ._limits import SQLITE_EXACT_MONEY_MAX, SQLITE_INT_MAX
from ._money import Money


class CreditNoteLineDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_invoice_line_id: int = Field(ge=1, le=SQLITE_INT_MAX)
    quantity: Decimal = Field(
        gt=0,
        le=Decimal("999999.9999"),
        max_digits=12,
        decimal_places=4,
    )


class CreditNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_invoice_id: int = Field(ge=1, le=SQLITE_INT_MAX)
    credit_note_number: str = Field(min_length=1, max_length=80)
    issue_date: date
    notes: str | None = Field(default=None, max_length=1000)
    lines: list[CreditNoteLineDraftIn] = Field(min_length=1)

    @field_validator("credit_note_number")
    @classmethod
    def _number_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("credit_note_number must not be blank")
        return value

    @field_validator("issue_date")
    @classmethod
    def _reportable_issue_date(cls, value: date) -> date:
        return check_reportable_date(value, field_name="issue_date")


class CreditNoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_note_number: str | None = Field(default=None, min_length=1, max_length=80)
    issue_date: date | None = None
    notes: str | None = Field(default=None, max_length=1000)
    lines: list[CreditNoteLineDraftIn] | None = Field(default=None, min_length=1)

    @field_validator("credit_note_number")
    @classmethod
    def _number_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("credit_note_number must not be blank")
        return value

    @field_validator("issue_date")
    @classmethod
    def _reportable_issue_date(cls, value: date | None) -> date | None:
        if value is None:
            return None
        return check_reportable_date(value, field_name="issue_date")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in ("credit_note_number", "issue_date", "lines"):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data


class CreditNoteLineOut(BaseModel):
    id: int
    source_invoice_line_id: int
    description: str
    account_id: int
    quantity: Decimal
    unit_price: Money
    gst_rate: Decimal
    line_subtotal: Money
    line_gst: Money
    line_total: Money
    tax_code: str


class CreditNoteApplicationOut(BaseModel):
    id: int
    invoice_id: int
    amount: Money
    application_date: date
    status: str
    created_at: datetime
    updated_at: datetime
    reversed_at: datetime | None
    reversal_date: date | None


class CreditNoteOut(BaseModel):
    id: int
    source_invoice_id: int
    source_invoice_number: str
    direction: str
    contact_id: int
    contact_name: str
    credit_note_number: str
    issue_date: date
    currency: str
    subtotal: Money
    gst_amount: Money
    total: Money
    gst_inclusive: bool
    status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime
    applied_amount: Money
    refunded_amount: Money
    remaining_amount: Money
    applications: list[CreditNoteApplicationOut]
    refunds: list[CreditNoteRefundOut]
    lines: list[CreditNoteLineOut]


class CreditNoteApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: int = Field(ge=1, le=SQLITE_INT_MAX)
    amount: Decimal = Field(
        gt=0,
        le=Decimal("999999.9999"),
        max_digits=16,
        decimal_places=2,
    )
    application_date: date

    @field_validator("application_date")
    @classmethod
    def _reportable_application_date(cls, value: date) -> date:
        return check_reportable_date(value, field_name="application_date")


class CreditNoteApplicationReverse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reversal_date: date

    @field_validator("reversal_date")
    @classmethod
    def _reportable_reversal_date(cls, value: date) -> date:
        return check_reportable_date(value, field_name="reversal_date")


class CreditNoteRefundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_account_id: int = Field(ge=1, le=SQLITE_INT_MAX)
    amount: Decimal = Field(
        gt=0,
        le=Decimal("999999.9999"),
        max_digits=16,
        decimal_places=2,
    )
    refund_date: date

    @field_validator("refund_date")
    @classmethod
    def _reportable_refund_date(cls, value: date) -> date:
        return check_reportable_date(value, field_name="refund_date")


class CreditNoteRefundReverse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reversal_date: date

    @field_validator("reversal_date")
    @classmethod
    def _reportable_reversal_date(cls, value: date) -> date:
        return check_reportable_date(value, field_name="reversal_date")


class CreditNoteVoid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    void_date: date


class CreditNoteRefundOut(BaseModel):
    id: int
    credit_note_id: int
    bank_account_id: int
    bank_transaction_id: int
    journal_entry_id: int
    amount: Money
    refund_date: date
    status: str
    reversed_at: datetime | None
    reversal_date: date | None
    reversal_bank_transaction_id: int | None
    reversal_journal_entry_id: int | None
    created_at: datetime
    updated_at: datetime


class CreditNoteSourceLineOut(BaseModel):
    source_invoice_line_id: int
    description: str
    account_id: int
    quantity: Decimal
    unit_price: Money
    gst_rate: Decimal
    tax_code: str
    line_subtotal: Money
    line_gst: Money
    line_total: Money
    quantity_reserved: Decimal
    remaining_creditable_quantity: Decimal


class CreditNoteSourceOut(BaseModel):
    source_invoice_id: int
    source_invoice_number: str
    direction: str
    contact_id: int
    contact_name: str
    issue_date: date
    currency: str
    gst_inclusive: bool
    status: str
    subtotal: Money
    gst_amount: Money
    total: Money
    paid_amount: Money
    lines: list[CreditNoteSourceLineOut]