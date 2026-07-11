"""Accounting period locks are enforced by every dated mutation API."""

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
HEAD = {"X-Company-Id": "periods"}


@pytest.fixture()
def client(monkeypatch, request):
    data_dir = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    for name in list(sys.modules):
        if name.startswith("app"):
            del sys.modules[name]
    from app.main import app

    with TestClient(app) as test_client:
        created = test_client.post(
            "/api/v1/companies",
            json={"id": "periods", "name": "Period Lock Pty Ltd"},
        )
        assert created.status_code == 201, created.text
        HEAD["X-Company-Generation"] = created.json()["generation_id"]
        yield test_client


@pytest.fixture()
def accounts(client):
    return {row["code"]: row for row in client.get("/api/v1/accounts", headers=HEAD).json()}


def _lock(client, through="2026-05-31"):
    response = client.patch(
        "/api/v1/companies/periods",
        headers=HEAD,
        json={"books_locked_through": through},
    )
    assert response.status_code == 200, response.text
    assert response.json()["books_locked_through"] == through


def _draft_invoice(client, accounts, *, number="LOCK-1", issue_date="2026-05-15"):
    response = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json={
            "direction": "AR",
            "contact_name": "Locked Customer",
            "invoice_number": number,
            "issue_date": issue_date,
            "subtotal": "100.00",
            "gst_amount": "10.00",
            "total": "110.00",
            "lines": [
                {
                    "description": "Service",
                    "account_id": accounts["4000"]["id"],
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
    assert response.status_code == 201, response.text
    return response.json()


def _journal_payload(accounts, entry_date):
    return {
        "entry_date": entry_date,
        "memo": "Period lock probe",
        "lines": [
            {"account_id": accounts["1000"]["id"], "debit_amount": "10.00"},
            {"account_id": accounts["3000"]["id"], "credit_amount": "10.00"},
        ],
    }


def test_lock_is_monotonic_and_cannot_cover_the_future(client):
    _lock(client)
    cleared = client.patch(
        "/api/v1/companies/periods",
        headers=HEAD,
        json={"books_locked_through": None},
    )
    assert cleared.status_code == 409
    backwards = client.patch(
        "/api/v1/companies/periods",
        headers=HEAD,
        json={"books_locked_through": "2026-04-30"},
    )
    assert backwards.status_code == 409
    future = client.patch(
        "/api/v1/companies/periods",
        headers=HEAD,
        json={"books_locked_through": "2100-06-30"},
    )
    assert future.status_code == 422


def test_invoice_post_and_void_cannot_rewrite_locked_period(client, accounts):
    draft = _draft_invoice(client, accounts)
    posted = _draft_invoice(
        client,
        accounts,
        number="LOCK-POSTED",
        issue_date="2026-05-20",
    )
    assert client.post(
        f"/api/v1/invoices/{posted['id']}/post", headers=HEAD
    ).status_code == 200
    _lock(client)

    blocked_post = client.post(f"/api/v1/invoices/{draft['id']}/post", headers=HEAD)
    assert blocked_post.status_code == 409
    assert "locked" in str(blocked_post.json()["detail"]).lower()
    assert client.post(
        f"/api/v1/invoices/{posted['id']}/void", headers=HEAD
    ).status_code == 409
    assert client.delete(
        f"/api/v1/invoices/{posted['id']}", headers=HEAD
    ).status_code == 409

    open_invoice = _draft_invoice(
        client,
        accounts,
        number="OPEN-POST",
        issue_date="2026-06-01",
    )
    assert client.post(
        f"/api/v1/invoices/{open_invoice['id']}/post", headers=HEAD
    ).status_code == 200


def test_bank_mutations_and_import_are_blocked_by_transaction_date(client, accounts):
    bank = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]
    old = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "out",
            "amount": "11.00",
            "occurred_at": "2026-05-15",
            "memo": "Old expense",
            "account_id": accounts["6100"]["id"],
            "tax_code": "standard",
            "gst_amount": "1.00",
        },
    )
    assert old.status_code == 201, old.text
    _lock(client)

    create_old = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "out",
            "amount": "22.00",
            "occurred_at": "2026-05-16",
            "account_id": accounts["6100"]["id"],
            "tax_code": "standard",
            "gst_amount": "2.00",
        },
    )
    assert create_old.status_code == 409
    assert client.patch(
        f"/api/v1/bank-accounts/transactions/{old.json()['id']}/categorise",
        headers=HEAD,
        json={"tax_code": "gst_free", "gst_amount": "0"},
    ).status_code == 409
    assert client.delete(
        f"/api/v1/bank-accounts/transactions/{old.json()['id']}", headers=HEAD
    ).status_code == 409

    imported = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {
                    "occurred_at": "2026-05-17",
                    "direction": "out",
                    "amount": "33.00",
                    "account_id": accounts["6100"]["id"],
                    "tax_code": "standard",
                    "gst_amount": "3.00",
                    "memo": "Locked import",
                }
            ]
        },
    )
    assert imported.status_code == 409

    open_txn = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "out",
            "amount": "44.00",
            "occurred_at": "2026-06-01",
            "account_id": accounts["6100"]["id"],
            "tax_code": "standard",
            "gst_amount": "4.00",
        },
    )
    assert open_txn.status_code == 201, open_txn.text


def test_manual_journal_edit_and_delete_cannot_change_locked_period(client, accounts):
    old = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json=_journal_payload(accounts, "2026-05-10"),
    )
    assert old.status_code == 201, old.text
    _lock(client)

    assert client.post(
        "/api/v1/journal",
        headers=HEAD,
        json=_journal_payload(accounts, "2026-05-11"),
    ).status_code == 409
    assert client.patch(
        f"/api/v1/journal/{old.json()['id']}",
        headers=HEAD,
        json={"memo": "Rewrite locked history"},
    ).status_code == 409
    assert client.delete(
        f"/api/v1/journal/{old.json()['id']}", headers=HEAD
    ).status_code == 409

    open_entry = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json=_journal_payload(accounts, "2026-06-01"),
    )
    assert open_entry.status_code == 201, open_entry.text


def test_locked_company_rejects_new_nonzero_bank_opening_balance(client):
    _lock(client)
    blocked = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={"name": "Late opening", "opening_balance": "100.00"},
    )
    assert blocked.status_code == 409
    zero = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={"name": "New zero account", "opening_balance": "0.00"},
    )
    assert zero.status_code == 201, zero.text


def test_lock_rejects_unreconciled_bank_half_posting(client, accounts):
    bank = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]
    transaction = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "out",
            "amount": "11.00",
            "occurred_at": "2026-05-15",
            "memo": "Needs reconciliation before close",
        },
    )
    assert transaction.status_code == 201, transaction.text

    blocked = client.patch(
        "/api/v1/companies/periods",
        headers=HEAD,
        json={"books_locked_through": "2026-05-31"},
    )
    assert blocked.status_code == 409, blocked.text
    assert "uncategorised" in blocked.json()["detail"]
    assert client.get("/api/v1/companies/periods", headers=HEAD).json()[
        "books_locked_through"
    ] is None

    reconciled = client.patch(
        f"/api/v1/bank-accounts/transactions/{transaction.json()['id']}/categorise",
        headers=HEAD,
        json={"account_id": accounts["6100"]["id"]},
    )
    assert reconciled.status_code == 200, reconciled.text
    _lock(client)


def test_lock_rejects_unbalanced_legacy_ledger_without_500(client, accounts):
    from datetime import date

    from app.db.company import company_session
    from app.models.company import JournalEntry, JournalEntrySource, JournalLine

    # Public write APIs refuse an unbalanced journal, but a recovered/legacy
    # database may contain one. Period close must fail with an actionable 409,
    # never crash or commit the irreversible lock.
    with company_session("periods") as db:
        entry = JournalEntry(
            entry_date=date(2026, 5, 15),
            memo="Legacy one-sided entry",
            source_type=JournalEntrySource.MANUAL,
        )
        entry.lines.append(
            JournalLine(
                account_id=accounts["6100"]["id"],
                debit_amount="7.00",
                credit_amount="0.00",
            )
        )
        db.add(entry)
        db.commit()

    blocked = client.patch(
        "/api/v1/companies/periods",
        headers=HEAD,
        json={"books_locked_through": "2026-05-31"},
    )
    assert blocked.status_code == 409, blocked.text
    assert "Trial Balance is out by 7.00" in blocked.json()["detail"]
    assert client.get("/api/v1/companies/periods", headers=HEAD).json()[
        "books_locked_through"
    ] is None
