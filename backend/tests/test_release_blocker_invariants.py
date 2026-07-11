"""Focused regressions for the final-audit P1 accounting invariants."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

HEAD = {"X-Company-Id": "tc"}


@pytest.fixture()
def client(monkeypatch, request):
    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(test_data))
    for module in list(sys.modules):
        if module.startswith("app"):
            del sys.modules[module]

    from app.main import app

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/companies",
            json={"id": "tc", "name": "Invariant Test Pty Ltd"},
        )
        assert response.status_code == 201, response.text
        HEAD["X-Company-Generation"] = response.json()["generation_id"]
        yield test_client


@pytest.fixture()
def accounts(client):
    response = client.get("/api/v1/accounts", headers=HEAD)
    assert response.status_code == 200, response.text
    return {account["code"]: account for account in response.json()}


def _line(account_id: int) -> dict:
    return {
        "description": "Services",
        "account_id": account_id,
        "line_subtotal": "100.00",
        "line_gst": "10.00",
        "line_total": "110.00",
    }


def _invoice_payload(*, number: str, direction: str, account_id: int, currency="AUD") -> dict:
    return {
        "direction": direction,
        "contact_name": "Invariant Counterparty",
        "invoice_number": number,
        "issue_date": "2026-07-11",
        "currency": currency,
        "subtotal": "100.00",
        "gst_amount": "10.00",
        "total": "110.00",
        "lines": [_line(account_id)],
    }


def test_invoice_direction_account_invariant_on_create_update_and_post(client, accounts):
    # Create rejects the exact stale-account combinations that previously
    # produced AR expenses and AP negative income.
    ar_expense = _invoice_payload(
        number="BAD-AR", direction="AR", account_id=accounts["6100"]["id"]
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=ar_expense)
    assert response.status_code == 422, response.text
    assert "active INCOME" in response.json()["detail"]

    ap_income = _invoice_payload(
        number="BAD-AP", direction="AP", account_id=accounts["4000"]["id"]
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=ap_income)
    assert response.status_code == 422, response.text
    assert "ASSET, EXPENSE, or COST_OF_SALES" in response.json()["detail"]

    ap_control = _invoice_payload(
        number="BAD-AP-CONTROL",
        direction="AP",
        account_id=accounts["1100"]["id"],
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=ap_control)
    assert response.status_code == 422, response.text
    assert "cannot be used as an AP purchase line" in response.json()["detail"]

    asset = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "1300", "name": "Inventory", "type": "ASSET"},
    )
    assert asset.status_code == 201, asset.text
    ap_asset = _invoice_payload(
        number="GOOD-AP-ASSET",
        direction="AP",
        account_id=asset.json()["id"],
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=ap_asset)
    assert response.status_code == 201, response.text

    good = _invoice_payload(
        number="SWITCH-1", direction="AR", account_id=accounts["4000"]["id"]
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=good)
    assert response.status_code == 201, response.text
    invoice_id = response.json()["id"]

    # Direction-only PATCH cannot retain the old AR income line as an AP line.
    response = client.patch(
        f"/api/v1/invoices/{invoice_id}", headers=HEAD, json={"direction": "AP"}
    )
    assert response.status_code == 422, response.text
    persisted = client.get(f"/api/v1/invoices/{invoice_id}", headers=HEAD).json()
    assert persisted["direction"] == "AR"

    # Replacing the line with an expense in the same switch is valid.
    response = client.patch(
        f"/api/v1/invoices/{invoice_id}",
        headers=HEAD,
        json={"direction": "AP", "lines": [_line(accounts["6100"]["id"])]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["direction"] == "AP"

    # Posting repeats the invariant for legacy or externally-drifted rows.
    from app.db.company import get_company_engine

    with get_company_engine("tc").begin() as connection:
        connection.execute(
            text("UPDATE invoice_lines SET account_id = :account_id WHERE invoice_id = :invoice_id"),
            {"account_id": accounts["4000"]["id"], "invoice_id": invoice_id},
        )
    response = client.post(f"/api/v1/invoices/{invoice_id}/post", headers=HEAD)
    assert response.status_code == 422, response.text
    assert "ASSET, EXPENSE, or COST_OF_SALES" in response.json()["detail"]


def test_control_accounts_cannot_break_system_identity_via_api(client, accounts):
    expected_types = {
        "1100": "ASSET",
        "1200": "ASSET",
        "2000": "LIABILITY",
        "2100": "LIABILITY",
    }
    for code, expected_type in expected_types.items():
        account_id = accounts[code]["id"]
        wrong_type = "LIABILITY" if expected_type == "ASSET" else "ASSET"
        attempts = (
            {"code": f"{code}-MOVED"},
            {"type": wrong_type},
            {"active": False},
        )
        for payload in attempts:
            response = client.patch(
                f"/api/v1/accounts/{account_id}", headers=HEAD, json=payload
            )
            assert response.status_code == 409, (code, payload, response.text)

        response = client.delete(f"/api/v1/accounts/{account_id}", headers=HEAD)
        assert response.status_code == 409, (code, response.text)

    current = {a["code"]: a for a in client.get("/api/v1/accounts", headers=HEAD).json()}
    for code, expected_type in expected_types.items():
        assert current[code]["type"] == expected_type
        assert current[code]["active"] is True


def test_aud_only_across_company_invoice_outgoing_and_excel_writes(client, accounts):
    # Company create/update normalises harmless input but rejects non-AUD.
    response = client.post(
        "/api/v1/companies",
        json={"id": "audco", "name": "AUD Co", "base_currency": " aud "},
    )
    assert response.status_code == 201, response.text
    aud_company = response.json()
    assert aud_company["base_currency"] == "AUD"
    aud_headers = {
        "X-Company-Id": "audco",
        "X-Company-Generation": aud_company["generation_id"],
    }
    response = client.patch(
        "/api/v1/companies/audco",
        headers=aud_headers,
        json={"base_currency": "USD"},
    )
    assert response.status_code == 422, response.text
    response = client.post(
        "/api/v1/companies",
        json={"id": "usdco", "name": "USD Co", "base_currency": "USD"},
    )
    assert response.status_code == 422, response.text

    # Invoice create and update are AUD-only and persist the canonical code.
    invoice_payload = _invoice_payload(
        number="AUD-INV", direction="AR", account_id=accounts["4000"]["id"],
        currency=" aud ",
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=invoice_payload)
    assert response.status_code == 201, response.text
    invoice = response.json()
    assert invoice["currency"] == "AUD"
    response = client.patch(
        f"/api/v1/invoices/{invoice['id']}", headers=HEAD, json={"currency": "NZD"}
    )
    assert response.status_code == 422, response.text
    invoice_payload["invoice_number"] = "USD-INV"
    invoice_payload["currency"] = "USD"
    response = client.post("/api/v1/invoices", headers=HEAD, json=invoice_payload)
    assert response.status_code == 422, response.text

    # Outgoing receipts enforce the same rule before numbering/persistence.
    response = client.post(
        "/api/v1/clients", headers=HEAD, json={"display_name": "AUD Customer"}
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["id"]
    receipt_payload = {
        "issue_date": "2026-07-11",
        "client_ref_id": client_id,
        "currency": "USD",
        "lines": [{"description": "Service", "unit_price": "50.00"}],
    }
    response = client.post("/api/v1/outgoing", headers=HEAD, json=receipt_payload)
    assert response.status_code == 422, response.text
    receipt_payload["currency"] = " aud "
    response = client.post("/api/v1/outgoing", headers=HEAD, json=receipt_payload)
    assert response.status_code == 201, response.text
    assert response.json()["currency"] == "AUD"

    # The Excel importer bypasses InvoiceCreate, so it has an explicit guard:
    # reject the USD row, create and canonicalise only the AUD row.
    mapping = {
        "direction": 0,
        "contact_name": 1,
        "invoice_number": 2,
        "issue_date": 3,
        "total": 4,
        "currency": 5,
    }
    response = client.post(
        "/api/v1/invoices/import-excel-rows",
        headers=HEAD,
        json={
            "mapping": mapping,
            "rows": [
                {"row_no": 2, "raw": ["AP", "USD Supplier", "X-USD", "2026-07-11", "10", "USD"]},
                {"row_no": 3, "raw": ["AP", "AUD Supplier", "X-AUD", "2026-07-11", "10", "aud"]},
            ],
            "direction_default": "AP",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["created"]) == 1
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["row"] == 2
    assert "Only AUD" in result["skipped"][0]["reason"]
    imported = client.get(f"/api/v1/invoices/{result['created'][0]}", headers=HEAD).json()
    assert imported["currency"] == "AUD"
