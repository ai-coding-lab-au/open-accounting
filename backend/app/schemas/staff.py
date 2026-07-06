"""Schemas for the firm staff list (M4)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RegistrationType = Literal["mara", "lpn", "none"]


class StaffUpsert(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    registration_type: RegistrationType = "none"
    registration_number: str | None = Field(default=None, max_length=40)
    active: bool = True


class StaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    registration_type: str
    registration_number: str | None
    active: bool
    # Convenience label for the FN dropdown, e.g. "Jane Doe, MARN 1234567".
    display_label: str = ""
