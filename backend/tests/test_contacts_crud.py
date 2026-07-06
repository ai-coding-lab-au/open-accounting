"""Tests for the Contacts (Providers) edit/delete endpoints."""

from __future__ import annotations

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
        import shutil
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app
    with TestClient(app) as c:
        c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        yield c


def _create(client, name: str, kind: str = "supplier") -> int:
    r = client.post("/api/v1/contacts", headers=HEAD, json={"name": name, "kind": kind})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_update_contact_fields(client):
    cid = _create(client, "Acme Pty Ltd")
    r = client.patch(
        f"/api/v1/contacts/{cid}",
        headers=HEAD,
        json={"abn": "12345678901", "email": "billing@acme.test", "phone": "+61 2 0000"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["abn"] == "12345678901"
    assert body["email"] == "billing@acme.test"


def test_update_rejects_name_clash(client):
    _create(client, "Alpha")
    cid = _create(client, "Bravo")
    r = client.patch(
        f"/api/v1/contacts/{cid}",
        headers=HEAD,
        json={"name": "Alpha"},
    )
    assert r.status_code == 409


def test_delete_unused_contact(client):
    cid = _create(client, "Delete Me")
    r = client.delete(f"/api/v1/contacts/{cid}", headers=HEAD)
    assert r.status_code == 204


def test_delete_contact_referenced_by_invoice_is_blocked(client):
    cid = _create(client, "Held By Invoice")
    r = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json={
            "direction": "AP",
            "contact_id": cid,
            "invoice_number": "INV-X",
            "issue_date": "2026-05-01",
            "subtotal": "100.00",
            "gst_amount": "10.00",
            "total": "110.00",
        },
    )
    assert r.status_code == 201, r.text

    r = client.delete(f"/api/v1/contacts/{cid}", headers=HEAD)
    assert r.status_code == 409
    assert "invoices" in r.json()["detail"]


def test_delete_contact_hook_registry_empty_path_proceeds(client, monkeypatch):
    """When no module has registered a contact reference check (the
    M1-standalone path), delete proceeds even if some future M2 table
    would have referenced the contact. Empties the registry for this
    test, then re-tests that an unrelated, non-invoice-referenced
    contact still deletes cleanly via the empty-registry path.
    """
    from app import hooks

    monkeypatch.setattr(hooks, "_contact_reference_checks", [])

    cid = _create(client, "Standalone Path")
    r = client.delete(f"/api/v1/contacts/{cid}", headers=HEAD)
    assert r.status_code == 204


def test_delete_contact_hook_registry_blocks_on_outgoing(client):
    """The M2 outgoing router registers a check on import. Verify it
    runs by creating an OutgoingDocument that references the contact,
    then asserting the delete returns 409 with the M2 hook's message.
    """
    cid = _create(client, "Held By Outgoing")
    # The manual POST /outgoing creation path leaves customer_id null
    # because the new flow uses client_ref_id. To exercise the hook we
    # set customer_id directly via a session, simulating any other code
    # path (legacy import, manual SQL) that points an outgoing doc at a
    # contact.
    from datetime import date
    from decimal import Decimal

    from app.db.company import company_session
    from app.models.outgoing import (
        DocumentStatus,
        DocumentType,
        OutgoingDocument,
    )

    db = company_session(HEAD["X-Company-Id"])
    try:
        doc = OutgoingDocument(
            doc_type=DocumentType.RECEIPT,
            doc_number="RCT-2026-0001-1",
            issue_date=date(2026, 5, 1),
            customer_id=cid,
            customer_name="Held By Outgoing",
            subtotal=Decimal("100.00"),
            gst_amount=Decimal("0.00"),
            total=Decimal("100.00"),
            status=DocumentStatus.DRAFT,
        )
        db.add(doc)
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/api/v1/contacts/{cid}", headers=HEAD)
    assert r.status_code == 409, r.text
    assert "outgoing documents" in r.json()["detail"].lower()


def test_update_404(client):
    r = client.patch("/api/v1/contacts/99999", headers=HEAD, json={"name": "x"})
    assert r.status_code == 404
