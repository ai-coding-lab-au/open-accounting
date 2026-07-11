from __future__ import annotations

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def api_client(monkeypatch, request):
    test_root = (PROJECT_ROOT / "tmp" / "tests").resolve()
    data_dir = (test_root / f"bank_idempotency_{request.node.name}").resolve()
    assert data_dir.parent == test_root
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("ALLOW_UNSAFE_DATA_DIR", "1")
    for module_name in list(sys.modules):
        if module_name.startswith("app"):
            del sys.modules[module_name]

    from app.main import app

    try:
        with TestClient(app) as client:
            company = client.post(
                "/api/v1/companies",
                json={
                    "id": "idem",
                    "name": "Idempotency Pty Ltd",
                    "gst_registered": True,
                },
            )
            assert company.status_code == 201, company.text
            headers = {
                "X-Company-Id": "idem",
                "X-Company-Generation": company.json()["generation_id"],
            }
            accounts = {
                row["code"]: row
                for row in client.get("/api/v1/accounts", headers=headers).json()
            }
            bank = client.get("/api/v1/bank-accounts", headers=headers).json()[0]
            yield client, headers, accounts, bank
    finally:
        from app.db.company import dispose_company_engine
        from app.db.master import master_engine

        dispose_company_engine("idem")
        master_engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)


def _posted_invoice(client, headers, accounts, number: str) -> int:
    created = client.post(
        "/api/v1/invoices",
        headers=headers,
        json={
            "direction": "AR",
            "contact_name": f"{number} Customer",
            "invoice_number": number,
            "issue_date": date.today().isoformat(),
            "subtotal": "100.00",
            "gst_amount": "10.00",
            "total": "110.00",
            "lines": [
                {
                    "description": number,
                    "account_id": accounts["4000"]["id"],
                    "line_subtotal": "100.00",
                    "line_gst": "10.00",
                    "line_total": "110.00",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]
    posted = client.post(f"/api/v1/invoices/{invoice_id}/post", headers=headers)
    assert posted.status_code == 200, posted.text
    return invoice_id


def _manual_headers(headers, key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


def _partial_receipt(bank, accounts, invoice_id: int) -> dict:
    return {
        "direction": "in",
        "amount": "40.00",
        "occurred_at": date.today().isoformat(),
        "memo": "partial receipt",
        "account_id": accounts["1100"]["id"],
        "tax_code": "none",
        "gst_amount": "0.00",
        "invoice_allocations": [
            {"invoice_id": invoice_id, "amount": "40.00"}
        ],
    }


def test_manual_create_requires_idempotency_key(api_client):
    client, headers, _, bank = api_client
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=headers,
        json={
            "direction": "in",
            "amount": "1.00",
            "occurred_at": date.today().isoformat(),
        },
    )
    assert response.status_code == 422, response.text
    assert "Idempotency-Key" in response.text


def test_same_key_same_partial_payment_replays_without_duplicate(api_client):
    client, headers, accounts, bank = api_client
    invoice_id = _posted_invoice(client, headers, accounts, "IDEM-PARTIAL")
    payload = _partial_receipt(bank, accounts, invoice_id)
    request_headers = _manual_headers(headers, "partial-retry-key")

    first = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json=payload,
    )
    second = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json=payload,
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["invoice_allocations"] == second.json()[
        "invoice_allocations"
    ]

    transactions = client.get(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=headers,
    ).json()
    assert [row["memo"] for row in transactions] == ["partial receipt"]
    invoice = client.get(
        f"/api/v1/invoices/{invoice_id}", headers=headers
    ).json()
    assert invoice["status"] == "partial"
    assert invoice["paid_amount"] == "40.00"


def test_same_key_different_payload_conflicts(api_client):
    client, headers, accounts, bank = api_client
    invoice_id = _posted_invoice(client, headers, accounts, "IDEM-CONFLICT")
    payload = _partial_receipt(bank, accounts, invoice_id)
    request_headers = _manual_headers(headers, "conflict-key")
    first = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json=payload,
    )
    assert first.status_code == 201, first.text

    conflict = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json={**payload, "memo": "changed payload"},
    )
    assert conflict.status_code == 409, conflict.text
    assert "different" in conflict.json()["detail"]


def test_allocation_order_is_canonical_for_replay(api_client):
    client, headers, accounts, bank = api_client
    first_invoice = _posted_invoice(client, headers, accounts, "IDEM-BATCH-A")
    second_invoice = _posted_invoice(client, headers, accounts, "IDEM-BATCH-B")
    allocations = [
        {"invoice_id": first_invoice, "amount": "110.00"},
        {"invoice_id": second_invoice, "amount": "110.00"},
    ]
    payload = {
        "direction": "in",
        "amount": "220.00",
        "occurred_at": date.today().isoformat(),
        "account_id": accounts["1100"]["id"],
        "invoice_allocations": allocations,
    }
    request_headers = _manual_headers(headers, "canonical-order-key")
    first = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json=payload,
    )
    replay = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json={**payload, "invoice_allocations": list(reversed(allocations))},
    )
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]


def test_concurrent_same_key_has_one_owner_and_one_transaction(api_client):
    client, headers, accounts, bank = api_client
    invoice_id = _posted_invoice(client, headers, accounts, "IDEM-CONCURRENT")
    payload = _partial_receipt(bank, accounts, invoice_id)
    request_headers = _manual_headers(headers, "concurrent-key")

    def submit():
        return client.post(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=request_headers,
            json=payload,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))
    assert [response.status_code for response in responses] == [201, 201]
    assert len({response.json()["id"] for response in responses}) == 1

    from app.db.company import company_session
    from app.models.company import (
        BankTransaction,
        BankTransactionIdempotencyKey,
    )

    with company_session("idem") as db:
        assert db.query(BankTransaction).count() == 1
        assert db.query(BankTransactionIdempotencyKey).count() == 1


def test_deleted_transaction_key_is_a_tombstone(api_client):
    client, headers, _, bank = api_client
    payload = {
        "direction": "in",
        "amount": "1.00",
        "occurred_at": date.today().isoformat(),
        "memo": "delete me",
    }
    request_headers = _manual_headers(headers, "deleted-key")
    created = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json=payload,
    )
    assert created.status_code == 201, created.text
    deleted = client.delete(
        f"/api/v1/bank-accounts/transactions/{created.json()['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    replay = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=request_headers,
        json=payload,
    )
    assert replay.status_code == 409, replay.text
    assert "deleted" in replay.json()["detail"]


def test_transaction_lists_eager_load_allocations(api_client):
    client, headers, accounts, bank = api_client
    for index in range(25):
        response = client.post(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=_manual_headers(headers, f"query-count-{index}"),
            json={
                "direction": "in",
                "amount": "1.00",
                "occurred_at": date.today().isoformat(),
                "memo": f"query-count-{index}",
            },
        )
        assert response.status_code == 201, response.text

    invoice_id = _posted_invoice(client, headers, accounts, "QUERY-COUNT")
    for index in range(5):
        response = client.post(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=_manual_headers(headers, f"query-count-allocation-{index}"),
            json={
                "direction": "in",
                "amount": "1.00",
                "occurred_at": date.today().isoformat(),
                "memo": f"query-count-allocation-{index}",
                "account_id": accounts["1100"]["id"],
                "invoice_allocations": [
                    {"invoice_id": invoice_id, "amount": "1.00"}
                ],
            },
        )
        assert response.status_code == 201, response.text

    from sqlalchemy import event

    from app.db.company import get_company_engine

    engine = get_company_engine("idem")
    counts = {"allocation_selects": 0, "component_selects": 0}

    def count_allocation_selects(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "invoice_payment_allocations" in statement
        ):
            counts["allocation_selects"] += 1
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "invoice_payment_tax_components" in statement
        ):
            counts["component_selects"] += 1

    event.listen(engine, "before_cursor_execute", count_allocation_selects)
    try:
        listed = client.get(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=headers,
            params={"limit": 30},
        )
        assert listed.status_code == 200, listed.text
        assert len(listed.json()) == 30
        assert counts["allocation_selects"] == 1
        assert counts["component_selects"] == 1

        counts["allocation_selects"] = 0
        counts["component_selects"] = 0
        uncategorised = client.get(
            "/api/v1/bank-accounts/transactions/uncategorised",
            headers=headers,
        )
        assert uncategorised.status_code == 200, uncategorised.text
        assert len(uncategorised.json()) == 25
        assert counts["allocation_selects"] == 1
        assert counts["component_selects"] == 0
    finally:
        event.remove(engine, "before_cursor_execute", count_allocation_selects)
