"""CRUD for the operator-editable staff list.

Per-company SQLite table. A staff member is selectable as the signer on a
Service Agreement and is printed under the company header on report PDFs.
Registration is MARA (MARN), legal practitioner (LPN), or none.
Soft-delete via active=False.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.company import StaffMember


_REG_TYPES = {"mara", "lpn", "none"}


class StaffServiceError(Exception):
    def __init__(self, detail: str, http_status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.http_status = http_status


def list_staff(db: Session, *, include_inactive: bool = False) -> list[StaffMember]:
    stmt = select(StaffMember).order_by(StaffMember.full_name.asc())
    if not include_inactive:
        stmt = stmt.where(StaffMember.active.is_(True))
    return list(db.execute(stmt).scalars().all())


def get_staff(db: Session, staff_id: int) -> StaffMember | None:
    return db.get(StaffMember, staff_id)


def _normalise(
    full_name: str, registration_type: str, registration_number: str | None
) -> tuple[str, str, str | None]:
    full_name = (full_name or "").strip()
    if not full_name:
        raise StaffServiceError("full_name is required", 422)
    registration_type = (registration_type or "none").strip().lower()
    if registration_type not in _REG_TYPES:
        raise StaffServiceError(
            "registration_type must be one of: mara, lpn, none", 422
        )
    # Strip ALL whitespace from the number (operators often type "12 34 567");
    # the stored/displayed form is space-free, e.g. "1234567".
    number = "".join((registration_number or "").split()) or None
    if registration_type != "none" and not number:
        raise StaffServiceError(
            f"registration_number is required for {registration_type.upper()}", 422
        )
    if registration_type == "none":
        number = None  # ignore any stray number when unregistered
    return full_name, registration_type, number


def create_staff(
    db: Session,
    *,
    full_name: str,
    registration_type: str = "none",
    registration_number: str | None = None,
) -> StaffMember:
    name, rtype, number = _normalise(full_name, registration_type, registration_number)
    row = StaffMember(
        full_name=name, registration_type=rtype, registration_number=number
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_staff(
    db: Session,
    staff_id: int,
    *,
    full_name: str,
    registration_type: str,
    registration_number: str | None,
    active: bool = True,
) -> StaffMember:
    row = get_staff(db, staff_id)
    if row is None:
        raise StaffServiceError("Staff member not found", 404)
    name, rtype, number = _normalise(full_name, registration_type, registration_number)
    row.full_name = name
    row.registration_type = rtype
    row.registration_number = number
    row.active = bool(active)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def soft_delete_staff(db: Session, staff_id: int) -> bool:
    row = get_staff(db, staff_id)
    if row is None:
        return False
    row.active = False
    db.add(row)
    db.commit()
    return True


def display_label(row: StaffMember) -> str:
    """Render "Name, MARN 1234567" / "Name, LPN 99999" / "Name"."""
    if row.registration_type == "mara" and row.registration_number:
        return f"{row.full_name}, MARN {row.registration_number}"
    if row.registration_type == "lpn" and row.registration_number:
        return f"{row.full_name}, LPN {row.registration_number}"
    return row.full_name


__all__ = [
    "StaffServiceError",
    "list_staff",
    "get_staff",
    "create_staff",
    "update_staff",
    "soft_delete_staff",
    "display_label",
]
