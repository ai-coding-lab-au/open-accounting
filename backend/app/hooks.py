"""Cross-module hook registry.

When a module (M2 Documents, future M-Trust, etc.) needs to register a
validation that runs from a core M1 endpoint, it registers a callable
here. The core endpoint iterates the relevant registry without
importing the module's models directly.

This is what lets `M1 contacts.delete` refuse to delete a contact that's
still referenced by an M2 OutgoingDocument without M1 having to import
OutgoingDocument. In M1 standalone (no M2 loaded), the registry is
empty and the delete proceeds.

Registration happens at import time. Modules that ship their hooks
should be imported during app startup (see backend/app/main.py).
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session


# A "contact reference check" runs before deleting a Contact.
# Returns a human-readable reason if the contact is still referenced,
# or None if the caller is free to delete. Callers raise HTTPException
# on the first non-None result.
ContactReferenceCheck = Callable[[Session, int], str | None]

_contact_reference_checks: list[ContactReferenceCheck] = []


def register_contact_reference_check(check: ContactReferenceCheck) -> None:
    """Register a function that vetoes Contact deletion when the contact
    is referenced from this module's tables.

    The check signature is `(db, contact_id) -> reason str | None`.
    Return a short user-facing string when the delete should be
    refused; return None to allow.
    """
    _contact_reference_checks.append(check)


def iter_contact_reference_checks() -> list[ContactReferenceCheck]:
    return list(_contact_reference_checks)
