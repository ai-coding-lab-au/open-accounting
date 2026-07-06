"""Smoke test for the outgoing-document PDF renderer.

Writes a sample Receipt PDF into tmp/ so the result can be eyeballed. Does not
assert on visual correctness — only that the bytes are produced and look like a
valid PDF.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.pdf_render import render_document_pdf  # noqa: E402


PROJECT_ROOT = ROOT.parent
OUTPUT_DIR = PROJECT_ROOT / "tmp" / "pdf_samples"


COMPANY = {
    "name": "Example Services Pty Ltd",
    "address_line1": "100 Example Street",
    "suburb": "Sydney",
    "state": "NSW",
    "postcode": "2000",
    "phone": "0400000000",
    "email": "accounts@example.test",
    "abn": "12 345 678 901",
    "bank_account_name": "EXAMPLE SERVICES PTY LTD",
    "bank_name": "EXAMPLE BANK",
    "bank_bsb": "000000",
    "bank_account_number": "00000000",
    "bank_swift": "EXAMPXX0",
}

CUSTOMER = {
    "name": "Abc DEF",
    "address": "Unit 1, 1 Example Street\nSpringfield, NSW 2000",
    "email": "customer@example.com",
    "phone": "0400000000",
}

LINES = [
    {
        "description": "Citizenship Application",
        "quantity": Decimal("1"),
        "unit_price": Decimal("1000.00"),
        "amount": Decimal("1000.00"),
    },
]


def _render_one(doc_type: str, doc_number: str, **kwargs) -> bytes:
    return render_document_pdf(
        doc_type=doc_type,
        doc_number=doc_number,
        issue_date=date(2026, 5, 18),
        expiration_date=date(2026, 5, 25) if doc_type != "receipt" else None,
        company=COMPANY,
        customer=CUSTOMER,
        lines=LINES,
        subtotal=Decimal("1000.00"),
        gst_amount=Decimal("0.00"),
        total=Decimal("1000.00"),
        is_gst_registered=False,
        **kwargs,
    )


def test_renders_receipt():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf = _render_one(
        "receipt",
        "RCT-2026-0001-1",
        paid_date=date(2026, 5, 17),
        payment_method="Bank transfer",
    )
    assert pdf.startswith(b"%PDF-")
    (OUTPUT_DIR / "sample_receipt.pdf").write_bytes(pdf)
