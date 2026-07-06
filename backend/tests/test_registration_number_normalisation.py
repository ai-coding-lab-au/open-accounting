"""Registration numbers may be typed with stray spaces (operators copy them
from emails/PDFs). Staff rows store them space-free, e.g.
"12 34 567" -> "1234567". Company ACN keeps the same normalisation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_company_update_strips_spaces_in_acn():
    from app.schemas.company import CompanyUpdate

    u = CompanyUpdate(acn="0 11 222 333")
    assert u.acn == "011222333"
    assert CompanyUpdate(acn="   ").acn is None


def test_staff_normalise_strips_internal_spaces():
    from app.services.staff import _normalise

    name, rtype, number = _normalise("Jane Doe", "mara", "12 34 567")
    assert (name, rtype, number) == ("Jane Doe", "mara", "1234567")
    # LPN path too.
    assert _normalise("Li Wei", "lpn", "9 99 99")[2] == "99999"
