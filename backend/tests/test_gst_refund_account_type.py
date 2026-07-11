"""Round-3 audit P1: BAS classifies refunds by account type, not direction alone.

An IN categorised to an EXPENSE account is a purchase refund (a decreasing
adjustment to the purchases side), NOT a sale; symmetrically an OUT categorised
to an INCOME account is a sale refund, NOT a purchase. The old direction-only
classification inflated G1/1A on an expense refund — the wrong number on a
government form. These tests pin the corrected treatment and assert BAS ==
gst-exposure parity and P&L agreement on the same cross-type rows.
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
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HEAD)
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


@pytest.fixture()
def biz_bank(client):
    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    return r.json()[0]


def _txn(client, biz_bank, **kw):
    payload = {"occurred_at": "2026-05-10", "memo": "t", "gst_amount": "0"}
    payload.update(kw)
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json=payload,
    )
    assert r.status_code == 201, r.text
    return r


def _gst(client):
    return client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()


def _bas(client):
    return client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    ).json()


def _pnl(client):
    return client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()


def test_expense_refund_reduces_purchases_not_inflates_sales(client, accounts, biz_bank):
    """The exact round-3 probe: OUT 550 (GST 50) to rent + refund IN 220 (GST 20)
    to the SAME expense account. BAS must net the purchase side down, NOT show
    G1=220/1A=20.
    """
    rent = accounts["6100"]  # EXPENSE
    _txn(client, biz_bank, direction="out", amount="550.00", gst_amount="50.00",
         tax_code="standard", account_id=rent["id"])
    _txn(client, biz_bank, direction="in", amount="220.00", gst_amount="20.00",
         tax_code="standard", account_id=rent["id"])

    g = _gst(client)
    # Sales side untouched by the refund.
    assert Decimal(g["g1_total_sales"]) == Decimal("0.00")
    assert Decimal(g["one_a_gst_on_sales"]) == Decimal("0.00")
    # Purchases side: 550 - 220 = 330; GST 50 - 20 = 30.
    assert Decimal(g["g11_non_capital_purchases"]) == Decimal("330.00")
    assert Decimal(g["one_b_gst_on_purchases"]) == Decimal("30.00")
    assert Decimal(g["net_gst_payable"]) == Decimal("-30.00")  # refund owed to firm
    # total_purchases is exposed on the BAS view.
    assert Decimal(_bas(client)["total_purchases"]) == Decimal("330.00")


def test_expense_refund_bas_matches_gst_exposure(client, accounts, biz_bank):
    """Round-1 parity must survive the fix: BAS delegates to gst_exposure."""
    rent = accounts["6100"]
    _txn(client, biz_bank, direction="out", amount="550.00", gst_amount="50.00",
         tax_code="standard", account_id=rent["id"])
    _txn(client, biz_bank, direction="in", amount="220.00", gst_amount="20.00",
         tax_code="standard", account_id=rent["id"])

    g = _gst(client)
    b = _bas(client)
    # BAS delegates to gst_exposure_for_quarter, so every shared box must match.
    assert Decimal(b["g1_total_sales"]) == Decimal(g["g1_total_sales"]) == Decimal("0.00")
    assert Decimal(b["one_a_gst_on_sales"]) == Decimal(g["one_a_gst_on_sales"]) == Decimal("0.00")
    assert Decimal(b["one_b_gst_on_purchases"]) == Decimal(g["one_b_gst_on_purchases"]) == Decimal("30.00")
    assert Decimal(b["net_gst_payable"]) == Decimal(g["net_gst_payable"]) == Decimal("-30.00")
    assert Decimal(b["total_purchases"]) == Decimal("330.00")


def test_expense_refund_pnl_agrees(client, accounts, biz_bank):
    """P&L nets the expense to -200 (550 - 50 GST out, less 220 - 20 GST refund)."""
    rent = accounts["6100"]
    _txn(client, biz_bank, direction="out", amount="550.00", gst_amount="50.00",
         tax_code="standard", account_id=rent["id"])
    _txn(client, biz_bank, direction="in", amount="220.00", gst_amount="20.00",
         tax_code="standard", account_id=rent["id"])

    p = _pnl(client)
    rent_row = next(row for row in p["expense_rows"] if row["code"] == "6100")
    # 500 expense net less 200 refund net = 300 cost (GST-exclusive).
    assert Decimal(rent_row["total"]) == Decimal("300.00")
    assert Decimal(p["total_expense"]) == Decimal("300.00")


def test_sale_refund_reduces_sales_not_inflates_purchases(client, accounts, biz_bank):
    """Symmetric hole: OUT categorised to an INCOME account is a sale refund."""
    sales = accounts["4000"]  # INCOME
    _txn(client, biz_bank, direction="in", amount="1100.00", gst_amount="100.00",
         tax_code="standard", account_id=sales["id"])
    _txn(client, biz_bank, direction="out", amount="330.00", gst_amount="30.00",
         tax_code="standard", account_id=sales["id"])

    g = _gst(client)
    # Sales side nets down; purchase side untouched.
    assert Decimal(g["g1_total_sales"]) == Decimal("770.00")     # 1100 - 330
    assert Decimal(g["one_a_gst_on_sales"]) == Decimal("70.00")  # 100 - 30
    assert Decimal(g["g11_non_capital_purchases"]) == Decimal("0.00")
    assert Decimal(g["one_b_gst_on_purchases"]) == Decimal("0.00")
    assert Decimal(g["net_gst_payable"]) == Decimal("70.00")
    assert Decimal(_bas(client)["total_purchases"]) == Decimal("0.00")


def test_uncategorised_is_excluded_from_gst_boxes_and_counted(client, biz_bank):
    """No account_id means the transaction still needs categorising before BAS."""
    _txn(client, biz_bank, direction="in", amount="1100.00", gst_amount="100.00",
         tax_code="standard")
    _txn(client, biz_bank, direction="out", amount="220.00", gst_amount="20.00",
         tax_code="standard")
    g = _gst(client)
    assert Decimal(g["g1_total_sales"]) == Decimal("0.00")
    assert Decimal(g["one_a_gst_on_sales"]) == Decimal("0.00")
    assert Decimal(g["g11_non_capital_purchases"]) == Decimal("0.00")
    assert Decimal(g["one_b_gst_on_purchases"]) == Decimal("0.00")
    assert Decimal(g["net_gst_payable"]) == Decimal("0.00")
    assert g["uncategorised_count"] == 2

    b = _bas(client)
    assert Decimal(b["g1_total_sales"]) == Decimal("0.00")
    assert Decimal(b["total_purchases"]) == Decimal("0.00")
    assert b["uncategorised_count"] == 2
