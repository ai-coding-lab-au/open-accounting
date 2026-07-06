"""Regression test: report PDFs must XML-escape user-controlled strings.

reportlab's Paragraph parses XML-ish markup, so unescaped `<`, `>`, `&` in
a client name (or any user-provided text) would crash the rendering. Each
renderer wraps user fields with the `_esc()` helper — these tests prove
that a hostile-looking display_name doesn't blow up the PDF build.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.report_render import (  # noqa: E402
    render_bank_statement_pdf,
    render_pnl_pdf,
)


COMPANY = SimpleNamespace(
    name="Example Visa & Co <Pty> Ltd",  # name itself has XML-hostile chars
    abn="12 345 678 901",
    acn=None,
    address_line1="Suite 1",
    address_line2=None,
    suburb="Sydney",
    state="NSW",
    postcode="2000",
    phone="0400 000 000",
    email="hello@example.com",
)


def _bank_statement_data() -> dict:
    return {
        "bank_account_name": "Business <Main> & Co",
        "period_start": date(2026, 5, 1),
        "period_end": date(2026, 5, 31),
        "opening_balance": Decimal("0.00"),
        "closing_balance": Decimal("100.00"),
        "total_in": Decimal("100.00"),
        "total_out": Decimal("0.00"),
        "rows": [
            {
                "occurred_at": date(2026, 5, 10),
                "direction": "in",
                "amount": Decimal("100.00"),
                "memo": "Payment <from> A & B",
                "counter_party_name": "A & B Pty Ltd",
                "account_code": "4000",
                "account_name": "Sales > <Services>",
                "running_balance": Decimal("100.00"),
            }
        ],
    }


def test_bank_statement_pdf_escapes_memo():
    pdf = render_bank_statement_pdf(company=COMPANY, data=_bank_statement_data())
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000


def test_report_pdf_uses_times_font():
    """Reports are English-only and render in Times (the serif used across all
    PDF output). The Times font appears in the PDF font dictionary."""
    from app.services.report_render import FONT_BASE

    assert FONT_BASE == "Times-Roman"

    pdf = render_bank_statement_pdf(company=COMPANY, data=_bank_statement_data())
    assert pdf.startswith(b"%PDF-")
    assert b"Times" in pdf


def test_pnl_pdf_escapes_account_names():
    data = {
        "period_start": date(2025, 7, 1),
        "period_end": date(2026, 6, 30),
        "income_rows": [
            {"code": "4000", "name": "Sales & <Services>", "total": Decimal("1000.00")}
        ],
        "total_income": Decimal("1000.00"),
        "cogs_rows": [],
        "total_cogs": Decimal("0.00"),
        "gross_profit": Decimal("1000.00"),
        "expense_rows": [
            {"code": "5000", "name": "Cost > <Sales>", "total": Decimal("400.00")}
        ],
        "total_expense": Decimal("400.00"),
        "net_profit": Decimal("600.00"),
        "uncategorised_in": Decimal("0.00"),
        "uncategorised_out": Decimal("0.00"),
    }
    pdf = render_pnl_pdf(company=COMPANY, data=data)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000
