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


def test_renders_cjk_text_with_embedded_font():
    """Chinese customer/company/line text must render with real glyphs, not
    the Times placeholder boxes (audit: '咨询服务' used to come out as 'nnnn').
    Extraction via pdfplumber proves the CJK face was embedded with a working
    ToUnicode map."""
    import io

    import pdfplumber

    pdf = render_document_pdf(
        doc_type="receipt",
        doc_number="RCT-2026-0002-1",
        issue_date=date(2026, 5, 18),
        company={**COMPANY, "name": "华人会计 Example Pty Ltd"},
        customer={
            "name": "张伟",
            "address": "Unit 1, 1 Example Street\nSpringfield, NSW 2000",
            "email": "customer@example.com",
            "phone": "0400000000",
        },
        lines=[
            {
                "description": "咨询服务 Consulting",
                "quantity": Decimal("1"),
                "unit_price": Decimal("100.00"),
                "amount": Decimal("100.00"),
            }
        ],
        subtotal=Decimal("100.00"),
        gst_amount=Decimal("10.00"),
        total=Decimal("110.00"),
        is_gst_registered=True,
        paid_date=date(2026, 5, 17),
        payment_method="银行转账 Bank transfer",
    )
    assert pdf.startswith(b"%PDF-")
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = doc.pages[0].extract_text() or ""
    assert "张伟" in text
    assert "咨询服务 Consulting" in text
    assert "华人会计 Example Pty Ltd" in text
    assert "银行转账 Bank transfer" in text
    # English content still renders alongside.
    assert "TOTAL (INCL. GST)" in text


def test_bilingual_labels_render_when_enabled():
    """Company.bilingual_labels=True turns fixed labels into "ENGLISH 中文";
    off (the default, exercised by every other test) keeps them English-only."""
    import io

    import pdfplumber

    pdf = render_document_pdf(
        doc_type="receipt",
        doc_number="RCT-2026-0003-1",
        issue_date=date(2026, 5, 18),
        company={**COMPANY, "bilingual_labels": True},
        customer=CUSTOMER,
        lines=LINES,
        subtotal=Decimal("1000.00"),
        gst_amount=Decimal("100.00"),
        total=Decimal("1100.00"),
        is_gst_registered=True,
        paid_date=date(2026, 5, 17),
        payment_method="Bank transfer",
    )
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = doc.pages[0].extract_text() or ""
    assert "收据" in text  # RECEIPT 收据
    assert "客户" in text  # BILL TO 客户
    assert "总计（含GST）" in text  # TOTAL (INCL. GST)
    assert "付款方式" in text  # PAYMENT METHOD
    assert "RECEIPT" in text  # English retained alongside

    # Default (no flag) stays English-only.
    pdf_default = _render_one(
        "receipt", "RCT-2026-0004-1", paid_date=date(2026, 5, 17)
    )
    with pdfplumber.open(io.BytesIO(pdf_default)) as doc:
        text_default = doc.pages[0].extract_text() or ""
    assert "收据" not in text_default


def test_bilingual_labels_in_html_template():
    from app.services.html_render import build_html

    common = dict(
        doc_type="receipt",
        doc_number="RCT-2026-0005-1",
        issue_date=date(2026, 5, 18),
        customer=CUSTOMER,
        lines=LINES,
        subtotal=Decimal("1000.00"),
        gst_amount=Decimal("100.00"),
        total=Decimal("1100.00"),
        is_gst_registered=True,
        paid_date=date(2026, 5, 17),
        payment_method="Bank transfer",
    )
    html_on = build_html(company={**COMPANY, "bilingual_labels": True}, **common)
    assert "RECEIPT 收据" in html_on
    assert "BILL TO 客户" in html_on
    assert "TOTAL 总计" in html_on
    assert "PAYMENT METHOD 付款方式" in html_on

    html_off = build_html(company=COMPANY, **common)
    assert "收据" not in html_off
