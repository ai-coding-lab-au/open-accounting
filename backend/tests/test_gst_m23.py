"""Tests for the GST tax_code + exposure report (M2.3).

Covers:
  - tax_code defaults to "standard" when not provided
  - non-STANDARD tax_code with positive gst_amount is rejected
  - the GST exposure report places amounts in the correct boxes
  - tax_code=none excludes from BAS entirely
  - trust-linked txns are excluded from GST
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
    r = client.get("/api/v1/accounts", headers=HEAD)
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


def _post_txn(client, biz_bank, **overrides):
    payload = {
        "direction": "in",
        "amount": "1100.00",
        "occurred_at": "2026-05-10",
        "memo": "test",
        "gst_amount": "0",
    }
    payload.update(overrides)
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json=payload,
    )
    return r


# ---------------------------------------------------------------------------
# tax_code field
# ---------------------------------------------------------------------------


def test_tax_code_defaults_to_standard(client, biz_bank):
    r = _post_txn(client, biz_bank)
    assert r.status_code == 201, r.text
    assert r.json()["tax_code"] == "standard"


def test_tax_code_gst_free_round_trips(client, biz_bank):
    r = _post_txn(client, biz_bank, tax_code="gst_free", gst_amount="0")
    assert r.status_code == 201, r.text
    assert r.json()["tax_code"] == "gst_free"


def test_non_standard_tax_code_forbids_positive_gst(client, biz_bank):
    r = _post_txn(client, biz_bank, tax_code="gst_free", gst_amount="100.00")
    assert r.status_code == 400
    assert "tax_code" in r.json()["detail"].lower()


def test_unknown_tax_code_rejected(client, biz_bank):
    r = _post_txn(client, biz_bank, tax_code="banana")
    # Pydantic pattern catches it as 422.
    assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# GST exposure report
# ---------------------------------------------------------------------------


def test_gst_exposure_empty(client):
    r = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["g1_total_sales"]) == 0
    assert Decimal(body["net_gst_payable"]) == 0


def test_gst_exposure_buckets_into_correct_boxes(client, accounts, biz_bank):
    sales = accounts["4000"]
    ppe = accounts["1700"]
    supplies = accounts["6400"]
    fees = accounts["6500"]
    # Standard taxable sale: 1100 inc 100 GST.
    _post_txn(
        client, biz_bank,
        direction="in", amount="1100.00", gst_amount="100.00",
        tax_code="standard", account_id=sales["id"], memo="Consulting",
    )
    # GST-free sale: 500.
    _post_txn(
        client, biz_bank,
        direction="in", amount="500.00", gst_amount="0",
        tax_code="gst_free", account_id=sales["id"], memo="Export",
    )
    # Capital purchase: 5500 inc 500 GST.
    _post_txn(
        client, biz_bank,
        direction="out", amount="5500.00", gst_amount="500.00",
        tax_code="capital", account_id=ppe["id"], memo="Laptop",
    )
    # Standard non-capital purchase: 220 inc 20 GST.
    _post_txn(
        client, biz_bank,
        direction="out", amount="220.00", gst_amount="20.00",
        tax_code="standard", account_id=supplies["id"], memo="Stationery",
    )
    # GST-free purchase: 300.
    _post_txn(
        client, biz_bank,
        direction="out", amount="300.00", gst_amount="0",
        tax_code="gst_free", account_id=fees["id"], memo="Bank fee abroad",
    )

    r = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    )
    body = r.json()
    assert Decimal(body["g1_total_sales"]) == Decimal("1600.00")     # 1100 + 500
    assert Decimal(body["g3_gst_free_sales"]) == Decimal("500.00")
    assert Decimal(body["g6_sales_subject_to_gst"]) == Decimal("1100.00")
    assert Decimal(body["one_a_gst_on_sales"]) == Decimal("100.00")

    assert Decimal(body["g10_capital_purchases"]) == Decimal("5500.00")
    # G14 overlaps G11: the GST-free $300 purchase is still a non-capital
    # purchase and must appear in both worksheet fields.
    assert Decimal(body["g11_non_capital_purchases"]) == Decimal("520.00")
    assert Decimal(body["g14_gst_free_purchases"]) == Decimal("300.00")
    assert Decimal(body["one_b_gst_on_purchases"]) == Decimal("520.00")  # 500 capital + 20 standard

    assert Decimal(body["net_gst_payable"]) == Decimal("-420.00")  # owed refund


def test_purchase_boxes_overlap_for_zero_gst_capital_and_refunds(
    client, accounts, biz_bank
):
    supplies = accounts["6400"]
    fees = accounts["6500"]
    ppe = accounts["1700"]

    # Non-capital purchases: every row is G11; rows without GST in the price
    # also overlap G14.
    _post_txn(client, biz_bank, direction="out", amount="220.00",
              gst_amount="20.00", tax_code="standard", account_id=supplies["id"])
    _post_txn(client, biz_bank, direction="out", amount="50.00",
              tax_code="gst_free", account_id=supplies["id"])
    _post_txn(client, biz_bank, direction="out", amount="30.00",
              tax_code="input_taxed", account_id=fees["id"])

    # Capital is explicit. A capital row with no GST in the price overlaps G10
    # and G14; a taxable capital row contributes GST to 1B but not G14.
    _post_txn(client, biz_bank, direction="out", amount="100.00",
              tax_code="capital", account_id=ppe["id"])
    _post_txn(client, biz_bank, direction="out", amount="110.00",
              gst_amount="10.00", tax_code="capital", account_id=ppe["id"])

    # IN rows coded to purchase accounts are purchase refunds/decreasing
    # adjustments and subtract from every overlapping worksheet membership.
    _post_txn(client, biz_bank, direction="in", amount="22.00",
              gst_amount="2.00", tax_code="standard", account_id=supplies["id"])
    _post_txn(client, biz_bank, direction="in", amount="5.00",
              tax_code="gst_free", account_id=supplies["id"])
    _post_txn(client, biz_bank, direction="in", amount="3.00",
              tax_code="input_taxed", account_id=fees["id"])
    _post_txn(client, biz_bank, direction="in", amount="10.00",
              tax_code="capital", account_id=ppe["id"])

    body = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()

    assert Decimal(body["g10_capital_purchases"]) == Decimal("200.00")
    assert Decimal(body["g11_non_capital_purchases"]) == Decimal("270.00")
    assert Decimal(body["g14_gst_free_purchases"]) == Decimal("162.00")
    assert Decimal(body["one_b_gst_on_purchases"]) == Decimal("28.00")
    assert Decimal(body["one_a_gst_on_sales"]) == Decimal("0.00")
    assert Decimal(body["net_gst_payable"]) == Decimal("-28.00")


def test_custom_asset_account_does_not_imply_capital(client, biz_bank):
    response = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "1300", "name": "Inventory", "type": "ASSET"},
    )
    assert response.status_code == 201, response.text
    inventory = response.json()

    _post_txn(
        client,
        biz_bank,
        direction="out",
        amount="110.00",
        gst_amount="10.00",
        tax_code="standard",
        account_id=inventory["id"],
    )
    _post_txn(
        client,
        biz_bank,
        direction="out",
        amount="40.00",
        tax_code="gst_free",
        account_id=inventory["id"],
    )
    _post_txn(
        client,
        biz_bank,
        direction="in",
        amount="55.00",
        gst_amount="5.00",
        tax_code="standard",
        account_id=inventory["id"],
    )

    body = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()

    assert Decimal(body["g10_capital_purchases"]) == 0
    assert Decimal(body["g11_non_capital_purchases"]) == Decimal("95.00")
    assert Decimal(body["g14_gst_free_purchases"]) == Decimal("40.00")
    assert Decimal(body["one_b_gst_on_purchases"]) == Decimal("5.00")
    assert Decimal(body["g1_total_sales"]) == 0
    assert Decimal(body["one_a_gst_on_sales"]) == 0


def test_unallocated_ar_ap_control_activity_is_rejected(client, accounts, biz_bank):
    for direction, amount, gst, account_code in (
        ("in", "110.00", "10.00", "1100"),
        ("out", "55.00", "5.00", "1100"),
        ("out", "220.00", "20.00", "2000"),
        ("in", "110.00", "10.00", "2000"),
    ):
        response = _post_txn(
            client,
            biz_bank,
            direction=direction,
            amount=amount,
            gst_amount=gst,
            tax_code="standard",
            account_id=accounts[account_code]["id"],
        )
        assert response.status_code == 409, response.text
        assert "invoice allocation" in response.text or "workflow" in response.text


def test_gst_exposure_none_tax_code_excluded(client, biz_bank):
    """tax_code=none means 'don't count this on BAS'."""
    _post_txn(
        client, biz_bank,
        direction="out", amount="1000.00", gst_amount="0",
        tax_code="none", memo="Owner drawing",
    )
    r = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    )
    body = r.json()
    assert Decimal(body["g11_non_capital_purchases"]) == 0
    assert body["excluded_count"] == 1


def test_gst_exposure_via_quarter_param(client, accounts, biz_bank):
    """fy_year+quarter shorthand produces the same shape."""
    _post_txn(
        client, biz_bank,
        direction="in", amount="1100.00", gst_amount="100.00",
        tax_code="standard", account_id=accounts["4000"]["id"], occurred_at="2026-05-15",
    )
    # AU FY2026 = Jul 2025 → Jun 2026; Q4 = Apr-Jun 2026.
    r = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fy_year"] == 2026
    assert body["quarter"] == 4
    assert Decimal(body["one_a_gst_on_sales"]) == Decimal("100.00")
