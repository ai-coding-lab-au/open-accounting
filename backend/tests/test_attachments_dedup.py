"""Audit P1 #1: re-uploading the same orphan PDF must not create a
second orphan Attachment row.

The dedup rule lives in `services.attachments.save_bytes`:
  - Same sha256, both `existing.invoice_id is None` and `invoice_id is None`
    → reuse the existing row (no second orphan).
  - If either side is linked to an invoice, a new row IS created
    (callers may legitimately want one PDF to back two invoices).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECT_ROOT = ROOT.parent


@pytest.fixture()
def company(monkeypatch, request):
    """Spin up an isolated company DB and yield (company_id, session)."""
    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        import shutil

        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]

    from app.db.company import company_session, init_company_db  # noqa: WPS433

    company_id = "tc"
    init_company_db(company_id)
    with company_session(company_id) as session:
        yield company_id, session


def test_reupload_same_orphan_pdf_reuses_attachment(company):
    """Same bytes uploaded twice as orphans → single Attachment row."""
    from app.models.company import Attachment
    from app.services import attachments as attach_svc

    company_id, session = company
    content = b"%PDF-1.4 fake bytes for dedup test"

    att1 = attach_svc.save_bytes(
        db=session,
        company_id=company_id,
        content=content,
        original_filename="bill.pdf",
        mime_type="application/pdf",
    )
    session.commit()

    att2 = attach_svc.save_bytes(
        db=session,
        company_id=company_id,
        content=content,
        original_filename="bill.pdf",
        mime_type="application/pdf",
    )
    session.commit()

    assert att1.id == att2.id, "second orphan upload should reuse the first row"
    rows = session.query(Attachment).filter(Attachment.sha256 == att1.sha256).all()
    assert len(rows) == 1


def test_reupload_when_existing_is_linked_creates_new_row(company):
    """If the existing row is linked to an invoice, a re-upload must
    create a fresh orphan — the user might be backing a different invoice."""
    from datetime import date
    from decimal import Decimal

    from app.models.company import Attachment, Contact, Invoice
    from app.services import attachments as attach_svc

    company_id, session = company

    # Minimal contact + invoice so the FK on Attachment.invoice_id resolves.
    contact = Contact(name="Acme Co", kind="supplier")
    session.add(contact)
    session.flush()
    invoice = Invoice(
        direction="AP",
        contact_id=contact.id,
        invoice_number="INV-LINK",
        issue_date=date(2026, 5, 1),
        subtotal=Decimal("100.00"),
        gst_amount=Decimal("10.00"),
        total=Decimal("110.00"),
        status="unpaid",
    )
    session.add(invoice)
    session.flush()

    content = b"%PDF-1.4 linked-then-reupload"

    att1 = attach_svc.save_bytes(
        db=session,
        company_id=company_id,
        content=content,
        original_filename="bill.pdf",
        invoice_id=invoice.id,
    )
    session.commit()

    att2 = attach_svc.save_bytes(
        db=session,
        company_id=company_id,
        content=content,
        original_filename="bill.pdf",
    )
    session.commit()

    assert att1.id != att2.id
    rows = session.query(Attachment).filter(Attachment.sha256 == att1.sha256).all()
    assert len(rows) == 2
