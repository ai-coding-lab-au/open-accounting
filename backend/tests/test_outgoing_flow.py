"""End-to-end test for the outgoing documents flow (Receipt only)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECT_ROOT = ROOT.parent


@pytest.fixture()
def client(monkeypatch, request):
    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        import shutil

        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app  # noqa: WPS433

    with TestClient(app) as c:
        c.test_data_dir = test_data  # type: ignore[attr-defined]
        yield c


def _make_company(client) -> dict:
    r = client.post(
        "/api/v1/companies",
        json={"id": "acme", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Example Migration Services", "gst_registered": False},
    )
    assert r.status_code == 201, r.text
    company = r.json()
    HDR["X-Company-Generation"] = company["generation_id"]
    return company


HDR = {"X-Company-Id": "acme"}


def _create_client(client, name: str) -> dict:
    r = client.post(
        "/api/v1/clients",
        headers=HDR,
        json={"display_name": name, "email": f"{name.lower().replace(' ', '.')}@example.com"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_receipt(client, customer: str, *, amount: str = "100", override: str | None = None) -> dict:
    client_row = _create_client(client, customer)
    payload = {
        "doc_type": "receipt",
        "issue_date": "2026-05-18",
        "client_ref_id": client_row["id"],
        "lines": [{"description": "Migration agent service", "quantity": "1", "unit_price": amount}],
    }
    if override:
        payload["doc_number_override"] = override
    r = client.post("/api/v1/outgoing", headers=HDR, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_duplicate_doc_number_override_clean_error(client):
    """A duplicate doc_number override returns a clean 409 — never the raw SQL
    / schema (no INSERT, no table/column names) leaked to the client."""
    _make_company(client)
    client_row = _create_client(client, "Dup Co")
    base = {
        "doc_type": "receipt",
        "issue_date": "2026-05-18",
        "client_ref_id": client_row["id"],
        "lines": [{"description": "Service", "quantity": "1", "unit_price": "100"}],
        "doc_number_override": "RCT-DUP-0001",
    }
    r = client.post("/api/v1/outgoing", headers=HDR, json=base)
    assert r.status_code == 201, r.text

    r = client.post("/api/v1/outgoing", headers=HDR, json=base)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "RCT-DUP-0001 already exists" in detail
    # No raw SQL / schema leakage.
    for leak in ("INSERT INTO", "outgoing_documents", "IntegrityError", "UNIQUE constraint", "(Background"):
        assert leak not in detail, f"leaked: {detail}"


def test_receipt_numbering_and_pdf(client):
    _make_company(client)

    # The editable counter controls the receipt document sequence.
    r = client.put(
        "/api/v1/outgoing/counters",
        headers=HDR,
        json={"doc_type": "receipt", "year": 2026, "last_number": 41},
    )
    assert r.status_code == 200, r.text
    assert r.json()["next_preview"] == "RCT-2026-0042-1"

    receipt = _create_receipt(client, "Acme Pty Ltd", amount="2000")
    assert receipt["doc_number"] == "RCT-2026-0042-1"
    assert receipt["doc_type"] == "receipt"
    assert receipt["status"] == "issued"
    assert float(receipt["subtotal"]) == 2000.0
    assert float(receipt["total"]) == 2000.0
    assert float(receipt["gst_amount"]) == 0.0

    r = client.post(f"/api/v1/outgoing/{receipt['id']}/pdf", headers=HDR)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"

    r = client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    refreshed = r.json()
    assert refreshed["status"] == "issued"
    assert refreshed["pdf_rel_path"] is not None

    receipt2 = _create_receipt(client, "Other Co", amount="2000")
    assert receipt2["doc_number"] == "RCT-2026-0043-1"


def test_direct_receipt_creation(client):
    _make_company(client)
    client_row = _create_client(client, "Direct Receipt Co")

    r = client.post(
        "/api/v1/outgoing",
        headers=HDR,
        json={
            "doc_type": "receipt",
            "issue_date": "2026-05-18",
            "client_ref_id": client_row["id"],
            "lines": [{"description": "Service", "quantity": "1", "unit_price": "100"}],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["doc_type"] == "receipt"
    assert body["status"] == "issued"
    assert body["paid_date"] == "2026-05-18"
    assert body["payment_method"] == "Bank transfer"
    assert body["customer_name"] == "Direct Receipt Co"


def test_receipt_create_idempotency_replays_and_rejects_key_reuse(client):
    _make_company(client)
    client_row = _create_client(client, "Retry Safe Co")
    payload = {
        "doc_type": "receipt",
        "issue_date": "2026-05-18",
        "client_ref_id": client_row["id"],
        "lines": [
            {"description": "Service", "quantity": "1", "unit_price": "100"}
        ],
    }
    headers = {**HDR, "Idempotency-Key": "receipt-timeout-retry-1"}

    first = client.post("/api/v1/outgoing", headers=headers, json=payload)
    retry = client.post("/api/v1/outgoing", headers=headers, json=payload)
    assert first.status_code == retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["doc_number"] == first.json()["doc_number"]
    assert len(client.get("/api/v1/outgoing", headers=HDR).json()) == 1

    changed = {
        **payload,
        "lines": [
            {"description": "Service", "quantity": "1", "unit_price": "101"}
        ],
    }
    conflict = client.post("/api/v1/outgoing", headers=headers, json=changed)
    assert conflict.status_code == 409, conflict.text
    assert "different receipt payload" in conflict.json()["detail"]
    assert len(client.get("/api/v1/outgoing", headers=HDR).json()) == 1


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [{"description": "Zero receipt", "quantity": "1", "unit_price": "0"}],
        [
            {
                "description": "Contradictory line",
                "quantity": "2",
                "unit_price": "10",
                "amount": "10",
            }
        ],
        [
            {
                "description": "Overflow",
                "quantity": "2",
                "unit_price": "99999999999999.99",
            }
        ],
    ],
)
def test_receipt_rejects_empty_zero_contradictory_and_overflow_totals(client, lines):
    _make_company(client)
    client_row = _create_client(client, "Bounded Receipt Co")
    response = client.post(
        "/api/v1/outgoing",
        headers={**HDR, "Idempotency-Key": "invalid-receipt"},
        json={
            "doc_type": "receipt",
            "issue_date": "2026-05-18",
            "client_ref_id": client_row["id"],
            "lines": lines,
        },
    )
    assert response.status_code == 422, response.text
    assert client.get("/api/v1/outgoing", headers=HDR).json() == []


def test_issued_receipt_keeps_issuer_snapshot_after_company_profile_changes(
    client, monkeypatch
):
    _make_company(client)
    r = client.patch(
        "/api/v1/companies/acme",
        headers=HDR,
        json={
            "name": "Original Issuer Pty Ltd",
            "abn": "11 222 333 444",
            "address_line1": "1 Original Street",
            "bank_name": "Original Bank",
            "bank_account_number": "12345678",
        },
    )
    assert r.status_code == 200, r.text
    receipt = _create_receipt(client, "Snapshot Customer", amount="100")

    # Later company settings must affect future receipts, not rewrite the legal
    # identity, payment details, or GST status of an already-issued receipt.
    r = client.patch(
        "/api/v1/companies/acme",
        headers=HDR,
        json={
            "name": "Replacement Issuer Pty Ltd",
            "abn": "99 888 777 666",
            "address_line1": "9 Replacement Avenue",
            "bank_name": "Replacement Bank",
            "bank_account_number": "99999999",
            "gst_registered": True,
        },
    )
    assert r.status_code == 200, r.text

    import app.api.v1.outgoing as outgoing_mod

    captured = {}

    def capture_render(**kwargs):
        captured.update(kwargs)
        return b"%PDF-1.4\nissuer snapshot\n%%EOF"

    monkeypatch.setattr(outgoing_mod.html_render, "render_document_pdf", capture_render)
    r = client.post(f"/api/v1/outgoing/{receipt['id']}/pdf", headers=HDR)
    assert r.status_code == 200, r.text
    assert captured["company"]["name"] == "Original Issuer Pty Ltd"
    assert captured["company"]["abn"] == "11 222 333 444"
    assert captured["company"]["address_line1"] == "1 Original Street"
    assert captured["company"]["bank_name"] == "Original Bank"
    assert captured["company"]["bank_account_number"] == "12345678"
    assert captured["is_gst_registered"] is False


def test_gst_registered_adds_gst_on_top(client):
    r = client.post(
        "/api/v1/companies",
        json={"id": "gstco", "marn": "1234567", "registered_agent_name": "A", "name": "GST Co", "gst_registered": True},
    )
    assert r.status_code == 201, r.text
    hdr = {
        "X-Company-Id": "gstco",
        "X-Company-Generation": r.json()["generation_id"],
    }
    cl = client.post("/api/v1/clients", headers=hdr, json={"display_name": "Client A"}).json()
    r = client.post(
        "/api/v1/outgoing",
        headers=hdr,
        json={
            "doc_type": "receipt",
            "issue_date": "2026-05-18",
            "client_ref_id": cl["id"],
            "lines": [{"description": "Service", "quantity": "1", "unit_price": "100"}],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert float(body["subtotal"]) == 100.0
    assert float(body["gst_amount"]) == 10.0
    assert float(body["total"]) == 110.0


def test_receipt_void_and_restore(client):
    _make_company(client)
    receipt = _create_receipt(client, "Restore Co")

    r = client.delete(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.status_code == 204, r.text

    r = client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "void"

    r = client.post(f"/api/v1/outgoing/{receipt['id']}/restore", headers=HDR)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "issued"

    # Restoring a non-void document is rejected.
    r = client.post(f"/api/v1/outgoing/{receipt['id']}/restore", headers=HDR)
    assert r.status_code == 400


def test_issued_receipt_lines_are_locked(client):
    _make_company(client)
    receipt = _create_receipt(client, "Locked Co", amount="100")

    # Notes-only edits are allowed on an issued receipt.
    r = client.patch(f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"notes": "ok"})
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "ok"

    # Editing lines/totals after issue is locked.
    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}",
        headers=HDR,
        json={"lines": [{"description": "New", "quantity": "1", "unit_price": "200"}]},
    )
    assert r.status_code == 409


def test_issued_receipt_issue_date_is_locked(client):
    """An issued receipt's issue_date can't be back-dated in place (notes still
    editable); change the date via void + re-issue."""
    _make_company(client)
    receipt = _create_receipt(client, "Date Lock Co")  # created ISSUED
    r = client.patch(f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"notes": "ok"})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"issue_date": "2024-03-15"})
    assert r.status_code == 409, r.text


def test_issued_receipt_payee_and_currency_are_locked(client):
    """An issued receipt must not be silently re-pointed to a different client
    or relabelled to another currency under the same document number — only
    notes are editable once issued."""
    _make_company(client)
    receipt = _create_receipt(client, "Payee Lock Co")  # created ISSUED
    other = _create_client(client, "Different Client")

    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}", headers=HDR,
        json={"client_ref_id": other["id"]},
    )
    assert r.status_code == 409, r.text

    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"currency": "USD"},
    )
    # AUD-only validation now rejects this before the issued-document lock is
    # evaluated.  Either way the persisted receipt must remain unchanged.
    assert r.status_code == 422, r.text

    # The stored payee is unchanged.
    r = client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.json()["customer_name"] == "Payee Lock Co"
    assert r.json()["currency"] == "AUD"


def test_void_receipt_pdf_render_does_not_repersist(client):
    """Voiding deletes the receipt's PII PDF and clears pdf_rel_path. Re-opening
    a void receipt may still render to the response, but must NOT re-write the
    PDF to disk or repopulate pdf_rel_path — otherwise void's PII cleanup is
    silently defeated by simply viewing the document."""
    _make_company(client)
    receipt = _create_receipt(client, "Void PDF Co")

    # Issue the PDF: persisted to disk, pdf_rel_path set.
    assert client.post(f"/api/v1/outgoing/{receipt['id']}/pdf", headers=HDR).status_code == 200
    assert client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR).json()["pdf_rel_path"] is not None

    # Void: on-disk PII PDF removed, pdf_rel_path cleared.
    assert client.delete(f"/api/v1/outgoing/{receipt['id']}", headers=HDR).status_code == 204
    assert client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR).json()["pdf_rel_path"] is None

    # Re-render a void receipt: still returns the PDF bytes...
    r = client.post(f"/api/v1/outgoing/{receipt['id']}/pdf", headers=HDR)
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    # ...but pdf_rel_path stays null (nothing re-written to disk).
    assert client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR).json()["pdf_rel_path"] is None


def test_void_racing_pdf_render_does_not_persist(client, monkeypatch):
    """The void-vs-render TOCTOU: a void that commits WHILE a PDF is rendering
    must still win — render must not write the PII PDF back to disk or
    repopulate pdf_rel_path. Simulated by voiding on a separate DB connection
    from inside the render call, then asserting nothing was persisted."""
    _make_company(client)
    receipt = _create_receipt(client, "Race Co")

    from app.api.v1 import outgoing as outgoing_mod
    from app.db.company import company_session
    from app.models.outgoing import OutgoingDocument, DocumentStatus

    orig_render = outgoing_mod._render_for

    def racing_render(doc, company):
        pdf = orig_render(doc, company)
        # A concurrent void commits on another connection mid-render.
        with company_session("acme") as s:
            d = s.get(OutgoingDocument, receipt["id"])
            d.status = DocumentStatus.VOID
            d.pdf_rel_path = None
            s.commit()
        return pdf

    monkeypatch.setattr(outgoing_mod, "_render_for", racing_render)

    r = client.post(f"/api/v1/outgoing/{receipt['id']}/pdf", headers=HDR)
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"  # still viewable in the response

    got = client.get(f"/api/v1/outgoing/{receipt['id']}", headers=HDR).json()
    assert got["status"] == "void"
    assert got["pdf_rel_path"] is None  # the racing void wins; nothing persisted


def test_void_receipt_cannot_be_edited(client):
    _make_company(client)
    receipt = _create_receipt(client, "Void Edit Co")
    r = client.delete(f"/api/v1/outgoing/{receipt['id']}", headers=HDR)
    assert r.status_code == 204, r.text

    r = client.patch(f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"notes": "nope"})
    assert r.status_code == 400


def test_status_not_settable_via_patch(client):
    _make_company(client)
    receipt = _create_receipt(client, "Status Co")
    r = client.patch(f"/api/v1/outgoing/{receipt['id']}", headers=HDR, json={"status": "void"})
    assert r.status_code == 422


def test_filters_and_search(client):
    _make_company(client)

    _create_receipt(client, "Alpha Co")
    _create_receipt(client, "Bravo Co")
    _create_receipt(client, "Charlie Co")

    r = client.get("/api/v1/outgoing", headers=HDR, params={"doc_type": "receipt"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    assert all(row["doc_type"] == "receipt" for row in rows)

    r = client.get("/api/v1/outgoing", headers=HDR, params={"q": "Charlie"})
    assert len(r.json()) == 1
    assert r.json()[0]["customer_name"] == "Charlie Co"


def test_doc_number_override_uniqueness(client):
    _make_company(client)
    first = _create_receipt(client, "X", override="RCT-2026-9999-1")
    assert first["doc_number"] == "RCT-2026-9999-1"

    r = client.post(
        "/api/v1/outgoing",
        headers=HDR,
        json={
            "doc_type": "receipt",
            "issue_date": "2026-05-18",
            "client_ref_id": first["client_ref_id"],
            "lines": [{"description": "Service", "quantity": "1", "unit_price": "100"}],
            "doc_number_override": "RCT-2026-9999-1",
        },
    )
    assert r.status_code == 409


def test_unknown_field_rejected_on_create(client):
    """Sending an unknown field (e.g. `doc_number` — the override field is
    `doc_number_override`) is rejected, not silently ignored."""
    _make_company(client)
    cid = _create_client(client, "Extra Create Co")["id"]
    r = client.post(
        "/api/v1/outgoing",
        headers=HDR,
        json={
            "doc_type": "receipt",
            "issue_date": "2026-05-18",
            "client_ref_id": cid,
            "lines": [{"description": "x", "quantity": "1", "unit_price": "100"}],
            "doc_number": "RCT-2026-9999-1",
        },
    )
    assert r.status_code == 422, r.text


def test_unknown_field_rejected_on_patch(client):
    _make_company(client)
    receipt = _create_receipt(client, "Patch Extra Co")
    r = client.patch(
        f"/api/v1/outgoing/{receipt['id']}",
        headers=HDR,
        json={"doc_number": "RCT-2026-0001-9"},
    )
    assert r.status_code == 422, r.text


def test_company_patch_persists_bank_details(client):
    _make_company(client)
    r = client.patch(
        "/api/v1/companies/acme",
        headers=HDR,
        json={
            "address_line1": "Suite 1, 100 George St",
            "suburb": "Sydney",
            "state": "NSW",
            "postcode": "2000",
            "phone": "+61 2 1234 5678",
            "email": "hello@example.example",
            "bank_account_name": "Example Migration Services Pty Ltd",
            "bank_name": "ANZ",
            "bank_bsb": "012-345",
            "bank_account_number": "1234 5678",
        },
    )
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["bank_bsb"] == "012-345"
    assert c["suburb"] == "Sydney"

    receipt = _create_receipt(client, "Sample", amount="10")
    r = client.post(f"/api/v1/outgoing/{receipt['id']}/pdf", headers=HDR)
    assert r.status_code == 200
    assert len(r.content) > 1000  # rendered something substantive


def test_bilingual_labels_toggle_flows_through_to_rendered_pdf(client):
    """Settings toggle (Company.bilingual_labels) → PATCH → _render_for →
    rendered receipt shows "ENGLISH 中文" labels."""
    import io

    import pdfplumber

    _make_company(client)

    receipt = _create_receipt(client, "Toggle Off Co", amount="500")
    r = client.post(f"/api/v1/outgoing/{receipt['id']}/pdf", headers=HDR)
    assert r.status_code == 200
    with pdfplumber.open(io.BytesIO(r.content)) as doc:
        text = doc.pages[0].extract_text() or ""
    assert "收据" not in text  # default: English-only labels

    r = client.patch(
        "/api/v1/companies/acme",
        headers=HDR,
        json={"bilingual_labels": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["bilingual_labels"] is True

    receipt2 = _create_receipt(client, "Toggle On Co", amount="600")
    r = client.post(f"/api/v1/outgoing/{receipt2['id']}/pdf", headers=HDR)
    assert r.status_code == 200
    with pdfplumber.open(io.BytesIO(r.content)) as doc:
        text = doc.pages[0].extract_text() or ""
    assert "收据" in text
    assert "付款方式" in text
    assert "RECEIPT" in text  # English retained
