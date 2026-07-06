"""Round-2 robustness regressions (docs/audits/2026-06-10-deep-audit.md ROUND 2).

Each test reproduces a verified fuzz/robustness finding that previously 500'd
(or stored bad data) and asserts the hardened behaviour.
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
HEAD = {"X-Company-Id": "tc"}


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
        r = c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        assert r.status_code == 201, r.text
        yield c


@pytest.fixture()
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HEAD)
    return {a["code"]: a for a in r.json()}


def _invoice_payload(accounts, *, number="R2-1"):
    return {
        "direction": "AR",
        "contact_name": "R2 Customer",
        "invoice_number": number,
        "issue_date": "2026-05-31",
        "subtotal": "100.00",
        "gst_amount": "10.00",
        "total": "110.00",
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
    }


def _create_invoice(client, accounts, *, number="R2-1"):
    r = client.post("/api/v1/invoices", headers=HEAD, json=_invoice_payload(accounts, number=number))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# R2-FUZZ-1: oversized integer path id → 422/404, never 500
# ---------------------------------------------------------------------------


def test_oversized_path_id_does_not_500(client):
    huge = 2**63  # one past signed-64 max
    r = client.get(f"/api/v1/invoices/{huge}", headers=HEAD)
    assert r.status_code == 422, r.text
    # A valid-range but absent id still 404s (not blocked by the bound).
    r2 = client.get(f"/api/v1/invoices/{2**63 - 1}", headers=HEAD)
    assert r2.status_code == 404, r2.text


# ---------------------------------------------------------------------------
# R2-FUZZ-2: PATCH explicit null on a NOT-NULL column → 422, omit still works
# ---------------------------------------------------------------------------


def test_patch_explicit_null_on_not_null_fields_rejected(client, accounts):
    inv = _create_invoice(client, accounts, number="R2-NULL")
    for field in ("subtotal", "issue_date", "direction"):
        r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HEAD, json={field: None})
        assert r.status_code == 422, f"{field}: {r.text}"
    # Omitting them (a no-op PATCH of an allowed field) still works.
    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HEAD, json={"notes": "ok"})
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "ok"


# ---------------------------------------------------------------------------
# R2-FUZZ-3: bank-rule amount bounds (le=MONEY_MAX, decimal_places=2)
# ---------------------------------------------------------------------------


def test_bank_rule_amount_bounds(client, accounts):
    rent = accounts["6100"]
    base = {
        "priority": 50,
        "description": "amount bound test",
        "set_account_id": rent["id"],
        "set_tax_code": "standard",
    }
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={**base, "match_amount_min": "1e500"})
    assert r.status_code == 422, r.text
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={**base, "match_amount_max": "100.123"})
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# R2-FUZZ-4: whitespace-only contact_name → 422, not 500
# ---------------------------------------------------------------------------


def test_blank_contact_name_rejected(client, accounts):
    payload = _invoice_payload(accounts, number="R2-BLANK")
    payload["contact_name"] = "   "
    r = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# R2-B-regex: catastrophic-backtracking regex rejected at rule create
# ---------------------------------------------------------------------------


def test_redos_regex_rejected_at_create(client, accounts):
    rent = accounts["6100"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 50,
        "description": "redos",
        "match_memo_regex": "(a+)+$",
        "set_account_id": rent["id"],
        "set_tax_code": "standard",
    })
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# R2-B-xss: attachment download serves a safe fixed content type
# ---------------------------------------------------------------------------


def test_attachment_download_serves_safe_content_type(client, accounts):
    # Upload a PDF claiming Content-Type text/html.
    files = {"file": ("evil.pdf", b"%PDF-1.4 fake", "text/html")}
    r = client.post("/api/v1/invoices/upload-pdf", headers=HEAD, files=files)
    assert r.status_code == 200, r.text
    att_id = r.json()["attachment_id"]

    payload = _invoice_payload(accounts, number="R2-XSS")
    payload["attachment_id"] = att_id
    inv = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert inv.status_code == 201, inv.text
    inv_id = inv.json()["id"]

    r = client.get(f"/api/v1/invoices/{inv_id}/attachment", headers=HEAD)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert "text/html" not in r.headers["content-type"]


# ---------------------------------------------------------------------------
# R2-B-orphan: deleting an invoice unlinks its attachment row + file
# ---------------------------------------------------------------------------


def test_delete_invoice_cleans_up_attachment(client, accounts):
    files = {"file": ("bill.pdf", b"%PDF-1.4 orphan-test bytes", "application/pdf")}
    r = client.post("/api/v1/invoices/upload-pdf", headers=HEAD, files=files)
    assert r.status_code == 200, r.text
    att_id = r.json()["attachment_id"]

    payload = _invoice_payload(accounts, number="R2-ORPHAN")
    payload["attachment_id"] = att_id
    inv = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert inv.status_code == 201, inv.text
    inv_id = inv.json()["id"]

    # Capture the on-disk path before deletion.
    from app.models.company import Attachment
    from app.services import attachments as attach_svc
    from app.db.company import company_session

    with company_session("tc") as s:
        att = s.get(Attachment, att_id)
        abs_path = attach_svc.attachment_absolute_path("tc", att)
    assert abs_path.exists()

    r = client.delete(f"/api/v1/invoices/{inv_id}", headers=HEAD)
    assert r.status_code == 204, r.text

    # Row and file are both gone.
    with company_session("tc") as s:
        assert s.get(Attachment, att_id) is None
    assert not abs_path.exists()


def test_delete_draft_with_shared_attachment_keeps_other_invoice_file(client, accounts):
    content = b"%PDF-1.4 shared attachment bytes"

    r = client.post(
        "/api/v1/invoices/upload-pdf",
        headers=HEAD,
        files={"file": ("bill.pdf", content, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    att1_id = r.json()["attachment_id"]

    payload = _invoice_payload(accounts, number="R2-SHARED-POSTED")
    payload["attachment_id"] = att1_id
    inv1 = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert inv1.status_code == 201, inv1.text
    inv1_id = inv1.json()["id"]
    posted = client.post(f"/api/v1/invoices/{inv1_id}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text

    r = client.post(
        "/api/v1/invoices/upload-pdf",
        headers=HEAD,
        files={"file": ("bill-copy.pdf", content, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    att2_id = r.json()["attachment_id"]
    assert att2_id != att1_id

    payload = _invoice_payload(accounts, number="R2-SHARED-DRAFT")
    payload["attachment_id"] = att2_id
    inv2 = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert inv2.status_code == 201, inv2.text
    inv2_id = inv2.json()["id"]

    from app.models.company import Attachment
    from app.services import attachments as attach_svc
    from app.db.company import company_session

    with company_session("tc") as s:
        att1 = s.get(Attachment, att1_id)
        att2 = s.get(Attachment, att2_id)
        assert att1.rel_path == att2.rel_path
        abs_path = attach_svc.attachment_absolute_path("tc", att1)
    assert abs_path.exists()

    deleted = client.delete(f"/api/v1/invoices/{inv2_id}", headers=HEAD)
    assert deleted.status_code == 204, deleted.text

    with company_session("tc") as s:
        assert s.get(Attachment, att1_id) is not None
        assert s.get(Attachment, att2_id) is None
    assert abs_path.exists()
    download = client.get(f"/api/v1/invoices/{inv1_id}/attachment", headers=HEAD)
    assert download.status_code == 200, download.text
