from __future__ import annotations

import shutil
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
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


def _invoice_payload(accounts, *, number="INV-API-1"):
    return {
        "direction": "AR",
        "contact_name": "API Customer",
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


def _create_invoice(client, accounts, *, number="INV-API-1"):
    r = client.post("/api/v1/invoices", headers=HEAD, json=_invoice_payload(accounts, number=number))
    assert r.status_code == 201, r.text
    return r.json()


def test_post_endpoint_creates_journal_and_list_filter_finds_it(client, accounts):
    inv = _create_invoice(client, accounts)
    assert inv["status"] == "draft"
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invoice"]["status"] == "authorised"
    assert body["journal_entry"]["source_type"] == "invoice_ar"

    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()] == [body["journal_entry"]["id"]]


def test_post_already_authorised_returns_409(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-2")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    assert r.status_code == 409


def test_void_endpoint_creates_reversal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-3")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invoice"]["status"] == "void"
    assert body["journal_entry"]["source_type"] == "invoice_reversal"
    assert body["journal_entry"]["reverses_entry_id"] is not None


def test_patch_financial_field_on_authorised_invoice_rejected(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-4")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HEAD, json={"subtotal": "90.00"})
    assert r.status_code == 422


def test_delete_draft_hard_deletes_without_journal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-5")
    r = client.delete(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 204, r.text
    assert client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD).status_code == 404
    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200
    assert r.json() == []


def test_delete_authorised_voids_and_posts_reversal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-6")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.delete(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 204, r.text
    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 200
    assert r.json()["status"] == "void"
    r = client.get("/api/v1/journal/entries?source_type=invoice_reversal", headers=HEAD)
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_concurrent_post_one_wins_other_409_not_500(client, accounts):
    """Round-3 P2: two simultaneous /post calls → [200, 409], one journal entry.
    The loser must not surface the DB unique-index IntegrityError as a 500.
    """
    inv = _create_invoice(client, accounts, number="INV-API-RACE-POST")

    def post():
        return client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(), range(2)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200
    entries = [e for e in r.json() if e["source_id"] == inv["id"]]
    assert len(entries) == 1


def test_concurrent_void_one_wins_other_409_not_500(client, accounts):
    """Round-3 P2: two simultaneous /void calls → [200, 409], one reversal entry."""
    inv = _create_invoice(client, accounts, number="INV-API-RACE-VOID")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200

    def void():
        return client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: void(), range(2)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    r = client.get("/api/v1/journal/entries?source_type=invoice_reversal", headers=HEAD)
    assert r.status_code == 200
    entries = [e for e in r.json() if e["source_id"] == inv["id"]]
    assert len(entries) == 1
