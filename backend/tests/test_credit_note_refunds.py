from __future__ import annotations

import shutil
import sys
from decimal import Decimal
from pathlib import Path

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
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(test_data))
    for module in list(sys.modules):
        if module.startswith("app"):
            del sys.modules[module]
    global company_session
    from app.db.company import company_session
    from app.main import app
    from app.models import company as company_models

    global BankAccount, BankTransaction, CreditNote, CreditNoteApplication
    global CreditNoteRefund, JournalEntry, JournalEntrySource
    BankAccount = company_models.BankAccount
    BankTransaction = company_models.BankTransaction
    CreditNote = company_models.CreditNote
    CreditNoteApplication = company_models.CreditNoteApplication
    CreditNoteRefund = company_models.CreditNoteRefund
    JournalEntry = company_models.JournalEntry
    JournalEntrySource = company_models.JournalEntrySource

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/companies",
            json={
                "id": "tc",
                "marn": "1234567",
                "registered_agent_name": "Fictional Test Agent",
                "name": "Fictional Test Pty Ltd",
            },
        )
        assert response.status_code == 201, response.text
        HEAD["X-Company-Generation"] = response.json()["generation_id"]
        yield test_client


@pytest.fixture()
def accounts(client):
    response = client.get("/api/v1/accounts", headers=HEAD)
    assert response.status_code == 200, response.text
    return {account["code"]: account for account in response.json()}


HEAD = {"X-Company-Id": "tc"}


def _source_line(accounts, *, direction, account_code, tax_code="standard"):
    if direction == "AR":
        subtotal, gst, total, gst_rate = "200.00", "20.00", "220.00", "0.1000"
        account = account_code
    else:
        subtotal, gst, total, gst_rate = "200.00", "20.00", "220.00", "0.1000"
        account = account_code
    return {
        "description": f"Fictional {direction} refund source",
        "account_id": accounts[account]["id"],
        "quantity": "2",
        "unit_price": "100.00",
        "line_subtotal": subtotal,
        "line_gst": gst,
        "line_total": total,
        "tax_code": tax_code,
        "gst_rate": gst_rate,
    }


def _create_source(client, accounts, *, direction, number):
    source = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json={
            "direction": direction,
            "contact_name": "Fictional Refund Customer" if direction == "AR" else "Fictional Refund Supplier",
            "invoice_number": number,
            "issue_date": "2026-05-31",
            "subtotal": "200.00",
            "gst_amount": "20.00",
            "total": "220.00",
            "gst_inclusive": False,
            "amount_mode": "exclusive",
            "lines": [
                _source_line(accounts, direction=direction, account_code="4000" if direction == "AR" else "6100")
            ],
        },
    )
    assert source.status_code == 201, source.text
    posted = client.post(f"/api/v1/invoices/{source.json()['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    posted_invoice = posted.json()["invoice"]
    snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{posted_invoice['id']}",
        headers=HEAD,
    )
    assert snapshot.status_code == 200, snapshot.text
    return {**posted_invoice, "lines": snapshot.json()["lines"]}


def _create_credit_note(client, source, *, direction, number):
    created = client.post(
        "/api/v1/credit-notes",
        headers=HEAD,
        json={
            "source_invoice_id": source["id"],
            "credit_note_number": number,
            "issue_date": "2026-06-01",
            "lines": [
                {
                    "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
                    "quantity": "2.0000",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    posted = client.post(
        f"/api/v1/credit-notes/{created.json()['id']}/post",
        headers=HEAD,
    )
    assert posted.status_code == 200, posted.text
    return created.json(), posted.json()


def _bank_accounts(client):
    accounts = client.get("/api/v1/bank-accounts", headers=HEAD).json()
    assert len(accounts) >= 1
    return accounts


def _create_second_bank_account(client, accounts):
    ledger = client.post(
        "/api/v1/accounts",
        headers=HEAD,
        json={"code": "1001", "name": "Refund Cash", "type": "ASSET", "is_gst": False},
    )
    assert ledger.status_code == 201, ledger.text
    bank = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={"name": "Refund Bank Account", "ledger_account_id": ledger.json()["id"]},
    )
    assert bank.status_code == 201, bank.text
    return bank.json()


def _refund_payload(bank_account_id, amount, refund_date):
    return {
        "bank_account_id": bank_account_id,
        "amount": amount,
        "refund_date": refund_date,
    }


def _journal_lines(client, source_type, source_id):
    entries = client.get("/api/v1/journal/entries", headers=HEAD).json()
    matching = [
        entry
        for entry in entries
        if entry["source_type"] == source_type and entry["source_id"] == source_id
    ]
    assert len(matching) == 1
    return matching[0]


def test_ar_and_ap_partial_refunds_and_reversals(client, accounts):
    ar_source = _create_source(client, accounts, direction="AR", number="REFUND-AR-SOURCE")
    ap_source = _create_source(client, accounts, direction="AP", number="REFUND-AP-SOURCE")
    ar_note, _ = _create_credit_note(client, ar_source, direction="AR", number="REFUND-AR-CN")
    ap_note, _ = _create_credit_note(client, ap_source, direction="AP", number="REFUND-AP-CN")
    bank_accounts = _bank_accounts(client)
    bank_account = _create_second_bank_account(client, bank_accounts)
    assert bank_account["id"] != bank_accounts[0]["id"]

    ar_refund_one = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "ar-refund-100"},
        json=_refund_payload(bank_account["id"], "100.00", "2026-06-15"),
    )
    assert ar_refund_one.status_code == 201, ar_refund_one.text
    ar_refund_one_json = ar_refund_one.json()
    assert Decimal(ar_refund_one_json["amount"]) == Decimal("100.00")
    assert ar_refund_one_json["bank_account_id"] == bank_account["id"]

    ar_refund_two = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "ar-refund-50"},
        json=_refund_payload(bank_account["id"], "50.00", "2026-06-16"),
    )
    assert ar_refund_two.status_code == 201, ar_refund_two.text

    replay = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "ar-refund-100"},
        json=_refund_payload(bank_account["id"], "100.00", "2026-06-15"),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == ar_refund_one_json["id"]

    different_bank = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "ar-refund-100"},
        json=_refund_payload(bank_accounts[0]["id"], "100.00", "2026-06-15"),
    )
    assert different_bank.status_code == 409, different_bank.text

    conflict = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "ar-refund-100"},
        json=_refund_payload(bank_account["id"], "101.00", "2026-06-15"),
    )
    assert conflict.status_code == 409, conflict.text

    over_refund = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "ar-over-refund"},
        json=_refund_payload(bank_account["id"], "71.00", "2026-06-17"),
    )
    assert over_refund.status_code == 409, over_refund.text

    ap_refund = client.post(
        f"/api/v1/credit-notes/{ap_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "ap-refund-70"},
        json=_refund_payload(bank_account["id"], "70.00", "2026-06-18"),
    )
    assert ap_refund.status_code == 201, ap_refund.text

    note_after = client.get(f"/api/v1/credit-notes/{ar_note['id']}", headers=HEAD).json()
    assert Decimal(note_after["refunded_amount"]) == Decimal("150.00")
    assert Decimal(note_after["remaining_amount"]) == Decimal("70.00")
    assert len(note_after["refunds"]) == 2
    assert {refund["status"] for refund in note_after["refunds"]} == {"active"}

    ap_note_after = client.get(f"/api/v1/credit-notes/{ap_note['id']}", headers=HEAD).json()
    assert Decimal(ap_note_after["refunded_amount"]) == Decimal("70.00")
    assert Decimal(ap_note_after["remaining_amount"]) == Decimal("150.00")

    ar_reversal = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds/{ar_refund_two.json()['id']}/reverse",
        headers=HEAD,
        json={"reversal_date": "2026-06-20"},
    )
    assert ar_reversal.status_code == 200, ar_reversal.text
    assert ar_reversal.json()["status"] == "reversed"
    assert ar_reversal.json()["reversal_date"] == "2026-06-20"
    assert ar_reversal.json()["bank_account_id"] == bank_account["id"]
    assert ar_reversal.json()["reversal_bank_transaction_id"] is not None

    duplicate_reversal = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/refunds/{ar_refund_two.json()['id']}/reverse",
        headers=HEAD,
        json={"reversal_date": "2026-06-21"},
    )
    assert duplicate_reversal.status_code == 409, duplicate_reversal.text

    note_after_reversal = client.get(f"/api/v1/credit-notes/{ar_note['id']}", headers=HEAD).json()
    assert Decimal(note_after_reversal["refunded_amount"]) == Decimal("100.00")
    assert Decimal(note_after_reversal["remaining_amount"]) == Decimal("120.00")
    assert [refund["status"] for refund in note_after_reversal["refunds"]] == ["active", "reversed"]

    with company_session("tc") as db:
        original_refund = db.get(CreditNoteRefund, ar_refund_two.json()["id"])
        assert original_refund is not None
        assert original_refund.status == "reversed"
        assert original_refund.reversal_journal_entry_id is not None
        assert original_refund.bank_transaction_id is not None
        assert original_refund.journal_entry_id is not None
        original_transaction = db.get(BankTransaction, original_refund.bank_transaction_id)
        reversal_transaction = db.get(BankTransaction, original_refund.reversal_bank_transaction_id)
        assert original_transaction.bank_account_id == bank_account["id"]
        assert reversal_transaction.bank_account_id == bank_account["id"]
        assert original_transaction.direction == "out"
        assert reversal_transaction.direction == "in"
        assert db.get(JournalEntry, original_refund.journal_entry_id).source_type == "refund_ar"
        assert db.get(JournalEntry, original_refund.reversal_journal_entry_id).source_type == "refund_reversal_ar"
        assert db.get(JournalEntry, original_refund.reversal_journal_entry_id).reverses_entry_id == original_refund.journal_entry_id
        assert db.get(BankAccount, bank_account["id"]).ledger_account_id is not None

    with company_session("tc") as db:
        ar_original_entry_data = (
            db.query(JournalEntry)
            .options(__import__("sqlalchemy.orm").orm.selectinload(JournalEntry.lines))
            .filter(JournalEntry.id == original_refund.journal_entry_id)
            .one()
        )
        ar_reversal_entry_data = (
            db.query(JournalEntry)
            .options(__import__("sqlalchemy.orm").orm.selectinload(JournalEntry.lines))
            .filter(JournalEntry.id == original_refund.reversal_journal_entry_id)
            .one()
        )
        assert ar_original_entry_data.reverses_entry_id is None
        assert ar_reversal_entry_data.reverses_entry_id == ar_original_entry_data.id
        ar_original_entry = ar_original_entry_data
        ar_reversal_entry = ar_reversal_entry_data
    assert {
        (line.account_id, line.debit_amount, line.credit_amount)
        for line in ar_original_entry.lines
    } == {
        (accounts["1100"]["id"], Decimal("50.00"), Decimal("0.00")),
        (bank_account["ledger_account_id"], Decimal("0.00"), Decimal("50.00")),
    }
    assert {
        (line.account_id, line.debit_amount, line.credit_amount)
        for line in ar_reversal_entry.lines
    } == {
        (bank_account["ledger_account_id"], Decimal("50.00"), Decimal("0.00")),
        (accounts["1100"]["id"], Decimal("0.00"), Decimal("50.00")),
    }

    with company_session("tc") as db:
        ap_refund_row = db.get(CreditNoteRefund, ap_refund.json()["id"])
        assert ap_refund_row is not None
        ap_original_entry = (
            db.query(JournalEntry)
            .options(__import__("sqlalchemy.orm").orm.selectinload(JournalEntry.lines))
            .filter(JournalEntry.id == ap_refund_row.journal_entry_id)
            .one()
        )
    assert {
        (line.account_id, line.debit_amount, line.credit_amount)
        for line in ap_original_entry.lines
    } == {
        (bank_account["ledger_account_id"], Decimal("70.00"), Decimal("0.00")),
        (accounts["2000"]["id"], Decimal("0.00"), Decimal("70.00")),
    }

    ar_invoice = client.get(f"/api/v1/invoices/{ar_source['id']}", headers=HEAD).json()
    ap_invoice = client.get(f"/api/v1/invoices/{ap_source['id']}", headers=HEAD).json()
    assert Decimal(ar_invoice["paid_amount"]) == Decimal("0.00")
    assert Decimal(ar_invoice["outstanding_amount"]) == Decimal("220.00")
    assert Decimal(ar_invoice["credit_applied_amount"]) == Decimal("0.00")
    assert Decimal(ap_invoice["paid_amount"]) == Decimal("0.00")
    assert Decimal(ap_invoice["outstanding_amount"]) == Decimal("220.00")
    assert Decimal(ap_invoice["credit_applied_amount"]) == Decimal("0.00")
    with company_session("tc") as db:
        assert db.query(CreditNoteApplication).filter_by(credit_note_id=ar_note["id"]).count() == 0
        assert db.query(CreditNoteApplication).filter_by(credit_note_id=ap_note["id"]).count() == 0
        assert db.query(BankTransaction).filter_by(bank_account_id=bank_account["id"]).count() == 4


def test_refund_application_coexistence_and_locked_dates(client, accounts):
    source = _create_source(client, accounts, direction="AR", number="COEXIST-SOURCE")
    note, _ = _create_credit_note(client, source, direction="AR", number="COEXIST-CN")
    bank_account = _bank_accounts(client)[0]

    application = client.post(
        f"/api/v1/credit-notes/{note['id']}/applications",
        headers={**HEAD, "Idempotency-Key": "coexist-application"},
        json={"invoice_id": source["id"], "amount": "50.00", "application_date": "2026-06-10"},
    )
    assert application.status_code == 201, application.text
    refund = client.post(
        f"/api/v1/credit-notes/{note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "coexist-refund"},
        json=_refund_payload(bank_account["id"], "100.00", "2026-06-11"),
    )
    assert refund.status_code == 201, refund.text

    note_after = client.get(f"/api/v1/credit-notes/{note['id']}", headers=HEAD).json()
    assert Decimal(note_after["applied_amount"]) == Decimal("50.00")
    assert Decimal(note_after["refunded_amount"]) == Decimal("100.00")
    assert Decimal(note_after["remaining_amount"]) == Decimal("70.00")
    invoice_after = client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json()
    assert Decimal(invoice_after["paid_amount"]) == Decimal("0.00")
    assert Decimal(invoice_after["credit_applied_amount"]) == Decimal("50.00")
    assert Decimal(invoice_after["outstanding_amount"]) == Decimal("170.00")
    with company_session("tc") as db:
        assert db.query(CreditNoteApplication).filter_by(credit_note_id=note["id"]).count() == 1
        assert db.query(CreditNoteRefund).filter_by(credit_note_id=note["id"]).count() == 1
        refund_transaction = db.get(
            BankTransaction,
            db.query(CreditNoteRefund).filter_by(credit_note_id=note["id"]).one().bank_transaction_id,
        )
        assert refund_transaction is not None

    categorise = client.patch(
        f"/api/v1/bank-accounts/transactions/{refund_transaction.id}/categorise",
        headers=HEAD,
        json={"account_id": accounts["1000"]["id"], "tax_code": "none"},
    )
    assert categorise.status_code == 200, categorise.text

    locked = client.patch(
        "/api/v1/companies/tc",
        headers=HEAD,
        json={"books_locked_through": "2026-06-11"},
    )
    assert locked.status_code == 200, locked.text
    blocked_refund = client.post(
        f"/api/v1/credit-notes/{note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "blocked-refund"},
        json=_refund_payload(bank_account["id"], "1.00", "2026-06-11"),
    )
    assert blocked_refund.status_code == 409
    blocked_reversal = client.post(
        f"/api/v1/credit-notes/{note['id']}/refunds/{refund.json()['id']}/reverse",
        headers=HEAD,
        json={"reversal_date": "2026-06-11"},
    )
    assert blocked_reversal.status_code == 409
    with company_session("tc") as db:
        assert db.query(CreditNoteRefund).filter_by(credit_note_id=note["id"]).count() == 1
        assert db.query(BankTransaction).filter_by(bank_account_id=bank_account["id"]).count() == 1


def test_refund_schema_and_provenance_are_created_on_startup(client, accounts):
    source = _create_source(client, accounts, direction="AR", number="PROVENANCE-SOURCE")
    note, _ = _create_credit_note(client, source, direction="AR", number="PROVENANCE-CN")
    bank_account = _bank_accounts(client)[0]
    refund = client.post(
        f"/api/v1/credit-notes/{note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "provenance-refund"},
        json=_refund_payload(bank_account["id"], "25.00", "2026-06-25"),
    )
    assert refund.status_code == 201, refund.text

    from sqlalchemy import inspect

    with company_session("tc") as db:
        table_names = set(inspect(db.bind).get_table_names())
        assert {
            "credit_note_refunds",
            "credit_note_refund_idempotency_keys",
        }.issubset(table_names)
        refund_row = db.get(CreditNoteRefund, refund.json()["id"])
        assert refund_row is not None
        assert refund_row.credit_note_id == note["id"]
        assert refund_row.bank_transaction_id is not None
        assert refund_row.journal_entry_id is not None
        assert refund_row.status == "active"
        assert refund_row.reversal_journal_entry_id is None
        assert db.query(CreditNote).filter_by(id=note["id"]).one().currency == "AUD"
        assert db.query(JournalEntry).filter_by(id=refund_row.journal_entry_id).one().source_type == JournalEntrySource.REFUND_AR
        assert db.query(BankTransaction).filter_by(id=refund_row.bank_transaction_id).one().direction == "out"
        assert db.query(BankAccount).filter_by(id=bank_account["id"]).one().ledger_account_id is not None


def test_refund_rejects_invalid_selected_bank_account(client, accounts):
    source = _create_source(client, accounts, direction="AR", number="INVALID-BANK-SOURCE")
    note, _ = _create_credit_note(client, source, direction="AR", number="INVALID-BANK-CN")
    bank_account = _bank_accounts(client)[0]

    missing = client.post(
        f"/api/v1/credit-notes/{note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "missing-bank"},
        json=_refund_payload(bank_account["id"] + 999999, "10.00", "2026-06-26"),
    )
    assert missing.status_code == 409
    assert "does not exist" in missing.json()["detail"]

    inactive = client.patch(
        f"/api/v1/bank-accounts/{bank_account['id']}",
        headers=HEAD,
        json={"is_active": False},
    )
    assert inactive.status_code == 200
    inactive_refund = client.post(
        f"/api/v1/credit-notes/{note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "inactive-bank"},
        json=_refund_payload(bank_account["id"], "10.00", "2026-06-27"),
    )
    assert inactive_refund.status_code == 409
    assert "inactive" in inactive_refund.json()["detail"]

    with company_session("tc") as db:
        unlinked = BankAccount(
            name="Unlinked Refund Bank",
            ledger_account_id=None,
            is_active=True,
        )
        db.add(unlinked)
        db.commit()
        unlinked_id = unlinked.id
    unlinked_refund = client.post(
        f"/api/v1/credit-notes/{note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "unlinked-bank"},
        json=_refund_payload(unlinked_id, "10.00", "2026-06-28"),
    )
    assert unlinked_refund.status_code == 409
    assert "no ledger account mapping" in unlinked_refund.json()["detail"]
