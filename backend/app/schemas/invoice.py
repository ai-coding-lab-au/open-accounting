from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._currency import normalise_aud
from ._dates import check_reportable_date
from ._limits import SQLITE_EXACT_MONEY_MAX, SQLITE_INT_MAX
from ._money import Money


# Keep writes inside SQLite's exact-cent subset of NUMERIC(16, 2).
_MONEY_MAX = SQLITE_EXACT_MONEY_MAX


class InvoiceLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=500)
    account_id: int | None = Field(default=None, ge=1, le=SQLITE_INT_MAX)
    quantity: Decimal = Field(
        default=Decimal("1"), ge=0, le=Decimal("999999.9999"), max_digits=12, decimal_places=4
    )
    unit_price: Decimal = Field(
        default=Decimal("0"), ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    gst_rate: Decimal = Field(
        default=Decimal("0.10"), ge=0, le=1, max_digits=5, decimal_places=4
    )
    line_subtotal: Decimal = Field(ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2)
    line_gst: Decimal = Field(
        default=Decimal("0"), ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    line_total: Decimal = Field(ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2)
    tax_code: str | None = Field(
        default=None,
        pattern=r"^(standard|gst_free|input_taxed|capital|none)$",
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_legacy_tax_code(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("tax_code") is not None:
            return data
        values = dict(data)
        try:
            line_gst = Decimal(str(values.get("line_gst", 0)))
        except Exception:
            return data
        values["tax_code"] = "standard" if line_gst > 0 else "gst_free"
        return values

    @model_validator(mode="after")
    def _tax_code_matches_gst(self):
        if self.tax_code in {"gst_free", "input_taxed", "none"} and self.line_gst > 0:
            raise ValueError(
                f"tax_code={self.tax_code} requires line_gst to be zero"
            )
        return self


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
    contact_id: int | None = Field(default=None, ge=1, le=SQLITE_INT_MAX)
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
    subtotal: Decimal = Field(ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2)
    gst_amount: Decimal = Field(
        default=Decimal("0"), ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    total: Decimal = Field(ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2)
    gst_inclusive: bool = True
    notes: str | None = Field(default=None, max_length=1000)
    source: str = Field(default="manual", pattern="^(manual|pdf|excel)$")
    source_ref: str | None = None
    status: str | None = Field(default=None, pattern="^(draft|authorised|unpaid|partial|paid|void)$")
    attachment_id: str | None = None     # link an existing uploaded file to this invoice
    lines: list[InvoiceLineIn] | None = None

    @field_validator("issue_date")
    @classmethod
    def _reportable_issue_date(cls, v: date) -> date:
        return check_reportable_date(v, field_name="issue_date")

    @field_validator("currency", mode="before")
    @classmethod
    def _aud_only(cls, v):
        return normalise_aud(v)


class InvoiceUpdate(BaseModel):
    direction: str | None = Field(default=None, pattern="^(AP|AR)$")
    issue_date: date | None = None
    due_date: date | None = None
    contact_id: int | None = Field(default=None, ge=1, le=SQLITE_INT_MAX)
    invoice_number: str | None = Field(default=None, min_length=1, max_length=80)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = Field(
        default=None, ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    gst_amount: Decimal | None = Field(
        default=None, ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    total: Decimal | None = Field(
        default=None, ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    gst_inclusive: bool | None = None
    status: str | None = Field(default=None, pattern="^(draft|authorised|unpaid|partial|paid|void)$")
    paid_amount: Decimal | None = Field(
        default=None, ge=0, le=_MONEY_MAX, max_digits=16, decimal_places=2
    )
    paid_date: date | None = None
    notes: str | None = Field(default=None, max_length=1000)
    lines: list[InvoiceLineIn] | None = None

    @field_validator("issue_date")
    @classmethod
    def _reportable_issue_date(cls, v: date | None) -> date | None:
        if v is None:
            return None
        return check_reportable_date(v, field_name="issue_date")

    @field_validator("currency", mode="before")
    @classmethod
    def _aud_only(cls, v):
        if v is None:
            raise ValueError("currency cannot be null")
        return normalise_aud(v)

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
                "currency",
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
