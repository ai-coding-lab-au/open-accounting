"""Pydantic schemas for bank accounts and bank transactions."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._money import Money


# Reportable financial-year window. BAS fy_year is limited to 2000..2100
# (api/v1/reports.py), i.e. FY2000 = Jul 1999 .. FY2100 = Jun 2100. A
# transaction dated outside this window can never appear in any BAS return, so
# reject it at entry rather than create an unreportable "orphan" row.
MIN_TXN_DATE = date(1999, 7, 1)
MAX_TXN_DATE = date(2100, 6, 30)


def check_txn_date(value: date) -> date:
    if value < MIN_TXN_DATE or value > MAX_TXN_DATE:
        raise ValueError(
            f"occurred_at must be between {MIN_TXN_DATE.isoformat()} and "
            f"{MAX_TXN_DATE.isoformat()} (a reportable BAS financial year)"
        )
    return value


# ---------------------------------------------------------------------------
# Bank accounts
# ---------------------------------------------------------------------------


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    bsb: str | None
    account_number: str | None
    opening_balance: Money
    is_active: bool
    notes: str | None
    created_at: datetime


class BankAccountWithBalance(BankAccountOut):
    current_balance: Money  # opening_balance + signed sum of bank_transactions


class BankAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    opening_balance: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=Decimal("99999999999999.99"),
        max_digits=16,
        decimal_places=2,
    )
    bsb: str | None = Field(default=None, max_length=20)
    account_number: str | None = Field(default=None, max_length=50)
    is_active: bool = True


class BankAccountUpdate(BaseModel):
    # `extra="forbid"` so anything unexpected is rejected with a 422
    # instead of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    bsb: str | None = Field(default=None, max_length=20)
    account_number: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None


_TAX_CODE_PATTERN = r"^(standard|gst_free|input_taxed|capital|none)$"


class BankTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bank_account_id: int
    direction: str
    amount: Money
    occurred_at: date
    memo: str | None
    counter_party_name: str | None
    account_id: int | None
    gst_amount: Money
    tax_code: str
    created_at: datetime


class BankTransactionIn(BaseModel):
    """Manual entry on a bank account."""

    direction: str = Field(pattern="^(in|out)$")
    amount: Decimal = Field(gt=0, le=Decimal("99999999999999.99"), max_digits=16, decimal_places=2)
    occurred_at: date
    memo: str | None = Field(default=None, max_length=500)
    counter_party_name: str | None = Field(default=None, max_length=200)
    account_id: int | None = None
    gst_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=Decimal("99999999999999.99"),
        max_digits=16,
        decimal_places=2,
    )
    tax_code: str = Field(default="standard", pattern=_TAX_CODE_PATTERN)

    @field_validator("occurred_at")
    @classmethod
    def _validate_occurred_at(cls, v: date) -> date:
        return check_txn_date(v)


class BankTransactionRecategorise(BaseModel):
    """Inline edit from the Reconciliation page."""

    account_id: int | None = None
    tax_code: str | None = Field(default=None, pattern=_TAX_CODE_PATTERN)
    gst_amount: Decimal | None = Field(
        default=None,
        ge=0,
        le=Decimal("99999999999999.99"),
        max_digits=16,
        decimal_places=2,
    )
