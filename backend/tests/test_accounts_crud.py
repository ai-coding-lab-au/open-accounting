"""Tests for the Accounts (Chart of Accounts) CRUD endpoints."""

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


def test_create_account(client):
    r = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "6420", "name": "Marketing", "type": "EXPENSE"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "6420"
    assert body["active"] is True
    assert body["type"] == "EXPENSE"


def test_create_duplicate_code_conflicts(client):
    client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "6420", "name": "Marketing", "type": "EXPENSE"},
    )
    r = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "6420", "name": "Other", "type": "EXPENSE"},
    )
    assert r.status_code == 409


def test_update_account(client):
    r = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "6420", "name": "Marketing", "type": "EXPENSE"},
    )
    aid = r.json()["id"]
    r = client.patch(
        f"/api/v1/accounts/{aid}",
        headers=HEAD,
        json={"name": "Marketing & Advertising", "active": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Marketing & Advertising"
    assert body["active"] is False


def test_delete_unused_account(client):
    r = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "6420", "name": "Marketing", "type": "EXPENSE"},
    )
    aid = r.json()["id"]
    r = client.delete(f"/api/v1/accounts/{aid}", headers=HEAD)
    assert r.status_code == 204


def test_delete_account_in_use_is_blocked(client):
    # Create a business bank account and a transaction referencing 6000 (Wages).
    r = client.get("/api/v1/accounts", headers=HEAD)
    wages = next(a for a in r.json() if a["code"] == "6000")

    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    biz = r.json()[0]

    r = client.post(
        f"/api/v1/bank-accounts/{biz['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "out",
            "amount": "500.00",
            "occurred_at": "2026-05-10",
            "memo": "Casual wage",
            "account_id": wages["id"],
        },
    )
    assert r.status_code == 201, r.text

    r = client.delete(f"/api/v1/accounts/{wages['id']}", headers=HEAD)
    assert r.status_code == 409
    assert "in use" in r.json()["detail"]


def test_parent_must_match_validation(client):
    # Same type as parent — OK.
    r = client.get("/api/v1/accounts", headers=HEAD)
    sales = next(a for a in r.json() if a["code"] == "4000")

    r = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={
            "code": "4050",
            "name": "Sales — Consulting",
            "type": "INCOME",
            "parent_id": sales["id"],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["parent_id"] == sales["id"]

    # Self-parent rejected.
    aid = r.json()["id"]
    r = client.patch(
        f"/api/v1/accounts/{aid}",
        headers=HEAD,
        json={"parent_id": aid},
    )
    assert r.status_code == 400


def test_delete_account_used_by_journal_is_blocked(client):
    accounts = {a["code"]: a for a in client.get("/api/v1/accounts", headers=HEAD).json()}
    bank = accounts["1000"]
    capital = accounts["3000"]

    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Opening contribution",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "100.00"},
                {"account_id": capital["id"], "credit_amount": "100.00"},
            ],
        },
    )
    assert r.status_code == 201, r.text

    r = client.delete(f"/api/v1/accounts/{capital['id']}", headers=HEAD)
    assert r.status_code == 409
    assert "journal entries" in r.json()["detail"]


def test_delete_account_used_by_bank_rule_is_blocked(client):
    r = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "6420", "name": "Marketing", "type": "EXPENSE"},
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    r = client.post(
        "/api/v1/bank-rules",
        headers=HEAD,
        json={
            "description": "Marketing spend",
            "match_direction": "out",
            "match_memo_regex": "marketing",
            "set_account_id": aid,
            "set_tax_code": "standard",
        },
    )
    assert r.status_code == 201, r.text

    r = client.delete(f"/api/v1/accounts/{aid}", headers=HEAD)
    assert r.status_code == 409
    assert "bank rules" in r.json()["detail"]
