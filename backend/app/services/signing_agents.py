"""Helpers for document signing-agent selection.

Service Agreements store an explicit staff_member_id. Documents without an
interactive selector use the first active registered staff member for the
company DB. A registered signer is a MARA agent (MARN) or a legal
practitioner (LPN) — both are accepted; only `registration_type == "none"`
staff cannot sign.

Function names keep the historical `mara` spelling for call-site stability,
but the selection now accepts MARA or LPN.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.company import StaffMember
from .staff import display_label

# A staff member can sign documents when they carry a registration — MARA or
# LPN. ("none" staff exist for non-signing roles and are excluded.)
_SIGNER_TYPES = ("mara", "lpn")


def first_active_mara(db: Session) -> StaffMember | None:
    return db.execute(
        select(StaffMember)
        .where(
            StaffMember.active.is_(True),
            StaffMember.registration_type.in_(_SIGNER_TYPES),
        )
        .order_by(StaffMember.full_name.asc(), StaffMember.id.asc())
    ).scalars().first()


def active_mara_by_id(db: Session, staff_id: int) -> StaffMember | None:
    row = db.get(StaffMember, staff_id)
    if (
        row is None
        or not row.active
        or row.registration_type not in _SIGNER_TYPES
        or not row.registration_number
    ):
        return None
    return row


def staff_payload(row: StaffMember | None) -> dict:
    if row is None:
        return {
            "registered_agent_name": "",
            "marn": "",
            "registered_legal_practitioner_name": "",
            "lpn": "",
            "signing_agent_name": "",
            "signing_agent_registration_type": "",
            "signing_agent_registration_number": "",
            "signing_agent_label": "",
        }
    if row.registration_type == "mara":
        return {
            "registered_agent_name": row.full_name,
            "marn": row.registration_number or "",
            "registered_legal_practitioner_name": "",
            "lpn": "",
            "signing_agent_name": row.full_name,
            "signing_agent_registration_type": "mara",
            "signing_agent_registration_number": row.registration_number or "",
            "signing_agent_label": display_label(row),
        }
    if row.registration_type == "lpn":
        return {
            "registered_agent_name": "",
            "marn": "",
            "registered_legal_practitioner_name": row.full_name,
            "lpn": row.registration_number or "",
            "signing_agent_name": row.full_name,
            "signing_agent_registration_type": "lpn",
            "signing_agent_registration_number": row.registration_number or "",
            "signing_agent_label": display_label(row),
        }
    return {
        "registered_agent_name": row.full_name,
        "marn": "",
        "registered_legal_practitioner_name": "",
        "lpn": "",
        "signing_agent_name": row.full_name,
        "signing_agent_registration_type": "none",
        "signing_agent_registration_number": "",
        "signing_agent_label": display_label(row),
    }


__all__ = ["active_mara_by_id", "display_label", "first_active_mara", "staff_payload"]
