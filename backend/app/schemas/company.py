from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _strip_internal_spaces(v: str | None) -> str | None:
    """Normalise a registration number that may be typed with stray spaces,
    e.g. "12 34 567" → "1234567". Other characters are preserved."""
    if v is None:
        return None
    cleaned = "".join(v.split())
    return cleaned or None


class CompanyCreate(BaseModel):
    id: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    abn: str | None = Field(default=None, max_length=20)
    base_currency: str = Field(default="AUD", min_length=3, max_length=3)
    fy_start_month: int = Field(default=7, ge=1, le=12)
    gst_registered: bool = True
    default_payment_terms_days: int = Field(default=28, ge=0, le=365)
    @field_validator("abn", mode="before")
    @classmethod
    def _normalise_reg_number(cls, v):
        return _strip_internal_spaces(v) if isinstance(v, str) else v


class CompanyUpdate(BaseModel):
    """All fields optional — patch any subset of company profile / bank details."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    abn: str | None = Field(default=None, max_length=20)
    fy_start_month: int | None = Field(default=None, ge=1, le=12)
    gst_registered: bool | None = None
    bilingual_labels: bool | None = None

    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    suburb: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=20)
    postcode: str | None = Field(default=None, max_length=10)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=200)

    bank_account_name: str | None = Field(default=None, max_length=200)
    bank_name: str | None = Field(default=None, max_length=100)
    bank_bsb: str | None = Field(default=None, max_length=10)
    bank_account_number: str | None = Field(default=None, max_length=30)
    bank_swift: str | None = Field(default=None, max_length=20)
    operating_bank_account_name: str | None = Field(default=None, max_length=200)
    operating_bank_name: str | None = Field(default=None, max_length=100)
    operating_bank_bsb: str | None = Field(default=None, max_length=10)
    operating_bank_account_number: str | None = Field(default=None, max_length=30)
    operating_bank_swift: str | None = Field(default=None, max_length=20)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)

    acn: str | None = Field(default=None, max_length=20)

    @field_validator("acn", mode="before")
    @classmethod
    def _normalise_reg_number(cls, v):
        return _strip_internal_spaces(v) if isinstance(v, str) else v


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    legal_name: str | None
    abn: str | None
    country: str
    base_currency: str
    fy_start_month: int
    gst_registered: bool
    bilingual_labels: bool = False

    address_line1: str | None = None
    address_line2: str | None = None
    suburb: str | None = None
    state: str | None = None
    postcode: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None

    bank_account_name: str | None = None
    bank_name: str | None = None
    bank_bsb: str | None = None
    bank_account_number: str | None = None
    bank_swift: str | None = None
    operating_bank_account_name: str | None = None
    operating_bank_name: str | None = None
    operating_bank_bsb: str | None = None
    operating_bank_account_number: str | None = None
    operating_bank_swift: str | None = None
    default_payment_terms_days: int = 28

    acn: str | None = None

    created_at: datetime


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    type: str
    parent_id: int | None
    is_gst: bool
    active: bool
    description: str | None


_ACCOUNT_TYPES = {"ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE", "COST_OF_SALES"}


class AccountCreate(BaseModel):
    code: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(pattern=r"^(ASSET|LIABILITY|EQUITY|INCOME|EXPENSE|COST_OF_SALES)$")
    parent_id: int | None = None
    is_gst: bool = False
    description: str | None = Field(default=None, max_length=500)


class AccountUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=20, pattern=r"^[A-Za-z0-9._-]+$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = Field(default=None, pattern=r"^(ASSET|LIABILITY|EQUITY|INCOME|EXPENSE|COST_OF_SALES)$")
    parent_id: int | None = None
    set_parent_null: bool = False
    is_gst: bool | None = None
    active: bool | None = None
    description: str | None = Field(default=None, max_length=500)
