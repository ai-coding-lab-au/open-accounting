"""Unit tests for the deterministic PDF bank-statement parser.

Synthetic statement PDFs are generated with reportlab (already a dependency) so
the tests don't need real bank files. Real-sample tuning of the per-bank parsers
is done separately as samples arrive.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.bank_pdf import parse_pdf, detect_bank, PdfStatementError  # noqa: E402
from app.services.bank_pdf.extract import extract_text  # noqa: E402


def _make_pdf(lines: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Courier", 10)
    y = 800
    for ln in lines:
        c.drawString(40, y, ln)
        y -= 16
    c.save()
    return buf.getvalue()


CBA_STATEMENT = [
    "Commonwealth Bank of Australia - Transaction listing",
    "Date        Description                 Amount      Balance",
    "01/07/2026  Opening balance                         1,000.00",
    "02/07/2026  Salary ACME PTY LTD         5,000.00    6,000.00",
    "03/07/2026  Rent payment                1,200.00    4,800.00",
    "05/07/2026  Coffee shop                    4.50     4,795.50",
]


def test_generic_signs_amounts_by_balance_movement():
    _, rows = parse_pdf(_make_pdf(CBA_STATEMENT), "auto")
    # (date, description, counter-party, signed amount)
    by_desc = {r[1]: r[3] for r in rows}
    assert by_desc["Salary ACME PTY LTD"] == "5000.00"      # balance up -> in
    assert by_desc["Rent payment"] == "-1200.00"            # balance down -> out
    assert by_desc["Coffee shop"] == "-4.50"


def test_headers_match_the_import_mapping():
    headers, _ = parse_pdf(_make_pdf(CBA_STATEMENT), None)
    assert headers == ["Date", "Description", "Counter-party", "Amount"]


def test_money_regex_accepts_ungrouped_amounts():
    """Some statements omit the thousands separator (1000.00, not 1,000.00);
    the money patterns must still match those."""
    from app.services.bank_pdf.parsers import _MONEY_RE, _MONEY_WORD

    for tok in ("1000.00", "12345.67", "999.99", "5,000.00", "1,234,567.89"):
        assert _MONEY_WORD.match(tok), tok
    m = _MONEY_RE.search("Rent 1000.00 4800.00")
    assert m and m.group(0) == "1000.00"
    # a bare integer (no cents) must NOT be treated as money
    assert not _MONEY_WORD.match("1000")
    # an amount must not be pulled out of a longer alphanumeric run...
    assert _MONEY_RE.search("REF1000.00") is None
    # ...and a 3-decimal value must not be truncated to 2 and matched
    assert _MONEY_RE.search("rate 4.500") is None
    # but a normal amount adjacent to a percent sign still reads (up to .dd)
    assert _MONEY_RE.search("fee 12.34 charged") is not None


UNGROUPED_STATEMENT = [
    "Commonwealth Bank of Australia - Transaction listing",
    "Date        Description                 Amount      Balance",
    "01/07/2026  Opening balance                         1000.00",
    "02/07/2026  Salary ACME PTY LTD         5000.00     6000.00",
    "03/07/2026  Rent payment                1200.00     4800.00",
]


def test_generic_parses_ungrouped_amounts_end_to_end():
    _, rows = parse_pdf(_make_pdf(UNGROUPED_STATEMENT), "auto")
    by_desc = {r[1]: r[3] for r in rows}
    assert by_desc["Salary ACME PTY LTD"] == "5000.00"
    assert by_desc["Rent payment"] == "-1200.00"


def test_autodetect_recognises_bank_brand():
    assert detect_bank(extract_text(_make_pdf(CBA_STATEMENT))) == "cba"
    assert detect_bank("Westpac Banking Corporation") == "westpac"
    assert detect_bank("some unbranded statement") is None


def test_explicit_bank_format_parses():
    for bank in ("cba", "nab", "anz", "westpac"):
        _, rows = parse_pdf(_make_pdf(CBA_STATEMENT), bank)
        assert len(rows) == 4, bank


def test_unknown_bank_falls_back_to_generic():
    _, rows = parse_pdf(_make_pdf(CBA_STATEMENT), "notabank")
    assert len(rows) == 4


def _make_cba_pdf() -> bytes:
    """Reconstruct the Commonwealth Bank layout: Date | Transaction | Debit |
    Credit | Balance, multi-line entries with Card / Value Date sub-rows and
    'DD Mon' dates without a year."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(650, 900))
    # Page 1: summary header with the statement period (spans a year boundary).
    c.setFont("Helvetica", 11)
    c.drawString(60, 800, "Your Statement")
    c.drawString(60, 770, "Statement 15  (Page 1 of 39)")
    c.drawString(60, 740, "Statement Period  31 Dec 2025 - 30 Jun 2026")
    c.showPage()
    # Page 2: the transaction table.
    c.setFont("Helvetica", 9)
    DATE, TXN, DEBIT, CREDIT, BAL = 40, 110, 360, 450, 520
    for t, x in [("Date", DATE), ("Transaction", TXN), ("Debit", DEBIT), ("Credit", CREDIT), ("Balance", BAL)]:
        c.drawString(x, 860, t)
    y = [840]

    def row(dt, desc, debit=None, credit=None, bal=None, card=None, vdate=None):
        c.drawString(DATE, y[0], dt)
        c.drawString(TXN, y[0], desc)
        if not card and not vdate:
            if debit:
                c.drawString(DEBIT, y[0], debit)
            if credit:
                c.drawString(CREDIT, y[0], credit)
            if bal:
                c.drawString(BAL, y[0], bal)
        y[0] -= 14
        if card:
            c.drawString(TXN, y[0], card)
            y[0] -= 14
        if vdate:
            c.drawString(TXN, y[0], "Value Date: " + vdate)
            if debit:
                c.drawString(DEBIT, y[0], debit)
            if credit:
                c.drawString(CREDIT, y[0], credit)
            if bal:
                c.drawString(BAL, y[0], bal)
            y[0] -= 14
        y[0] -= 6

    row("31 Dec", "OLD YEAR PURCHASE", debit="1.00", bal="$151.27 CR")
    row("01 Jan", "EXAMPLE PHARMACY SPRINGFIELD NS AUS", debit="8.90", bal="$642.10 CR", card="Card xx1111", vdate="29/12/2025")
    row("01 Jan", "Debit Excess Interest", debit="0.04", bal="$642.06 CR")
    row("01 Jan", "Transfer from xx2222 CommBank app", credit="500.00", bal="$1142.06 CR")
    row("02 Jan", "SAMPLE TOLL ROAD OPERATOR AU", debit="24.50", bal="$1117.56 CR")
    row("15 Jun", "J CITIZEN TRANSFER springfield NS AUS", debit="30.00", bal="$1087.56 CR", card="Card xx1111", vdate="14/06/2026")
    c.save()
    return buf.getvalue()


def test_cba_multiline_layout_extracts_desc_direction_and_year():
    from app.services.bank_pdf.parsers import parse_cba

    rows = parse_cba(_make_cba_pdf())
    by_desc = {r["description"]: r for r in rows}
    # Full merchant description (not truncated at the column edge).
    assert "EXAMPLE PHARMACY SPRINGFIELD NS AUS" in by_desc
    assert "SAMPLE TOLL ROAD OPERATOR AU" in by_desc
    # Direction from the Debit/Credit column.
    assert by_desc["EXAMPLE PHARMACY SPRINGFIELD NS AUS"]["amount"] == "-8.90"
    assert by_desc["Transfer from xx2222 CommBank app"]["amount"] == "500.00"
    # 'DD Mon' dates get their year from the statement period (31 Dec 2025 -
    # 30 Jun 2026), which spans a year boundary.
    assert by_desc["OLD YEAR PURCHASE"]["date"] == "2025-12-31"
    assert by_desc["EXAMPLE PHARMACY SPRINGFIELD NS AUS"]["date"] == "2026-01-01"
    assert by_desc["Debit Excess Interest"]["date"] == "2026-01-01"
    assert by_desc["J CITIZEN TRANSFER springfield NS AUS"]["date"] == "2026-06-15"


def test_scanned_or_empty_pdf_raises_clear_error():
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    canvas.Canvas(buf).save()  # a blank page, no text
    with pytest.raises(PdfStatementError) as exc:
        parse_pdf(buf.getvalue(), None)
    assert "no extractable text" in str(exc.value).lower()


def test_signed_amount_flows_to_direction_shape():
    # A signed Amount column must materialise into in/out via the existing
    # _row_to_txn_shape (positive = in, negative = out).
    from app.services.bank_import import _row_to_txn_shape, propose_mapping

    headers, rows = parse_pdf(_make_pdf(CBA_STATEMENT), "auto")
    mapping = propose_mapping(headers)
    salary = next(r for r in rows if r[1] == "Salary ACME PTY LTD")
    rent = next(r for r in rows if r[1] == "Rent payment")
    assert _row_to_txn_shape(salary, mapping)["direction"] == "in"
    assert _row_to_txn_shape(rent, mapping)["direction"] == "out"
