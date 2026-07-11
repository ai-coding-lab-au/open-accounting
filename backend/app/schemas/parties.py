"""Pydantic schemas for clients and business partners."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _strip_phone_spaces(v: str | None) -> str | None:
    """Remove stray whitespace from a phone number typed with spaces,
    e.g. "0400 000 000" → "0400000000". Other characters (+, -, ()) are
    preserved. Empty result collapses to None."""
    if v is None:
        return None
    cleaned = "".join(v.split())
    return cleaned or None


def _strip_required_name(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    cleaned = v.strip()
    if not cleaned:
        raise ValueError("display_name must not be blank")
    return cleaned


def _strip_optional_ref(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    return v.strip() or None


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class ClientCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    client_ref: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("display_name", mode="before")
    @classmethod
    def _normalise_display_name(cls, v: Any) -> Any:
        return _strip_required_name(v)

    @field_validator("client_ref", mode="before")
    @classmethod
    def _normalise_client_ref(cls, v: Any) -> Any:
        return _strip_optional_ref(v)

    @field_validator("phone", mode="before")
    @classmethod
    def _normalise_phone(cls, v):
        return _strip_phone_spaces(v) if isinstance(v, str) else v


class ClientUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    client_ref: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None

    @field_validator("display_name", mode="before")
    @classmethod
    def _normalise_display_name(cls, v: Any) -> Any:
        return _strip_required_name(v)

    @field_validator("client_ref", mode="before")
    @classmethod
    def _normalise_client_ref(cls, v: Any) -> Any:
        return _strip_optional_ref(v)

    @field_validator("phone", mode="before")
    @classmethod
    def _normalise_phone(cls, v):
        return _strip_phone_spaces(v) if isinstance(v, str) else v

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        # display_name and is_active map to nullable=False columns on the Client
        # ORM model (see models/company.py). They're Optional only so the field
        # can be *omitted* (= no change); an explicit JSON null would reach
        # setattr and 500 on commit. Reject explicit null here as a 422.
        if isinstance(data, dict):
            for field in ("display_name", "is_active"):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str
    email: str | None
    phone: str | None
    address: str | None
    client_ref: str | None
    notes: str | None
    is_active: bool
    created_at: datetime
