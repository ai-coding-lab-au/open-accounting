from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ContactCreate(BaseModel):
    kind: str = Field(default="supplier", pattern="^(customer|supplier|both)$")
    name: str = Field(min_length=1, max_length=200)
    abn: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)
    active: bool = True


class ContactUpdate(BaseModel):
    kind: str | None = Field(default=None, pattern="^(customer|supplier|both)$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    abn: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)
    active: bool | None = None


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
