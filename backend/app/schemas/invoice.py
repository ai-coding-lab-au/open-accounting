from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._money import Money


# Money column shape: NUMERIC(16, 2) → max 14 integer digits + 2 decimal.
_MONEY_MAX = Decimal("99999999999999.99")
_MONEY_MIN = Decimal("-99999999999999.99")


class InvoiceLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    account_id: int | None = None
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    gst_rate: Decimal = Decimal("0.10")
    line_subtotal: Decimal
    line_gst: Decimal = Decimal("0")
    line_total: Decimal


class InvoiceLineOut(InvoiceLineIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    unit_price: Money
    line_subtotal: Money
    line_gst: Money
    line_total: Money


class InvoiceCreate(BaseModel):
    """Payload to create an invoice (used after PDF preview confirmation, manual entry, or Excel import)."""

    direction: str = Field(pattern="^(AP|AR)$")
    contact_id: int | None = None
    contact_name: str | None = None      # if contact_id missing, server creates/looks up by name

    @field_validator("contact_name")
    @classmethod
    def _contact_name_not_blank(cls, v: str | None) -> str | None:
        # A whitespace-only name passes the column but get_or_create_contact
        # raises a bare ValueError on the empty stripped name (→ 500). Reject
        # it here as a 422; "field omitted/None" still means "use contact_id".
        if v is not None and not v.strip():
            raise ValueError("contact_name must not be blank")
        return v
    contact_abn: str | None = None
    invoice_number: str = Field(min_length=1, max_length=80)
    issue_date: date
    due_date: date | None = None
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    subtotal: Decimal = Field(ge=_MONEY_MIN, le=_MONEY_MAX, max_digits=16, decimal_places=2)
    gst_amount: Decimal = Field(
        default=Decimal("0"), ge=_MONEY_MIN, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    total: Decimal = Field(ge=_MONEY_MIN, le=_MONEY_MAX, max_digits=16, decimal_places=2)
    gst_inclusive: bool = True
    notes: str | None = Field(default=None, max_length=1000)
    source: str = Field(default="manual", pattern="^(manual|pdf|excel)$")
    source_ref: str | None = None
    status: str | None = Field(default=None, pattern="^(draft|authorised|unpaid|partial|paid|void)$")
    attachment_id: str | None = None     # link an existing uploaded file to this invoice
    lines: list[InvoiceLineIn] | None = None


class InvoiceUpdate(BaseModel):
    direction: str | None = Field(default=None, pattern="^(AP|AR)$")
    issue_date: date | None = None
    due_date: date | None = None
    contact_id: int | None = None
    invoice_number: str | None = Field(default=None, min_length=1, max_length=80)
    subtotal: Decimal | None = Field(
        default=None, ge=_MONEY_MIN, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    gst_amount: Decimal | None = Field(
        default=None, ge=_MONEY_MIN, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    total: Decimal | None = Field(
        default=None, ge=_MONEY_MIN, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    gst_inclusive: bool | None = None
    status: str | None = Field(default=None, pattern="^(draft|authorised|unpaid|partial|paid|void)$")
    paid_amount: Decimal | None = Field(
        default=None, ge=_MONEY_MIN, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    paid_date: date | None = None
    notes: str | None = Field(default=None, max_length=1000)
    lines: list[InvoiceLineIn] | None = None

    # These map to NOT-NULL columns. They're Optional only so the field can be
    # *omitted* (= no change); an explicit JSON null would reach setattr and
    # 500 (Decimal(None) in invoice_math, or an IntegrityError on commit).
    # Reject explicit null here as a 422 while keeping "omitted" = no change.
    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        # The full set of fields that map to nullable=False columns on the
        # Invoice ORM model (see models/company.py). Each is rejected if sent
        # as an explicit null; omitting the field still means "no change".
        if isinstance(data, dict):
            for field in (
                "direction",
                "contact_id",
                "invoice_number",
                "issue_date",
                "subtotal",
                "gst_amount",
                "total",
                "gst_inclusive",
                "status",
                "paid_amount",
            ):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data


class JournalEntrySummary(BaseModel):
    id: int
    entry_date: date
    memo: str
    source_type: str
    source_id: int | None
    reverses_entry_id: int | None


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    uploaded_at: datetime


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    direction: str
    contact_id: int
    contact_name: str | None = None
    invoice_number: str
    issue_date: date
    due_date: date | None
    currency: str
    subtotal: Money
    gst_amount: Money
    total: Money
    gst_inclusive: bool
    status: str
    paid_amount: Money
    paid_date: date | None
    authorised_at: datetime | None
    notes: str | None
    source: str
    source_ref: str | None
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentOut] = []
    journal_entries: list[JournalEntrySummary] = []


class PdfUploadResult(BaseModel):
    attachment_id: str
    filename: str
    size_bytes: int


class SpreadsheetPreviewRow(BaseModel):
    row_no: int
    cells: list[str]
    raw: list[Any]


class SpreadsheetPreview(BaseModel):
    headers: list[str]
    mapping: dict[str, int | None]
    rows: list[SpreadsheetPreviewRow]
    field_options: list[str]


class ExcelImportRow(BaseModel):
    row_no: int | None = None
    raw: list[Any] = Field(default_factory=list)
    direction_default: str | None = Field(default=None, pattern="^(AP|AR)$")


class ExcelImportPayload(BaseModel):
    mapping: dict[str, int | None]
    rows: list[ExcelImportRow] = Field(min_length=1, max_length=10000)
    direction_default: str = Field(default="AP", pattern="^(AP|AR)$")

    @model_validator(mode="after")
    def require_minimum_mapping(self) -> "ExcelImportPayload":
        required = ("contact_name", "invoice_number", "issue_date", "total")
        missing = [field for field in required if self.mapping.get(field) is None]
        if missing:
            raise ValueError(f"mapping missing required fields: {', '.join(missing)}")
        return self
