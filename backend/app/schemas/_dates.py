"""Shared date bounds for transactions and reportable accounting documents."""

from __future__ import annotations

from datetime import date


# BAS fy_year is limited to 2000..2100. FY2000 starts in July 1999 and FY2100
# ends in June 2100, so activity outside this window cannot be included in any
# supported BAS period.
MIN_REPORTABLE_DATE = date(1999, 7, 1)
MAX_REPORTABLE_DATE = date(2100, 6, 30)


def current_date() -> date:
    """Clock seam for surfaces whose contract is explicitly "as of today"."""

    return date.today()


def check_reportable_date(value: date, *, field_name: str) -> date:
    if value < MIN_REPORTABLE_DATE or value > MAX_REPORTABLE_DATE:
        raise ValueError(
            f"{field_name} must be between {MIN_REPORTABLE_DATE.isoformat()} and "
            f"{MAX_REPORTABLE_DATE.isoformat()} (a reportable BAS financial year)"
        )
    return value
