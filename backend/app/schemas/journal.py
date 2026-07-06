from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ._money import Money


class JournalLineCreate(BaseModel):
    account_id: int
    debit_amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    credit_amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    description: str | None = Field(default=None, max_length=500)


class JournalEntryCreate(BaseModel):
    entry_date: date
    memo: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=80)
    lines: list[JournalLineCreate] = Field(min_length=2)


class JournalEntryUpdate(BaseModel):
    entry_date: date | None = None
    memo: str | None = Field(default=None, min_length=1, max_length=500)
    reference: str | None = Field(default=None, max_length=80)
    # Lines, when present, fully replace the existing lines. Omit to keep them.
    lines: list[JournalLineCreate] | None = Field(default=None, min_length=2)


class JournalLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    debit_amount: Money
    credit_amount: Money
    description: str | None


class JournalEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_date: date
    memo: str
    reference: str | None
    source_type: str
    source_id: int | None
    reverses_entry_id: int | None
    created_at: datetime
    updated_at: datetime
    lines: list[JournalLineOut]
