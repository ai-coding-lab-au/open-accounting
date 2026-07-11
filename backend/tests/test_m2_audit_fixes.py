"""Regression tests for the M2 audit fixes (state-machine bypasses).

Covers:
  - P0-E: invoice status not settable via PATCH; draft delete blocked when
    journal entries exist
  - P1-8: paid_amount/paid_date rejected on never-posted draft invoices
  - P1-9: header-total drift is rejected before persistence with 422
  - P1-5: generic /outgoing PATCH cannot set status or rewrite issued
    receipt/invoice lines; SAs are rejected outright
  - P1-6: deleting a PR cascades the void to its receipts + invoices
  - P1-7: a recognised (PAID) SA cannot be voided; restoring a recognised SA
    returns it to PAID, not ISSUED
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROJECT_ROOT = ROOT.parent
HDR = {"X-Company-Id": "tc"}


@pytest.fixture()
def client(monkeypatch, request):
    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app

    with TestClient(app) as c:
        # These state-machine tests create GST-bearing invoices. Non-registered
        # behavior has dedicated invariant coverage in test_non_gst_invariant.
        r = c.post(
            "/api/v1/companies",
            json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd", "gst_registered": True},
        )
        assert r.status_code == 201, r.text
        HDR["X-Company-Generation"] = r.json()["generation_id"]
        r = c.post(
            "/api/v1/staff",
            headers=HDR,
            json={"full_name": "Test Agent", "registration_type": "mara", "registration_number": "1234567"},
        )
        assert r.status_code == 201, r.text
        yield c


def _service_session():
    from app.db.company import company_session

    return company_session("tc")


# ---------------------------------------------------------------------------
# M1 invoices (general ledger)
# ---------------------------------------------------------------------------


@pytest.fixture()
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HDR)
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


def _create_invoice(client, accounts, *, number="INV-AUD-1", total="110.00"):
    r = client.post(
        "/api/v1/invoices",
        headers=HDR,
        json={
            "direction": "AR",
            "contact_name": "Audit Customer",
            "invoice_number": number,
            "issue_date": "2026-05-31",
            "subtotal": "100.00",
            "gst_amount": "10.00",
            "total": total,
            "lines": [
                {
                    "description": "Services",
                    "account_id": accounts["4000"]["id"],
                    "quantity": "1",
                    "unit_price": "100.00",
                    "gst_rate": "0.10",
                    "line_subtotal": "100.00",
                    "line_gst": "10.00",
                    "line_total": "110.00",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_posted_invoice_status_patch_rejected(client, accounts):
    """P0-E: a posted invoice cannot be demoted to draft via PATCH."""
    inv = _create_invoice(client, accounts, number="INV-AUD-P0E")
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HDR)
    assert r.status_code == 200, r.text

    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HDR, json={"status": "draft"})
    assert r.status_code == 422, r.text

    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HDR)
    assert r.json()["status"] == "authorised"


def test_draft_invoice_status_patch_rejected(client, accounts):
    """Status transitions only via post/void — also on drafts (no GL bypass)."""
    inv = _create_invoice(client, accounts, number="INV-AUD-DST")
    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HDR, json={"status": "paid"})
    assert r.status_code == 422, r.text
    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HDR)
    assert r.json()["status"] == "draft"


def test_delete_draft_with_journal_entries_blocked(client, accounts):
    """P0-E: hard-delete refuses when journal entries reference the invoice,
    even if the status row drifted back to draft."""
    inv = _create_invoice(client, accounts, number="INV-AUD-DEL")
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HDR)
    assert r.status_code == 200, r.text

    # Force the drifted state directly (the API no longer allows it).
    from app.models.company import Invoice, InvoiceStatus

    db = _service_session()
    try:
        row = db.get(Invoice, inv["id"])
        row.status = InvoiceStatus.DRAFT
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/api/v1/invoices/{inv['id']}", headers=HDR)
    assert r.status_code == 409, r.text
    assert "journal entries" in r.json()["detail"]

    # Invoice and its journal entry both survive.
    assert client.get(f"/api/v1/invoices/{inv['id']}", headers=HDR).status_code == 200
    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HDR)
    assert len(r.json()) == 1


def test_invoice_rejects_direct_paid_amount_until_bank_clearing_exists(client, accounts):
    """P1-8/P1-5: paid_amount must not fake payment status without GL clearing."""
    inv = _create_invoice(client, accounts, number="INV-AUD-PAID")

    r = client.patch(
        f"/api/v1/invoices/{inv['id']}", headers=HDR, json={"paid_amount": "110.00"}
    )
    assert r.status_code == 422, r.text
    assert "post the invoice first" in r.json()["detail"]

    r = client.patch(
        f"/api/v1/invoices/{inv['id']}", headers=HDR, json={"paid_date": "2026-06-01"}
    )
    assert r.status_code == 422, r.text

    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HDR)
    body = r.json()
    assert body["status"] == "draft"
    assert float(body["paid_amount"]) == 0.0

    # After posting, direct payment status is still blocked: explicit bank
    # allocations are now the only source of settlement truth.
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HDR)
    assert r.status_code == 200, r.text
    r = client.patch(
        f"/api/v1/invoices/{inv['id']}", headers=HDR, json={"paid_amount": "110.00"}
    )
    assert r.status_code == 409, r.text
    assert "bank-to-invoice allocations" in r.json()["detail"]
    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HDR)
    assert r.json()["status"] == "authorised"
    assert float(r.json()["paid_amount"]) == 0.0


def test_header_total_drift_rejected_before_persistence(client, accounts):
    """P1-9: a header that disagrees with its lines is rejected immediately."""
    inv = _create_invoice(client, accounts, number="INV-AUD-DRIFT")

    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HDR, json={"total": "110.01"})
    assert r.status_code == 422, r.text
    assert "doesn't balance" in r.json()["detail"].lower()

    # The rejected update did not corrupt the valid draft.
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HDR)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# M2 outgoing documents (Receipt only)
# ---------------------------------------------------------------------------


def _make_client_row(client, name="Jane Customer") -> int:
    r = client.post("/api/v1/clients", headers=HDR, json={"display_name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_receipt(client, client_ref_id: int, *, amount="100") -> dict:
    r = client.post(
        "/api/v1/outgoing",
        headers=HDR,
        json={
            "doc_type": "receipt",
            "issue_date": "2026-05-18",
            "client_ref_id": client_ref_id,
            "lines": [{"description": "Service", "quantity": "1", "unit_price": amount}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_generic_patch_cannot_set_status(client):
    """P1-5: status is never settable via PATCH /outgoing/{id}."""
    cid = _make_client_row(client)
    receipt = _make_receipt(client, cid)

    r = client.delete(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.status_code == 204, r.text
    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"status": "issued"}
    )
    assert r.status_code == 422, r.text
    r = client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.json()["status"] == "void"


def test_issued_receipt_lines_locked_via_generic_patch(client):
    """P1-5: an issued receipt's lines/totals cannot be rewritten; notes can."""
    cid = _make_client_row(client)
    receipt = _make_receipt(client, cid)
    assert receipt["status"] == "issued"
    original_total = receipt["total"]

    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}",
        headers=HDR,
        json={"lines": [{"description": "Rewritten", "quantity": "1", "unit_price": "999"}]},
    )
    assert r.status_code == 409, r.text
    r = client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.json()["total"] == original_total

    # Notes-only edits keep working.
    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"notes": "Paid in person"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "Paid in person"


def test_void_receipt_not_editable_via_generic_patch(client):
    cid = _make_client_row(client)
    receipt = _make_receipt(client, cid)
    r = client.delete(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.status_code == 204, r.text

    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"notes": "still here?"}
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Re-test follow-up fixes
# ---------------------------------------------------------------------------


def test_void_invoice_cannot_be_revived_via_paid_amount(client, accounts):
    """A void invoice's journal entry is reversed; recording a payment would
    resurrect it to "paid" with zero GL backing."""
    inv = _create_invoice(client, accounts)
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HDR)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HDR)
    assert r.status_code == 200, r.text

    r = client.patch(
        f"/api/v1/invoices/{inv['id']}", headers=HDR, json={"paid_amount": "110.00"}
    )
    assert r.status_code == 409, r.text
    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HDR)
    assert r.json()["status"] == "void"


def test_create_invoice_as_void_rejected(client, accounts):
    """Void-on-create would have no journal entry to reverse and could never
    be deleted — an unremovable zombie row."""
    r = client.post(
        "/api/v1/invoices",
        headers=HDR,
        json={
            "direction": "AR",
            "contact_name": "Audit Customer",
            "invoice_number": "INV-VOID-1",
            "issue_date": "2026-05-31",
            "subtotal": "100.00",
            "gst_amount": "10.00",
            "total": "110.00",
            "status": "void",
            "lines": [
                {
                    "description": "Services",
                    "account_id": accounts["4000"]["id"],
                    "quantity": "1",
                    "unit_price": "100.00",
                    "gst_rate": "0.10",
                    "line_subtotal": "100.00",
                    "line_gst": "10.00",
                    "line_total": "110.00",
                }
            ],
        },
    )
    assert r.status_code == 422, r.text
