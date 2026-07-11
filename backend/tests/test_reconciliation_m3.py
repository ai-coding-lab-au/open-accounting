"""Tests for the new reconciliation helpers (M3):
- list uncategorised business-bank transactions
- PATCH .../categorise to recategorise / change tax_code / set gst
- GST exposure PDF endpoint produces a PDF
"""

from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal

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


@pytest.fixture()
def biz_bank(client):
    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    return r.json()[0]


@pytest.fixture()
def accounts(client):
    return {a["code"]: a for a in client.get("/api/v1/accounts", headers=HEAD).json()}


def _post_txn(client, biz_bank, **overrides):
    payload = {
        "direction": "out",
        "amount": "100.00",
        "occurred_at": "2026-05-01",
        "memo": "test",
    }
    payload.update(overrides)
    return client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json=payload,
    )


def test_uncategorised_list_only_returns_missing_account(client, biz_bank, accounts):
    rent = accounts["6100"]
    # One categorised, one not.
    _post_txn(client, biz_bank, memo="categorised", account_id=rent["id"])
    _post_txn(client, biz_bank, memo="needs review")
    r = client.get(
        "/api/v1/bank-accounts/transactions/uncategorised",
        headers=HEAD,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["memo"] == "needs review"


def test_recategorise_sets_account_and_tax(client, biz_bank, accounts):
    rent = accounts["6100"]
    r = _post_txn(client, biz_bank, memo="x")
    tid = r.json()["id"]

    r = client.patch(
        f"/api/v1/bank-accounts/transactions/{tid}/categorise",
        headers=HEAD,
        json={
            "account_id": rent["id"],
            "tax_code": "standard",
            "gst_amount": "10.00",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_id"] == rent["id"]
    assert body["tax_code"] == "standard"
    assert Decimal(body["gst_amount"]) == Decimal("10.00")


def test_recategorise_tax_code_change_revalidates_existing_gst(client, biz_bank, accounts):
    rent = accounts["6100"]
    r = _post_txn(
        client,
        biz_bank,
        account_id=rent["id"],
        tax_code="standard",
        gst_amount="10.00",
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    r = client.patch(
        f"/api/v1/bank-accounts/transactions/{tid}/categorise",
        headers=HEAD,
        json={"account_id": rent["id"], "tax_code": "gst_free"},
    )
    assert r.status_code == 400
    assert "forbids" in r.json()["detail"]


def test_recategorise_can_change_tax_code_when_gst_is_cleared(client, biz_bank, accounts):
    rent = accounts["6100"]
    r = _post_txn(
        client,
        biz_bank,
        account_id=rent["id"],
        tax_code="standard",
        gst_amount="10.00",
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    r = client.patch(
        f"/api/v1/bank-accounts/transactions/{tid}/categorise",
        headers=HEAD,
        json={
            "account_id": rent["id"],
            "tax_code": "gst_free",
            "gst_amount": "0.00",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["tax_code"] == "gst_free"
    assert Decimal(r.json()["gst_amount"]) == Decimal("0.00")


def test_recategorise_rejects_inactive_account(client, biz_bank, accounts):
    rent = accounts["6100"]
    client.patch(
        f"/api/v1/accounts/{rent['id']}",
        headers=HEAD,
        json={"active": False},
    )
    r = _post_txn(client, biz_bank)
    tid = r.json()["id"]
    r = client.patch(
        f"/api/v1/bank-accounts/transactions/{tid}/categorise",
        headers=HEAD,
        json={"account_id": rent["id"]},
    )
    assert r.status_code == 400


def test_recategorise_clears_account_when_null(client, biz_bank, accounts):
    rent = accounts["6100"]
    r = _post_txn(client, biz_bank, account_id=rent["id"])
    tid = r.json()["id"]
    r = client.patch(
        f"/api/v1/bank-accounts/transactions/{tid}/categorise",
        headers=HEAD,
        json={"account_id": None},
    )
    assert r.status_code == 200
    assert r.json()["account_id"] is None


def test_gst_exposure_pdf_endpoint_returns_pdf(client, biz_bank, accounts):
    sales = accounts["4000"]
    _post_txn(
        client, biz_bank,
        direction="in", amount="1100.00", gst_amount="100.00",
        tax_code="standard", account_id=sales["id"], memo="Sale",
    )
    r = client.get(
        "/api/v1/reports/gst-exposure/pdf",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")
