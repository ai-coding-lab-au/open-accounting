"""Tests for the P1 report-seam fixes.

Four seams between trial balance / P&L / balance sheet / BAS, all derived
from bank transactions with (previously) different filters:

  1. BankAccount.opening_balance gets a contra equity leg (3000 Owner's
     Capital) so TB and BS balance; negative openings reverse the legs.
  2. Cross-type categorisations (IN on expense = refund, OUT on income =
     reversal) net the P&L instead of being silently dropped, so the P&L
     agrees with the trial balance and the BS stays balanced.
  3. /reports/bas reuses the gst-exposure computation, so tax_code=none and
     trust-linked txns are excluded from both, identically.
  4. PATCH .../categorise with account_id omitted keeps the current
     category; explicit account_id=null still de-categorises.
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


def _post_txn(client, bank_id, **overrides):
    payload = {
        "direction": "in",
        "amount": "100.00",
        "occurred_at": "2026-05-10",
        "memo": "test",
        "gst_amount": "0",
    }
    payload.update(overrides)
    r = client.post(
        f"/api/v1/bank-accounts/{bank_id}/transactions",
        headers=HEAD,
        json=payload,
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1) Opening balance contra equity leg
# ---------------------------------------------------------------------------


def test_opening_balance_balances_tb_and_bs_with_equity_contra(client, accounts):
    r = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={"name": "NAB savings", "opening_balance": "1000.00"},
    )
    assert r.status_code == 201, r.text

    tb = client.get("/api/v1/reports/trial-balance", headers=HEAD).json()
    assert tb["is_balanced"], tb
    assert Decimal(tb["total_debit"]) == Decimal("1000.00")
    assert Decimal(tb["total_credit"]) == Decimal("1000.00")
    capital_row = next(
        row for row in tb["rows"] if row["ref_id"] == accounts["3000"]["id"]
    )
    assert Decimal(capital_row["credit_total"]) == Decimal("1000.00")

    bs = client.get("/api/v1/reports/balance-sheet", headers=HEAD).json()
    assert bs["is_balanced"], bs
    assert Decimal(bs["total_assets"]) == Decimal("1000.00")
    equity_lines = [
        line for group in bs["equity"] for line in group["lines"]
        if line["code"] == "3000"
    ]
    assert len(equity_lines) == 1
    assert Decimal(equity_lines[0]["balance"]) == Decimal("1000.00")


def test_negative_opening_balance_balances_with_reversed_legs(client, accounts, biz_bank):
    # The create API rejects negative openings, so set one directly
    # (e.g. a migrated overdraft).
    from app.db.company import company_session
    from app.models.company import BankAccount

    with company_session("tc") as db:
        ba = db.get(BankAccount, biz_bank["id"])
        ba.opening_balance = Decimal("-500.00")
        db.commit()

    tb = client.get("/api/v1/reports/trial-balance", headers=HEAD).json()
    assert tb["is_balanced"], tb
    bank_row = next(row for row in tb["rows"] if row["kind"] == "bank")
    assert Decimal(bank_row["credit_total"]) == Decimal("500.00")
    capital_row = next(
        row for row in tb["rows"] if row["ref_id"] == accounts["3000"]["id"]
    )
    assert Decimal(capital_row["debit_total"]) == Decimal("500.00")

    bs = client.get("/api/v1/reports/balance-sheet", headers=HEAD).json()
    assert bs["is_balanced"], bs
    assert Decimal(bs["total_assets"]) == Decimal("-500.00")
    assert Decimal(bs["total_equity"]) == Decimal("-500.00")


# ---------------------------------------------------------------------------
# 2) Cross-type categorisations net the P&L
# ---------------------------------------------------------------------------


def test_expense_refund_nets_expense_down_and_bs_balances(client, accounts, biz_bank):
    rent = accounts["6100"]
    # Normal rent payment: 500 out.
    _post_txn(client, biz_bank["id"], direction="out", amount="500.00",
              account_id=rent["id"], memo="Office rent")
    # Refund from the landlord: 110 in (incl. 10 GST), categorised to rent.
    _post_txn(client, biz_bank["id"], direction="in", amount="110.00",
              gst_amount="10.00", account_id=rent["id"], memo="Rent refund")

    pnl = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()
    assert Decimal(pnl["total_expense"]) == Decimal("400.00")  # 500 - (110 - 10)

    bs = client.get("/api/v1/reports/balance-sheet", headers=HEAD).json()
    assert bs["is_balanced"], bs

    tb = client.get("/api/v1/reports/trial-balance", headers=HEAD).json()
    assert tb["is_balanced"], tb


def test_income_reversal_nets_income_down_and_bs_balances(client, accounts, biz_bank):
    sales = accounts["4000"]
    _post_txn(client, biz_bank["id"], direction="in", amount="1000.00",
              account_id=sales["id"], memo="Sale")
    # Customer refunded: 300 out, categorised to the same income account.
    _post_txn(client, biz_bank["id"], direction="out", amount="300.00",
              account_id=sales["id"], memo="Sale reversal")

    pnl = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()
    assert Decimal(pnl["total_income"]) == Decimal("700.00")
    assert Decimal(pnl["net_profit"]) == Decimal("700.00")

    bs = client.get("/api/v1/reports/balance-sheet", headers=HEAD).json()
    assert bs["is_balanced"], bs


# ---------------------------------------------------------------------------
# 3) BAS aligned with gst-exposure (tax_code=none excluded)
# ---------------------------------------------------------------------------


def test_bas_excludes_none_and_matches_gst_exposure(client, accounts, biz_bank):
    sales = accounts["4000"]
    supplies = accounts["6400"]
    # Real BAS-relevant activity (May 2026 → FY2026 Q4).
    _post_txn(client, biz_bank["id"], direction="in", amount="1100.00",
              gst_amount="100.00", tax_code="standard", account_id=sales["id"], memo="Consulting")
    _post_txn(client, biz_bank["id"], direction="out", amount="220.00",
              gst_amount="20.00", tax_code="standard", account_id=supplies["id"], memo="Stationery")
    # tax_code=none: owner draw + internal sweep — must not hit G1/purchases.
    _post_txn(client, biz_bank["id"], direction="out", amount="1000.00",
              tax_code="none", memo="Owner drawing")
    _post_txn(client, biz_bank["id"], direction="in", amount="2000.00",
              tax_code="none", memo="Sweep from savings")

    bas = client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    )
    assert bas.status_code == 200, bas.text
    body = bas.json()
    assert Decimal(body["g1_total_sales"]) == Decimal("1100.00")
    assert Decimal(body["total_purchases"]) == Decimal("220.00")
    assert Decimal(body["one_a_gst_on_sales"]) == Decimal("100.00")
    assert Decimal(body["one_b_gst_on_purchases"]) == Decimal("20.00")
    assert Decimal(body["net_gst_payable"]) == Decimal("80.00")

    exposure = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    ).json()
    assert body["g1_total_sales"] == exposure["g1_total_sales"]
    assert body["one_a_gst_on_sales"] == exposure["one_a_gst_on_sales"]
    assert body["one_b_gst_on_purchases"] == exposure["one_b_gst_on_purchases"]
    assert body["net_gst_payable"] == exposure["net_gst_payable"]


# ---------------------------------------------------------------------------
# 4) PATCH categorise: omitted vs explicit-null account_id
# ---------------------------------------------------------------------------


def test_categorise_omitted_account_id_keeps_category(client, accounts, biz_bank):
    rent = accounts["6100"]
    txn = _post_txn(client, biz_bank["id"], direction="out", amount="110.00",
                    account_id=rent["id"], memo="Rent")

    r = client.patch(
        f"/api/v1/bank-accounts/transactions/{txn['id']}/categorise",
        headers=HEAD,
        json={"tax_code": "standard", "gst_amount": "10.00"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_id"] == rent["id"]
    assert body["tax_code"] == "standard"
    assert Decimal(body["gst_amount"]) == Decimal("10.00")


def test_categorise_explicit_null_account_id_decategorises(client, accounts, biz_bank):
    rent = accounts["6100"]
    txn = _post_txn(client, biz_bank["id"], direction="out", amount="100.00",
                    account_id=rent["id"], memo="Rent")

    r = client.patch(
        f"/api/v1/bank-accounts/transactions/{txn['id']}/categorise",
        headers=HEAD,
        json={"account_id": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_id"] is None
