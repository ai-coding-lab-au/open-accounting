"""Tests for the M2.2 trial balance, updated P&L, and balance sheet.

Strategy: drive everything through the HTTP layer so we exercise the same
path the UI will hit. Build a small consistent dataset: opening balances
via journal entries, then a couple of bank txns categorised against
income / expense, then verify each report.

Books-always-balance invariant lives in test_trial_balance_balances and
test_balance_sheet_balances.
"""

from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal

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


@pytest.fixture()
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HEAD)
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


@pytest.fixture()
def biz_bank(client):
    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    assert r.status_code == 200
    return r.json()[0]


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------


def test_trial_balance_empty_company_is_balanced(client):
    r = client.get("/api/v1/reports/trial-balance", headers=HEAD)
    assert r.status_code == 200
    body = r.json()
    assert body["is_balanced"]
    assert body["rows"] == []
    assert Decimal(body["total_debit"]) == 0
    assert Decimal(body["total_credit"]) == 0


def test_trial_balance_balances_after_journal_only(client, accounts):
    """Journal-only postings always balance (the service enforces it)."""
    bank = accounts["1000"]
    capital = accounts["3000"]
    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Opening contribution",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "10000.00"},
                {"account_id": capital["id"], "credit_amount": "10000.00"},
            ],
        },
    )
    assert r.status_code == 201

    r = client.get("/api/v1/reports/trial-balance", headers=HEAD)
    body = r.json()
    assert body["is_balanced"], body
    # Both accounts present, equal-and-opposite.
    bank_row = next(row for row in body["rows"] if row["ref_id"] == bank["id"])
    capital_row = next(row for row in body["rows"] if row["ref_id"] == capital["id"])
    assert Decimal(bank_row["debit_total"]) == Decimal("10000.00")
    assert Decimal(capital_row["credit_total"]) == Decimal("10000.00")


def test_trial_balance_balances_with_categorised_bank_txns(client, accounts, biz_bank):
    """Categorised bank txns implicitly post a balanced Dr/Cr pair."""
    sales = accounts["4000"]
    rent = accounts["6100"]

    # Money in, categorised as sales.
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "in",
            "amount": "1100.00",
            "occurred_at": "2026-05-05",
            "memo": "Consulting fee",
            "account_id": sales["id"],
        },
    )
    assert r.status_code == 201, r.text

    # Money out, categorised as rent.
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "out",
            "amount": "500.00",
            "occurred_at": "2026-05-06",
            "memo": "Office rent",
            "account_id": rent["id"],
        },
    )
    assert r.status_code == 201, r.text

    r = client.get("/api/v1/reports/trial-balance", headers=HEAD)
    body = r.json()
    assert body["is_balanced"], body
    assert Decimal(body["uncategorised_bank_in"]) == 0
    assert Decimal(body["uncategorised_bank_out"]) == 0


def test_trial_balance_uncategorised_breaks_balance(client, biz_bank):
    """An uncategorised bank txn is a half-posting; trial balance flags it."""
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "in",
            "amount": "200.00",
            "occurred_at": "2026-05-08",
            "memo": "??? need to categorise",
            # no account_id
        },
    )
    assert r.status_code == 201, r.text

    r = client.get("/api/v1/reports/trial-balance", headers=HEAD)
    body = r.json()
    assert not body["is_balanced"]
    assert Decimal(body["uncategorised_bank_in"]) == Decimal("200.00")


def test_trial_balance_as_of_excludes_later_entries(client, accounts):
    bank = accounts["1000"]
    cap = accounts["3000"]
    for d, amt in [("2026-04-01", "1000"), ("2026-05-01", "2000")]:
        client.post(
            "/api/v1/journal",
            headers=HEAD,
            json={
                "entry_date": d,
                "memo": f"open {d}",
                "lines": [
                    {"account_id": bank["id"], "debit_amount": amt},
                    {"account_id": cap["id"], "credit_amount": amt},
                ],
            },
        )
    r = client.get("/api/v1/reports/trial-balance", headers=HEAD, params={"as_of": "2026-04-30"})
    body = r.json()
    bank_row = next(row for row in body["rows"] if row["ref_id"] == bank["id"])
    assert Decimal(bank_row["debit_total"]) == Decimal("1000")  # only the April entry


# ---------------------------------------------------------------------------
# P&L now includes journal entries
# ---------------------------------------------------------------------------


def test_pnl_includes_journal_postings(client, accounts):
    sales = accounts["4000"]
    bank = accounts["1000"]
    rent = accounts["6100"]
    cap = accounts["3000"]

    # Journal-only sale.
    client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-10",
            "memo": "Manual sale accrual",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "300.00"},
                {"account_id": sales["id"], "credit_amount": "300.00"},
            ],
        },
    )
    # Journal-only expense.
    client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-11",
            "memo": "Depreciation",
            "lines": [
                {"account_id": rent["id"], "debit_amount": "100.00"},
                {"account_id": cap["id"], "credit_amount": "100.00"},
            ],
        },
    )

    r = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["total_income"]) == Decimal("300.00")
    assert Decimal(body["total_expense"]) == Decimal("100.00")
    assert Decimal(body["net_profit"]) == Decimal("200.00")


def test_pnl_journal_outside_period_excluded(client, accounts):
    sales = accounts["4000"]
    bank = accounts["1000"]
    client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-04-15",     # outside period
            "memo": "Earlier sale",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "999.00"},
                {"account_id": sales["id"], "credit_amount": "999.00"},
            ],
        },
    )
    r = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    )
    body = r.json()
    assert Decimal(body["total_income"]) == 0


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------


def test_balance_sheet_balances_after_opening_entry(client, accounts):
    bank = accounts["1000"]
    cap = accounts["3000"]
    client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Opening contribution",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "5000.00"},
                {"account_id": cap["id"], "credit_amount": "5000.00"},
            ],
        },
    )

    r = client.get(
        "/api/v1/reports/balance-sheet",
        headers=HEAD,
        params={"as_of": "2026-05-31"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_balanced"], body
    assert Decimal(body["total_assets"]) == Decimal("5000.00")
    assert (
        Decimal(body["total_liabilities"]) + Decimal(body["total_equity"])
        == Decimal("5000.00")
    )


def test_balance_sheet_balances_with_gst_on_asset_purchase(client, accounts, biz_bank):
    """Regression: a STANDARD-rated purchase with GST categorised to an ASSET
    account must keep the balance sheet balanced. The contra leg is booked
    gross, so the embedded GST has to be stripped from the asset (leaving it in
    the net-GST line only). Previously only CAPITAL-coded purchases were
    stripped, so a standard-rated asset purchase left Assets overstated by the
    GST and `is_balanced` went False.
    """
    prepay = accounts["1500"]  # Prepayments (asset)

    # $1,100 out = $1,000 prepaid asset + $100 GST input credit.
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "out",
            "amount": "1100.00",
            "occurred_at": "2024-07-05",
            "memo": "Annual insurance prepaid",
            "account_id": prepay["id"],
            "tax_code": "standard",
            "gst_amount": "100.00",
        },
    )
    assert r.status_code == 201, r.text

    bs = client.get(
        "/api/v1/reports/balance-sheet",
        headers=HEAD,
        params={"as_of": "2024-07-31"},
    )
    assert bs.status_code == 200, bs.text
    body = bs.json()
    assert body["is_balanced"], body
    assert Decimal(body["diff"]) == 0

    # The asset holds the ex-GST $1,000, not the gross $1,100.
    prepay_line = next(
        line
        for group in body["assets"]
        for line in group["lines"]
        if line.get("account_id") == prepay["id"]
    )
    assert Decimal(prepay_line["balance"]) == Decimal("1000.00")


def test_balance_sheet_with_pnl_balances(client, accounts, biz_bank):
    """Income & expense flow into retained earnings on the BS, keeping it
    balanced even when there are P&L-natured postings."""
    sales = accounts["4000"]
    rent = accounts["6100"]
    # In: $1000 sales. Out: $400 rent. Net profit $600 → equity side.
    client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "in",
            "amount": "1000.00",
            "occurred_at": "2026-05-10",
            "memo": "Sale",
            "account_id": sales["id"],
        },
    )
    client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "out",
            "amount": "400.00",
            "occurred_at": "2026-05-11",
            "memo": "Rent",
            "account_id": rent["id"],
        },
    )

    r = client.get("/api/v1/reports/balance-sheet", headers=HEAD)
    body = r.json()
    assert body["is_balanced"], body
    # Total assets should equal net cash impact (1000 - 400) = 600
    # which should appear on the equity side as retained earnings.
    assert Decimal(body["total_assets"]) == Decimal("600.00")
    assert Decimal(body["total_equity"]) == Decimal("600.00")
    assert Decimal(body["total_liabilities"]) == 0


def test_cash_basis_invoice_receipt_reports_net_income_and_no_open_ar(client, accounts, biz_bank):
    sales = accounts["4000"]

    inv = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json={
            "direction": "AR",
            "contact_name": "Acme Pty Ltd",
            "invoice_number": "SINV-2024-001",
            "issue_date": "2024-07-01",
            "due_date": "2024-07-15",
            "subtotal": "1000.00",
            "gst_amount": "100.00",
            "total": "1100.00",
            "gst_inclusive": True,
            "lines": [
                {
                    "description": "Services",
                    "account_id": sales["id"],
                    "quantity": "1",
                    "unit_price": "1000.00",
                    "gst_rate": "0.10",
                    "line_subtotal": "1000.00",
                    "line_gst": "100.00",
                    "line_total": "1100.00",
                }
            ],
        },
    )
    assert inv.status_code == 201, inv.text
    # Cash basis: the invoice stays an unposted draft (status writes via PATCH
    # are locked, and a draft can't take paid_amount — post first). Drafts are
    # excluded from open AR; the bank receipt below drives income and GST.

    receipt = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
        json={
            "direction": "in",
            "amount": "1100.00",
            "occurred_at": "2024-07-05",
            "memo": "Invoice payment SINV-2024-001",
            "account_id": sales["id"],
            "tax_code": "standard",
            "gst_amount": "100.00",
        },
    )
    assert receipt.status_code == 201, receipt.text

    pnl = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2024-07-01", "period_end": "2024-09-30"},
    )
    assert pnl.status_code == 200, pnl.text
    assert Decimal(pnl.json()["total_income"]) == Decimal("1000.00")

    bas = client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2025, "quarter": 1},
    )
    assert bas.status_code == 200, bas.text
    assert Decimal(bas.json()["one_a_gst_on_sales"]) == Decimal("100.00")

    tb = client.get(
        "/api/v1/reports/trial-balance",
        headers=HEAD,
        params={"as_of": "2024-09-30"},
    )
    assert tb.status_code == 200, tb.text
    assert tb.json()["is_balanced"], tb.json()
    assert Decimal(tb.json()["supplementary"]["ar_open_total"]) == Decimal("0.00")

    bs = client.get(
        "/api/v1/reports/balance-sheet",
        headers=HEAD,
        params={"as_of": "2024-09-30"},
    )
    assert bs.status_code == 200, bs.text
    assert bs.json()["is_balanced"], bs.json()
    ar_lines = [
        line
        for group in bs.json()["assets"]
        for line in group["lines"]
        if line["name"] == "Accounts Receivable (open invoices)"
    ]
    assert ar_lines == []


def test_bas_decimal_serialisation_always_two_decimals(client):
    r = client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2025, "quarter": 2},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["net_gst_payable"] == "0.00"
    assert body["g1_total_sales"] == "0.00"


def test_bank_statement_boundaries_and_delete_recalculate(client, biz_bank):
    def post_txn(direction, amount, occurred_at, memo):
        r = client.post(
            f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
            headers=HEAD,
            json={
                "direction": direction,
                "amount": amount,
                "occurred_at": occurred_at,
                "memo": memo,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()

    post_txn("in", "100.00", "2026-04-30", "Before period")
    may_out = post_txn("out", "25.00", "2026-05-01", "First day")
    post_txn("in", "10.00", "2026-05-31", "Last day")
    post_txn("in", "999.00", "2026-06-01", "After period")

    r = client.get(
        "/api/v1/reports/bank-statement",
        headers=HEAD,
        params={"bank_account_id": biz_bank["id"], "year": 2026, "month": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["opening_balance"]) == Decimal("100.00")
    assert Decimal(body["total_in"]) == Decimal("10.00")
    assert Decimal(body["total_out"]) == Decimal("25.00")
    assert Decimal(body["closing_balance"]) == Decimal("85.00")
    assert [row["memo"] for row in body["rows"]] == ["First day", "Last day"]

    r = client.delete(f"/api/v1/bank-accounts/transactions/{may_out['id']}", headers=HEAD)
    assert r.status_code == 204
    r = client.get(
        "/api/v1/reports/bank-statement",
        headers=HEAD,
        params={"bank_account_id": biz_bank["id"], "year": 2026, "month": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["total_out"]) == Decimal("0.00")
    assert Decimal(body["closing_balance"]) == Decimal("110.00")
    assert [row["memo"] for row in body["rows"]] == ["Last day"]
