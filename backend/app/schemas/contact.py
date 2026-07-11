from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _strip_contact_name(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name must not be blank")
    return cleaned


def _strip_internal_spaces(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = "".join(value.split())
    return cleaned or None


class ContactCreate(BaseModel):
    kind: str = Field(default="supplier", pattern="^(customer|supplier|both)$")
    name: str = Field(min_length=1, max_length=200)
    abn: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)
    active: bool = True

    @field_validator("name", mode="before")
    @classmethod
    def _normalise_name(cls, value: Any) -> Any:
        return _strip_contact_name(value)

    @field_validator("abn", "phone", mode="before")
    @classmethod
    def _normalise_compact_fields(cls, value: Any) -> Any:
        return _strip_internal_spaces(value)


class ContactUpdate(BaseModel):
    kind: str | None = Field(default=None, pattern="^(customer|supplier|both)$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    abn: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)
    active: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _normalise_name(cls, value: Any) -> Any:
        return _strip_contact_name(value)

    @field_validator("abn", "phone", mode="before")
    @classmethod
    def _normalise_compact_fields(cls, value: Any) -> Any:
        return _strip_internal_spaces(value)

    @model_validator(mode="before")
    @classmethod
    def _reject_required_null(cls, data: Any) -> Any:
        # These fields are optional only so PATCH callers may omit them. An
        # explicit JSON null cannot be persisted to the non-nullable columns.
        if isinstance(data, dict):
            for field in ("kind", "name", "active"):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    name: str
    abn: str | None
    email: str | None
    phone: str | None
    address: str | None
    notes: str | None
    active: bool
    created_at: datetime
