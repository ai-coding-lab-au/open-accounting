"""Adversarial accounting invariants found after the first release-gate pass."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _request_headers import manual_transaction_headers


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(monkeypatch, request):
    data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data))
    for module in list(sys.modules):
        if module.startswith("app"):
            del sys.modules[module]
    from app.main import app

    with TestClient(app) as test_client:
        created = test_client.post(
            "/api/v1/companies",
            json={"id": "r4", "name": "Round Four Pty Ltd", "gst_registered": True},
        )
        assert created.status_code == 201, created.text
        test_client.headers.update(
            {
                "X-Company-Id": "r4",
                "X-Company-Generation": created.json()["generation_id"],
            }
        )
        yield test_client


@pytest.fixture()
def accounts(client):
    return {
        account["code"]: account
        for account in client.get("/api/v1/accounts").json()
    }


@pytest.fixture()
def bank(client):
    return client.get("/api/v1/bank-accounts").json()[0]


def _invoice_payload(
    direction: str,
    number: str,
    account_id: int,
    *,
    contact: str | None = None,
) -> dict:
    return {
        "direction": direction,
        "contact_name": contact or f"{direction} {number}",
        "invoice_number": number,
        "issue_date": "2026-06-01",
        "subtotal": "100.00",
        "gst_amount": "10.00",
        "total": "110.00",
        "lines": [
            {
                "description": "Service",
                "account_id": account_id,
                "quantity": "1",
                "unit_price": "100.00",
                "gst_rate": "0.10",
                "line_subtotal": "100.00",
                "line_gst": "10.00",
                "line_total": "110.00",
            }
        ],
    }


def _create_and_post(client, payload: dict) -> dict:
    created = client.post("/api/v1/invoices", json=payload)
    assert created.status_code == 201, created.text
    invoice = created.json()
    posted = client.post(f"/api/v1/invoices/{invoice['id']}/post")
    assert posted.status_code == 200, posted.text
    return invoice


def test_post_rejects_preexisting_primary_cash_duplicate_ar_ap(
    client, accounts, bank
):
    cases = (
        ("AR", "AR-PRE-CASH", "in", "4000", "4000"),
        ("AP", "AP-PRE-CASH", "out", "6100", "6100"),
    )
    for direction, number, cash_direction, cash_code, line_code in cases:
        cash = client.post(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=manual_transaction_headers({}),
            json={
                "direction": cash_direction,
                "amount": "110.00",
                "occurred_at": "2026-06-02",
                "memo": f"Payment {number}",
                "account_id": accounts[cash_code]["id"],
                "tax_code": "standard",
                "gst_amount": "10.00",
            },
        )
        assert cash.status_code == 201, cash.text
        draft = client.post(
            "/api/v1/invoices",
            json=_invoice_payload(direction, number, accounts[line_code]["id"]),
        )
        assert draft.status_code == 201, draft.text
        posted = client.post(f"/api/v1/invoices/{draft.json()['id']}/post")
        assert posted.status_code == 409, posted.text
        assert "already categorised directly" in posted.text


def test_named_batch_residual_blocks_other_invoice_void_both_orders(
    client, accounts, bank
):
    invoice_a = _create_and_post(
        client,
        _invoice_payload("AR", "BATCH-A", accounts["4000"]["id"], contact="A Client"),
    )
    invoice_b = _create_and_post(
        client,
        _invoice_payload("AR", "BATCH-B", accounts["4000"]["id"], contact="B Client"),
    )
    payment = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers({}),
        json={
            "direction": "in",
            "amount": "220.00",
            "occurred_at": "2026-06-03",
            "memo": "Batch payment BATCH-A",
            "account_id": accounts["1100"]["id"],
            "tax_code": "standard",
            "gst_amount": "20.00",
            "invoice_allocations": [
                {"invoice_id": invoice_a["id"], "amount": "110.00"},
                {"invoice_id": invoice_b["id"], "amount": "110.00"},
            ],
        },
    )
    assert payment.status_code == 201, payment.text

    blocked = client.post(f"/api/v1/invoices/{invoice_b['id']}/void")
    assert blocked.status_code == 409, blocked.text

    transaction_id = payment.json()["id"]
    cleared = client.patch(
        f"/api/v1/bank-accounts/transactions/{transaction_id}/categorise",
        json={"account_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    voided = client.post(f"/api/v1/invoices/{invoice_b['id']}/void")
    assert voided.status_code == 200, voided.text

    reverse_blocked = client.patch(
        f"/api/v1/bank-accounts/transactions/{transaction_id}/categorise",
        json={"account_id": accounts["1100"]["id"]},
    )
    assert reverse_blocked.status_code == 409, reverse_blocked.text

    assert client.get(f"/api/v1/invoices/{invoice_a['id']}").status_code == 200


@pytest.mark.parametrize(
    "line_patch,header_patch",
    [
        ({"line_gst": "999.00", "line_total": "1099.00"}, {}),
        ({"line_gst": "20.00", "line_total": "120.00"}, {}),
        ({"quantity": "2", "unit_price": "60.00"}, {}),
        ({}, {"total": "110.01"}),
    ],
)
def test_invoice_line_math_matches_header_and_journal(
    client, accounts, line_patch, header_patch
):
    payload = _invoice_payload("AR", f"BAD-{len(str(line_patch))}", accounts["4000"]["id"])
    payload["lines"][0].update(line_patch)
    payload.update(header_patch)
    response = client.post("/api/v1/invoices", json=payload)
    assert response.status_code == 422, response.text


def test_create_paid_is_rejected_and_legacy_paid_zero_can_be_voided(client, accounts):
    payload = _invoice_payload("AR", "PAID-ZERO", accounts["4000"]["id"])
    rejected = client.post("/api/v1/invoices", json={**payload, "status": "paid"})
    assert rejected.status_code == 422, rejected.text

    invoice = _create_and_post(client, payload)
    from app.db.company import company_session
    from app.models.company import Invoice, InvoiceStatus

    with company_session("r4") as db:
        row = db.get(Invoice, invoice["id"])
        row.status = InvoiceStatus.PAID
        row.paid_amount = 0
        db.commit()

    voided = client.post(f"/api/v1/invoices/{invoice['id']}/void")
    assert voided.status_code == 200, voided.text


def test_control_settlement_rejects_capital_tax_code(client, accounts, bank):
    invoice = _create_and_post(
        client,
        _invoice_payload("AR", "AR-CAPITAL", accounts["4000"]["id"]),
    )
    rejected = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers({}),
        json={
            "direction": "in",
            "amount": "110.00",
            "occurred_at": "2026-06-03",
            "memo": "Payment AR-CAPITAL",
            "account_id": accounts["1100"]["id"],
            "tax_code": "capital",
            "gst_amount": "10.00",
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert "incompatible" in rejected.text
    assert client.get(f"/api/v1/invoices/{invoice['id']}").status_code == 200


def test_gst_free_history_blocks_global_registration_downgrade(
    client, accounts, bank
):
    created = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers({}),
        json={
            "direction": "in",
            "amount": "100.00",
            "occurred_at": "2026-06-03",
            "account_id": accounts["4000"]["id"],
            "tax_code": "gst_free",
            "gst_amount": "0",
        },
    )
    assert created.status_code == 201, created.text
    rejected = client.patch(
        "/api/v1/companies/r4", json={"gst_registered": False}
    )
    assert rejected.status_code == 409, rejected.text


def test_duplicate_invoice_error_does_not_echo_sql_or_parameters(client, accounts):
    payload = _invoice_payload("AR", "DUP-SAFE", accounts["4000"]["id"])
    assert client.post("/api/v1/invoices", json=payload).status_code == 201
    duplicate = client.post("/api/v1/invoices", json=payload)
    assert duplicate.status_code == 409, duplicate.text
    assert "INSERT INTO" not in duplicate.text
    assert "parameters" not in duplicate.text.lower()
