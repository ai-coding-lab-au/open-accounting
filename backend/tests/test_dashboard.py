"""Tests for the dashboard summary endpoint."""

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


def test_empty_company_dashboard(client):
    r = client.get("/api/v1/dashboard/summary", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert float(body["business_total"]) == 0.0
    assert float(body["unpaid_ap_total"]) == 0.0
    assert isinstance(body["bank_accounts"], list)
    assert len(body["bank_accounts"]) == 1


def test_dashboard_reflects_business_txn(client):
    # Find Sales — Services
    r = client.get("/api/v1/accounts", headers=HEAD)
    sales = next(a for a in r.json() if a["code"] == "4000")

    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    biz = r.json()[0]

    # Book the txn today so it always falls inside the current FY, whatever
    # the test-run date (a fixed date breaks every July 1 when the AU FY rolls).
    from datetime import date

    client.post(
        f"/api/v1/bank-accounts/{biz['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "1000.00",
            "occurred_at": date.today().isoformat(),
            "memo": "Client payment",
            "account_id": sales["id"],
        },
    )

    r = client.get("/api/v1/dashboard/summary", headers=HEAD)
    body = r.json()
    assert float(body["business_total"]) >= 1000.0
    # FY income (booked today, so always inside the current FY)
    assert float(body["fy_total_income"]) >= 1000.0
    # Recent txns should include this one
    descs = [t["memo"] for t in body["recent_business_txns"]]
    assert "Client payment" in descs


def test_dashboard_bank_balance_includes_uncategorised_transactions(client):
    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    biz = r.json()[0]

    from datetime import date

    client.post(
        f"/api/v1/bank-accounts/{biz['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "42.00",
            "occurred_at": date.today().isoformat(),
            "memo": "Unmatched deposit",
            "account_id": None,
        },
    )

    dashboard = client.get("/api/v1/dashboard/summary", headers=HEAD).json()
    bank_accounts = client.get("/api/v1/bank-accounts", headers=HEAD).json()

    assert dashboard["business_total"] == "42.00"
    assert dashboard["bank_accounts"][0]["balance"] == "42.00"
    assert bank_accounts[0]["current_balance"] == "42.00"
    assert dashboard["tb_uncategorised_in"] == "42.00"


def test_dashboard_includes_unpaid_ap(client):
    # Create a contact and an unpaid AP invoice.
    r = client.post(
        "/api/v1/contacts",
        headers=HEAD,
        json={"name": "ACME Supplier", "kind": "supplier"},
    )
    cid = r.json()["id"]
    accounts = {
        a["code"]: a
        for a in client.get("/api/v1/accounts", headers=HEAD).json()
    }
    r = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json={
            "direction": "AP",
            "contact_id": cid,
            "invoice_number": "ACME-001",
            "issue_date": "2026-05-01",
            "due_date": "2026-05-15",
            "subtotal": "100.00",
            "gst_amount": "10.00",
            "total": "110.00",
            "status": "unpaid",
            "lines": [
                {
                    "description": "Supplies",
                    "account_id": accounts["6400"]["id"],
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

    r = client.get("/api/v1/dashboard/summary", headers=HEAD)
    body = r.json()
    assert float(body["unpaid_ap_total"]) == 110.0
    assert any(inv["invoice_number"] == "ACME-001" for inv in body["unpaid_ap"])


def test_dashboard_summary_after_authorising_ap_invoice(client):
    """Repro for BUG-R10: authorise a manual AP invoice, then load the
    dashboard. The summary must return 200 (it counts AUTHORISED AP as
    outstanding) — it must not 503 / hang."""
    # An expense account to code the AP line to.
    r = client.get("/api/v1/accounts", headers=HEAD)
    assert r.status_code == 200, r.text
    expense = next(a for a in r.json() if a["code"] == "6100")

    # Create a manual AP invoice with one coded line (mirrors the UI).
    body = {
        "direction": "AP",
        "contact_name": "R10 Supplier Co",
        "invoice_number": "R10-AP-001",
        "issue_date": "2026-06-01",
        "subtotal": "1000.00",
        "gst_amount": "100.00",
        "total": "1100.00",
        "gst_inclusive": True,
        "source": "manual",
        "lines": [
            {
                "description": "Rent",
                "account_id": expense["id"],
                "line_subtotal": "1000.00",
                "line_gst": "100.00",
                "line_total": "1100.00",
            }
        ],
    }
    r = client.post("/api/v1/invoices", headers=HEAD, json=body)
    assert r.status_code == 201, r.text
    inv_id = r.json()["id"]

    # Authorise (post to ledger).
    r = client.post(f"/api/v1/invoices/{inv_id}/post", headers=HEAD)
    assert r.status_code == 200, r.text
    assert r.json()["invoice"]["status"] == "authorised"

    # The failing step in R10: dashboard summary right after authorising.
    r = client.get("/api/v1/dashboard/summary", headers=HEAD)
    assert r.status_code == 200, r.text
    summary = r.json()
    # Authorised-but-unpaid AP must be counted as outstanding.
    assert float(summary["unpaid_ap_total"]) == 1100.0, summary


def test_current_month_bounds_is_month_to_date():
    """Current-month income is month-TO-DATE: the period ends at today, not the
    last day of the month, so future-dated entries don't inflate it."""
    from datetime import date

    from app.services.dashboard import _current_month_bounds

    start, end = _current_month_bounds(date(2026, 6, 3))
    assert start == date(2026, 6, 1)
    assert end == date(2026, 6, 3)  # not 2026-06-30
