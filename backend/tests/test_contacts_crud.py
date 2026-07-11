"""Tests for the Contacts (Providers) edit/delete endpoints."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
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
        company = c.post(
            "/api/v1/companies",
            json={
                "id": "tc",
                "marn": "1234567",
                "registered_agent_name": "Test Agent",
                "name": "Test Pty Ltd",
            },
        )
        HEAD["X-Company-Generation"] = company.json()["generation_id"]
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


def test_contact_names_and_compact_fields_are_normalised(client):
    r = client.post(
        "/api/v1/contacts",
        headers=HEAD,
        json={
            "name": "  Acme Migration Services  ",
            "kind": "supplier",
            "abn": "12 345 678 901",
            "phone": "+61 2 0000 0000",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Acme Migration Services"
    assert body["abn"] == "12345678901"
    assert body["phone"] == "+61200000000"

    duplicate = client.post(
        "/api/v1/contacts",
        headers=HEAD,
        json={"name": "acme migration services", "kind": "supplier"},
    )
    assert duplicate.status_code == 409

    for method, path in (
        (client.post, "/api/v1/contacts"),
        (client.patch, f"/api/v1/contacts/{body['id']}"),
    ):
        response = method(path, headers=HEAD, json={"name": "   "})
        assert response.status_code == 422, response.text


def test_contact_ui_patch_null_clears_and_omission_preserves(client):
    created = client.post(
        "/api/v1/contacts",
        headers=HEAD,
        json={
            "name": "Provider One",
            "kind": "supplier",
            "abn": "12345678901",
            "email": "accounts@provider.test",
            "phone": "0400000000",
            "address": "1 Provider Street",
            "notes": "Initial note",
            "active": True,
        },
    )
    assert created.status_code == 201, created.text
    contact_id = created.json()["id"]

    # Providers.tsx sends this full object shape for edits, using JSON null for
    # cleared optional inputs.
    updated = client.patch(
        f"/api/v1/contacts/{contact_id}",
        headers=HEAD,
        json={
            "name": "  Provider One Renamed  ",
            "kind": "both",
            "abn": None,
            "email": None,
            "phone": None,
            "address": None,
            "notes": None,
            "active": False,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["name"] == "Provider One Renamed"
    assert body["kind"] == "both"
    assert body["active"] is False
    for field in ("abn", "email", "phone", "address", "notes"):
        assert body[field] is None

    omitted = client.patch(
        f"/api/v1/contacts/{contact_id}",
        headers=HEAD,
        json={"email": "new@provider.test"},
    )
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["name"] == "Provider One Renamed"
    assert omitted.json()["kind"] == "both"
    assert omitted.json()["active"] is False


@pytest.mark.parametrize("field", ["kind", "name", "active"])
def test_contact_patch_rejects_explicit_null_for_required_fields(client, field):
    contact_id = _create(client, f"Required field {field}")
    r = client.patch(
        f"/api/v1/contacts/{contact_id}",
        headers=HEAD,
        json={field: None},
    )
    assert r.status_code == 422, r.text


def test_concurrent_contact_name_check_is_serialised(client):
    def create(name: str):
        return client.post(
            "/api/v1/contacts",
            headers=HEAD,
            json={"name": name, "kind": "supplier"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, ("Race Provider", "race provider")))

    assert sorted(response.status_code for response in responses) == [201, 409]
    rows = client.get(
        "/api/v1/contacts",
        headers=HEAD,
        params={"q": "Race Provider"},
    )
    assert rows.status_code == 200, rows.text
    assert len(rows.json()) == 1


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
