"""Smoke tests for the invoice flow."""

from __future__ import annotations

import io
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

# Ensure imports work whether pytest runs from repo root or backend/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


PROJECT_ROOT = ROOT.parent  # repo root


@pytest.fixture()
def client(monkeypatch, request):
    # DATA_DIR must live inside the project (sandbox rule enforced by app.config).
    # Use a unique per-test folder under the project's tmp/ area so we never touch real data.
    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        import shutil

        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app  # noqa: WPS433

    with TestClient(app) as c:
        yield c


def _make_company(client):
    r = client.post(
        "/api/v1/companies",
        json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_company_create_seeds_coa(client):
    _make_company(client)
    r = client.get("/api/v1/accounts", headers={"X-Company-Id": "tc"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 30
    codes = {row["code"] for row in rows}
    assert "1200" in codes  # GST Paid
    assert "2100" in codes  # GST Collected


def test_excel_import_round_trip(client):
    _make_company(client)
    wb = Workbook()
    ws = wb.active
    ws.append(["Supplier", "Invoice No", "Date", "Subtotal", "GST", "Total"])
    ws.append(["Acme Pty Ltd", "INV-001", "2025-08-15", 100.00, 10.00, 110.00])
    ws.append(["Bolt Co", "B-42", "2025-08-20", 200.00, 20.00, 220.00])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    r = client.post(
        "/api/v1/invoices/upload-excel",
        headers={"X-Company-Id": "tc"},
        files={"file": ("bills.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["mapping"]["contact_name"] is not None
    assert preview["mapping"]["invoice_number"] is not None
    assert preview["mapping"]["issue_date"] is not None
    assert preview["mapping"]["total"] is not None

    r = client.post(
        "/api/v1/invoices/import-excel-rows",
        headers={"X-Company-Id": "tc"},
        json={"mapping": preview["mapping"], "rows": preview["rows"], "direction_default": "AP"},
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert len(result["created"]) == 2
    assert result["skipped"] == []

    r = client.get("/api/v1/invoices?direction=AP", headers={"X-Company-Id": "tc"})
    assert r.status_code == 200
    invs = r.json()
    assert len(invs) == 2
    by_num = {i["invoice_number"]: i for i in invs}
    assert by_num["INV-001"]["status"] == "draft"
    assert by_num["INV-001"]["contact_name"] == "Acme Pty Ltd"
    assert float(by_num["INV-001"]["total"]) == 110.00


def test_excel_reimport_uses_friendly_collision_message(client):
    """Re-audit polish: re-importing the same Excel rows must surface the
    same clean `An invoice with source='excel' source_ref=... already
    exists` message as POST /invoices, not a raw IntegrityError."""
    _make_company(client)
    wb = Workbook()
    ws = wb.active
    ws.append(["Supplier", "Invoice No", "Date", "Subtotal", "GST", "Total"])
    ws.append(["Acme Pty Ltd", "INV-001", "2025-08-15", 100.00, 10.00, 110.00])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    r = client.post(
        "/api/v1/invoices/upload-excel",
        headers={"X-Company-Id": "tc"},
        files={"file": ("bills.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    preview = r.json()

    payload = {"mapping": preview["mapping"], "rows": preview["rows"], "direction_default": "AP"}

    r = client.post("/api/v1/invoices/import-excel-rows", headers={"X-Company-Id": "tc"}, json=payload)
    assert r.status_code == 200, r.text
    assert len(r.json()["created"]) == 1

    # Second import of the same rows: same content-derived source_ref
    # → skipped with the friendly message, NOT an IntegrityError dump.
    r = client.post("/api/v1/invoices/import-excel-rows", headers={"X-Company-Id": "tc"}, json=payload)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == []
    assert len(result["skipped"]) == 1
    reason = result["skipped"][0]["reason"]
    assert "source=" in reason and "source_ref=" in reason
    assert "already exists" in reason
    assert "IntegrityError" not in reason
    assert "db error" not in reason


def test_excel_import_same_row_number_different_content_is_not_skipped(client):
    _make_company(client)
    mapping = {
        "contact_name": 0,
        "invoice_number": 1,
        "issue_date": 2,
        "total": 3,
    }
    first = {
        "mapping": mapping,
        "rows": [{"row_no": 2, "raw": ["Acme Pty Ltd", "INV-JUL", "2025-07-01", "110.00"]}],
        "direction_default": "AP",
    }
    second = {
        "mapping": mapping,
        "rows": [{"row_no": 2, "raw": ["Bolt Co", "INV-AUG", "2025-08-01", "220.00"]}],
        "direction_default": "AP",
    }

    r = client.post("/api/v1/invoices/import-excel-rows", headers={"X-Company-Id": "tc"}, json=first)
    assert r.status_code == 200, r.text
    assert len(r.json()["created"]) == 1

    r = client.post("/api/v1/invoices/import-excel-rows", headers={"X-Company-Id": "tc"}, json=second)
    assert r.status_code == 200, r.text
    result = r.json()
    assert len(result["created"]) == 1
    assert result["skipped"] == []


def test_manual_invoice_create_and_status_update(client):
    _make_company(client)
    accounts = {
        a["code"]: a
        for a in client.get("/api/v1/accounts", headers={"X-Company-Id": "tc"}).json()
    }

    r = client.post(
        "/api/v1/invoices",
        headers={"X-Company-Id": "tc"},
        json={
            "direction": "AR",
            "contact_name": "Customer X",
            "invoice_number": "AR-001",
            "issue_date": "2025-08-01",
            "subtotal": "500.00",
            "gst_amount": "50.00",
            "total": "550.00",
            "lines": [
                {
                    "description": "Services",
                    "account_id": accounts["4000"]["id"],
                    "quantity": "1",
                    "unit_price": "500.00",
                    "gst_rate": "0.10",
                    "line_subtotal": "500.00",
                    "line_gst": "50.00",
                    "line_total": "550.00",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["status"] == "draft"
    inv_id = inv["id"]
    r = client.post(f"/api/v1/invoices/{inv_id}/post", headers={"X-Company-Id": "tc"})
    assert r.status_code == 200, r.text

    # Posted invoice payment status is blocked until bank clearing posts GL.
    r = client.patch(
        f"/api/v1/invoices/{inv_id}",
        headers={"X-Company-Id": "tc"},
        json={"paid_amount": "200.00"},
    )
    assert r.status_code == 409, r.text
    assert "bank clearing" in r.text

    bank = client.get("/api/v1/bank-accounts", headers={"X-Company-Id": "tc"}).json()[0]
    r = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers={"X-Company-Id": "tc"},
        json={
            "direction": "in",
            "amount": "550.00",
            "occurred_at": "2025-08-05",
            "counter_party_name": "Customer X",
            "memo": "Payment for AR-001",
            "account_id": accounts["4000"]["id"],
            "tax_code": "standard",
            "gst_amount": "50.00",
        },
    )
    assert r.status_code == 409, r.text
    assert "Accounts Receivable" in r.text

    # Correct settlement: Accounts Receivable with the STANDARD tax code — the
    # txn-level GST keeps the sale in the cash-basis BAS; the income and GST
    # liability are already on the ledger via the invoice journal.
    r = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers={"X-Company-Id": "tc"},
        json={
            "direction": "in",
            "amount": "550.00",
            "occurred_at": "2025-08-05",
            "counter_party_name": "Customer X",
            "memo": "Payment for AR-001",
            "account_id": accounts["1100"]["id"],
            "tax_code": "standard",
            "gst_amount": "50.00",
        },
    )
    assert r.status_code == 201, r.text

    # Every report agrees on the settled invoice: BAS captures the GST at
    # payment date, income is counted once, and the balance sheet shows no
    # phantom AR and no doubled GST (journal 2100 is the only GST liability).
    r = client.get(
        "/api/v1/reports/gst-exposure",
        headers={"X-Company-Id": "tc"},
        params={"period_start": "2025-08-01", "period_end": "2025-08-31"},
    )
    body = r.json()
    assert Decimal(body["g1_total_sales"]) == Decimal("550.00")
    assert Decimal(body["one_a_gst_on_sales"]) == Decimal("50.00")

    r = client.get(
        "/api/v1/reports/balance-sheet",
        headers={"X-Company-Id": "tc"},
        params={"as_of": "2025-08-31"},
    )
    body = r.json()
    assert body["is_balanced"], body
    assert Decimal(body["total_assets"]) == Decimal("550.00")  # bank only; AR cleared
    assert Decimal(body["total_liabilities"]) == Decimal("50.00")  # 2100 once, not doubled
    assert Decimal(body["total_equity"]) == Decimal("500.00")  # income counted once


def test_ap_settlement_guard_and_reports(client):
    """Mirror of the AR guard: paying a posted supplier bill must not be
    categorised to an expense account (double-counted P&L, AP never cleared);
    settling to 2000 with standard GST keeps every report correct."""
    _make_company(client)
    accounts = {
        a["code"]: a
        for a in client.get("/api/v1/accounts", headers={"X-Company-Id": "tc"}).json()
    }

    r = client.post(
        "/api/v1/invoices",
        headers={"X-Company-Id": "tc"},
        json={
            "direction": "AP",
            "contact_name": "Bolt Supplies",
            "invoice_number": "BILL-9",
            "issue_date": "2025-08-01",
            "subtotal": "200.00",
            "gst_amount": "20.00",
            "total": "220.00",
            "lines": [
                {
                    "description": "Rent",
                    "account_id": accounts["6100"]["id"],
                    "quantity": "1",
                    "unit_price": "200.00",
                    "gst_rate": "0.10",
                    "line_subtotal": "200.00",
                    "line_gst": "20.00",
                    "line_total": "220.00",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    inv_id = r.json()["id"]
    r = client.post(f"/api/v1/invoices/{inv_id}/post", headers={"X-Company-Id": "tc"})
    assert r.status_code == 200, r.text

    bank = client.get("/api/v1/bank-accounts", headers={"X-Company-Id": "tc"}).json()[0]

    # Natural-but-wrong: the payment categorised to the expense account.
    r = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers={"X-Company-Id": "tc"},
        json={
            "direction": "out",
            "amount": "220.00",
            "occurred_at": "2025-08-05",
            "counter_party_name": "Bolt Supplies",
            "memo": "Payment for BILL-9",
            "account_id": accounts["6100"]["id"],
            "tax_code": "standard",
            "gst_amount": "20.00",
        },
    )
    assert r.status_code == 409, r.text
    assert "Accounts Payable" in r.text

    # Correct settlement: 2000 with standard GST.
    r = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers={"X-Company-Id": "tc"},
        json={
            "direction": "out",
            "amount": "220.00",
            "occurred_at": "2025-08-05",
            "counter_party_name": "Bolt Supplies",
            "memo": "Payment for BILL-9",
            "account_id": accounts["2000"]["id"],
            "tax_code": "standard",
            "gst_amount": "20.00",
        },
    )
    assert r.status_code == 201, r.text

    r = client.get(
        "/api/v1/reports/gst-exposure",
        headers={"X-Company-Id": "tc"},
        params={"period_start": "2025-08-01", "period_end": "2025-08-31"},
    )
    body = r.json()
    assert Decimal(body["g11_non_capital_purchases"]) == Decimal("220.00")
    assert Decimal(body["one_b_gst_on_purchases"]) == Decimal("20.00")

    r = client.get(
        "/api/v1/reports/balance-sheet",
        headers={"X-Company-Id": "tc"},
        params={"as_of": "2025-08-31"},
    )
    body = r.json()
    assert body["is_balanced"], body
    assert Decimal(body["total_liabilities"]) == Decimal("0.00")  # AP cleared
    assert Decimal(body["total_equity"]) == Decimal("-200.00")  # expense counted once


def test_duplicate_invoice_rejected(client):
    _make_company(client)
    body = {
        "direction": "AP",
        "contact_name": "DupCo",
        "invoice_number": "X-1",
        "issue_date": "2025-08-01",
        "subtotal": "10.00",
        "gst_amount": "1.00",
        "total": "11.00",
    }
    r = client.post("/api/v1/invoices", headers={"X-Company-Id": "tc"}, json=body)
    assert r.status_code == 201
    r = client.post("/api/v1/invoices", headers={"X-Company-Id": "tc"}, json=body)
    assert r.status_code == 409


def test_duplicate_source_ref_rejected(client):
    """Audit P1: re-importing the same source row (same (source,
    source_ref)) must surface a 409 instead of silently creating a
    duplicate invoice with a different invoice_number.
    """
    _make_company(client)
    base = {
        "direction": "AP",
        "contact_name": "ImportCo",
        "issue_date": "2025-08-01",
        "subtotal": "100.00",
        "gst_amount": "10.00",
        "total": "110.00",
        "source": "pdf",
        "source_ref": "ImportCo-Aug-2025.pdf",
    }
    r = client.post(
        "/api/v1/invoices",
        headers={"X-Company-Id": "tc"},
        json={**base, "invoice_number": "INV-A"},
    )
    assert r.status_code == 201

    # Second import with same source_ref but different invoice number —
    # without the guard this used to silently create a duplicate.
    r = client.post(
        "/api/v1/invoices",
        headers={"X-Company-Id": "tc"},
        json={**base, "invoice_number": "INV-B"},
    )
    assert r.status_code == 409, r.text
    assert "source_ref" in r.text

    # Different `source` is allowed (Excel row 1 vs PDF row 1 etc.).
    r = client.post(
        "/api/v1/invoices",
        headers={"X-Company-Id": "tc"},
        json={
            **base,
            "invoice_number": "INV-C",
            "source": "excel",
        },
    )
    assert r.status_code == 201, r.text
