"""Server-side accounting period lock invariants.

The lock is a monotonically advancing company policy.  UI warnings are useful,
but every dated mutation is checked here (or immediately before its service
call) so direct API clients cannot rewrite a closed reporting period.
"""

from __future__ import annotations

from datetime import date

from ..models.master import Company


class AccountingPeriodLockedError(ValueError):
    """Raised when a mutation would change a closed accounting period."""


def require_open_date(
    company: Company,
    value: date,
    *,
    operation: str,
) -> None:
    locked_through = company.books_locked_through
    if locked_through is None or value > locked_through:
        return
    raise AccountingPeriodLockedError(
        f"Cannot {operation} on {value.isoformat()}: accounting periods through "
        f"{locked_through.isoformat()} are locked. Record a dated adjustment in "
        "an open period instead."
    )
