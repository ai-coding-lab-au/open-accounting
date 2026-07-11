"""Tests for the Accounts (Chart of Accounts) CRUD endpoints."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _request_headers import manual_transaction_headers

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
        company = c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        HEAD["X-Company-Generation"] = company.json()["generation_id"]
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
        headers=manual_transaction_headers(HEAD),
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
    equity = accounts["3100"]

    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Opening contribution",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "100.00"},
                {"account_id": equity["id"], "credit_amount": "100.00"},
            ],
        },
    )
    assert r.status_code == 201, r.text

    r = client.delete(f"/api/v1/accounts/{equity['id']}", headers=HEAD)
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


@pytest.mark.parametrize(
    ("reference_source", "account_code", "new_type"),
    [
        ("bank_transaction", "6100", "INCOME"),
        ("invoice_line", "4010", "EXPENSE"),
        ("journal_line", "6500", "INCOME"),
    ],
)
def test_referenced_account_type_is_immutable_but_metadata_remains_editable(
    client, reference_source, account_code, new_type
):
    accounts = {
        account["code"]: account
        for account in client.get("/api/v1/accounts", headers=HEAD).json()
    }
    target = accounts[account_code]

    if reference_source == "bank_transaction":
        bank = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]
        response = client.post(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=manual_transaction_headers(HEAD),
            json={
                "direction": "out",
                "amount": "100.00",
                "occurred_at": "2026-05-10",
                "memo": "Historic rent",
                "account_id": target["id"],
                "tax_code": "none",
                "gst_amount": "0.00",
            },
        )
    elif reference_source == "invoice_line":
        response = client.post(
            "/api/v1/invoices",
            headers=HEAD,
            json={
                "direction": "AR",
                "contact_name": "Historic Customer",
                "invoice_number": "TYPE-LOCK-1",
                "issue_date": "2026-05-10",
                "subtotal": "100.00",
                "gst_amount": "0.00",
                "total": "100.00",
                "lines": [
                    {
                        "description": "Historic service",
                        "account_id": target["id"],
                        "quantity": "1",
                        "unit_price": "100.00",
                        "gst_rate": "0.00",
                        "line_subtotal": "100.00",
                        "line_gst": "0.00",
                        "line_total": "100.00",
                    }
                ],
            },
        )
    else:
        response = client.post(
            "/api/v1/journal",
            headers=HEAD,
            json={
                "entry_date": "2026-05-10",
                "memo": "Historic bank fee",
                "lines": [
                    {"account_id": target["id"], "debit_amount": "100.00"},
                    {
                        "account_id": accounts["3000"]["id"],
                        "credit_amount": "100.00",
                    },
                ],
            },
        )
    assert response.status_code in (200, 201), response.text

    response = client.patch(
        f"/api/v1/accounts/{target['id']}",
        headers=HEAD,
        json={"type": new_type},
    )
    assert response.status_code == 409, response.text
    assert "referenced by" in response.json()["detail"]

    response = client.patch(
        f"/api/v1/accounts/{target['id']}",
        headers=HEAD,
        json={
            "name": f"{target['name']} (renamed)",
            "description": "Updated display metadata",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["type"] == target["type"]
    assert response.json()["description"] == "Updated display metadata"


def test_opening_balance_equity_identity_is_protected(client):
    accounts = {
        account["code"]: account
        for account in client.get("/api/v1/accounts", headers=HEAD).json()
    }
    capital = accounts["3000"]

    for payload in ({"code": "3001"}, {"type": "ASSET"}, {"active": False}):
        response = client.patch(
            f"/api/v1/accounts/{capital['id']}", headers=HEAD, json=payload
        )
        assert response.status_code == 409, (payload, response.text)

    response = client.delete(f"/api/v1/accounts/{capital['id']}", headers=HEAD)
    assert response.status_code == 409, response.text

    response = client.patch(
        f"/api/v1/accounts/{capital['id']}",
        headers=HEAD,
        json={"name": "Opening Capital", "description": "Display text is editable"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["code"] == "3000"
    assert response.json()["type"] == "EQUITY"
    assert response.json()["active"] is True
    assert response.json()["name"] == "Opening Capital"


def test_missing_legacy_opening_equity_fails_closed_with_operator_error(client):
    response = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={"name": "Legacy opening bank", "opening_balance": "100.00"},
    )
    assert response.status_code == 201, response.text

    # Simulate a database damaged before the runtime protection existed.
    from app.db.company import company_session
    from app.models.company import Account

    with company_session("tc") as db:
        capital = db.query(Account).filter(Account.code == "3000").one()
        db.delete(capital)
        db.commit()

    response = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={"name": "Another opening bank", "opening_balance": "50.00"},
    )
    assert response.status_code == 409, response.text
    assert "opening-balance equity account 3000 is missing" in response.json()[
        "detail"
    ]

    for report_path in ("trial-balance", "balance-sheet"):
        response = client.get(f"/api/v1/reports/{report_path}", headers=HEAD)
        assert response.status_code == 409, (report_path, response.text)
        assert "Restore account 3000" in response.json()["detail"]
