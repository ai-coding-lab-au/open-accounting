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
        headers=HEAD,
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
    assert Decimal(body["g11_non_capital_purchases"]) == Decimal("220.00")
    assert Decimal(body["g14_gst_free_purchases"]) == Decimal("300.00")
    assert Decimal(body["one_b_gst_on_purchases"]) == Decimal("520.00")  # 500 capital + 20 standard

    assert Decimal(body["net_gst_payable"]) == Decimal("-420.00")  # owed refund


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
