"""Explicit bank-to-invoice allocation lifecycle regressions."""

from __future__ import annotations

import shutil
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.db.base import CompanyBase
from app.db.migrations import run_company_migrations
from app.services.allocation_batches import (
    InvalidExactMoneyInputError,
    parse_exact_money,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
HEAD = {"X-Company-Id": "alloc"}


def _manual_headers():
    return {**HEAD, "Idempotency-Key": f"alloc-{uuid4()}"}


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
        company = test_client.post(
            "/api/v1/companies",
            json={"id": "alloc", "name": "Allocation Pty Ltd", "gst_registered": True},
        )
        assert company.status_code == 201, company.text
        HEAD["X-Company-Generation"] = company.json()["generation_id"]
        yield test_client


@pytest.fixture()
def accounts(client):
    return {row["code"]: row for row in client.get("/api/v1/accounts", headers=HEAD).json()}


@pytest.fixture()
def bank(client):
    return client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]


def _posted_invoice(client, accounts, *, number: str, direction: str = "AR", total="110.00"):
    account = accounts["4000"] if direction == "AR" else accounts["6100"]
    contact_name = "Allocation Customer" if direction == "AR" else "Allocation Supplier"
    response = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json={
            "direction": direction,
            "contact_name": contact_name,
            "invoice_number": number,
            "issue_date": "2026-05-01",
            "subtotal": "100.00",
            "gst_amount": "10.00",
            "total": total,
            "lines": [
                {
                    "description": "Allocated service",
                    "account_id": account["id"],
                    "quantity": "1",
                    "unit_price": "100.00",
                    "gst_rate": "0.10",
                    "line_subtotal": "100.00",
                    "line_gst": "10.00",
                    "line_total": total,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    invoice = response.json()
    posted = client.post(f"/api/v1/invoices/{invoice['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    return invoice


def _manual_payment(client, bank, accounts, invoice, *, amount, direction="in"):
    control = accounts["1100"] if direction == "in" else accounts["2000"]
    return client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": direction,
            "amount": amount,
            "occurred_at": "2026-05-20",
            "memo": invoice["invoice_number"],
            "account_id": control["id"],
            # Deliberately wrong caller GST: settlement derives it from invoice.
            "tax_code": "none",
            "gst_amount": "0",
            "invoice_allocations": [{"invoice_id": invoice["id"], "amount": amount}],
        },
    )


def _posted_gst_free_invoice(client, accounts, *, number: str):
    response = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json={
            "direction": "AR",
            "contact_name": "GST Free Customer",
            "invoice_number": number,
            "issue_date": "2026-05-01",
            "subtotal": "110.00",
            "gst_amount": "0.00",
            "total": "110.00",
            "lines": [
                {
                    "description": "GST-free service",
                    "account_id": accounts["4000"]["id"],
                    "quantity": "1",
                    "unit_price": "110.00",
                    "gst_rate": "0",
                    "line_subtotal": "110.00",
                    "line_gst": "0.00",
                    "line_total": "110.00",
                    "tax_code": "gst_free",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    invoice = response.json()
    posted = client.post(f"/api/v1/invoices/{invoice['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    return invoice


def test_full_and_partial_allocations_derive_invoice_state_and_gst(client, accounts, bank):
    invoice = _posted_invoice(client, accounts, number="ALLOC-AR-1")

    first = _manual_payment(client, bank, accounts, invoice, amount="40.00")
    assert first.status_code == 201, first.text
    assert Decimal(first.json()["gst_amount"]) == Decimal("3.64")
    assert first.json()["invoice_allocations"][0]["invoice_id"] == invoice["id"]
    current = client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json()
    assert current["status"] == "partial"
    assert Decimal(current["paid_amount"]) == Decimal("40.00")

    second = _manual_payment(client, bank, accounts, invoice, amount="70.00")
    assert second.status_code == 201, second.text
    assert Decimal(second.json()["gst_amount"]) == Decimal("6.36")
    current = client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json()
    assert current["status"] == "paid"
    assert Decimal(current["paid_amount"]) == Decimal("110.00")
    assert current["paid_date"] == "2026-05-20"

    bas = client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    ).json()
    assert Decimal(bas["one_a_gst_on_sales"]) == Decimal("10.00")


def test_batch_allocation_and_ap_payment_update_each_invoice(client, accounts, bank):
    first = _posted_invoice(client, accounts, number="ALLOC-BATCH-A")
    second = _posted_invoice(client, accounts, number="ALLOC-BATCH-B")
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "220.00",
            "occurred_at": "2026-05-20",
            "account_id": accounts["1100"]["id"],
            "invoice_allocations": [
                {"invoice_id": first["id"], "amount": "110.00"},
                {"invoice_id": second["id"], "amount": "110.00"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["gst_amount"]) == Decimal("20.00")
    for invoice in (first, second):
        body = client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json()
        assert body["status"] == "paid"

    ap = _posted_invoice(client, accounts, number="ALLOC-AP-1", direction="AP")
    payment = _manual_payment(
        client, bank, accounts, ap, amount="110.00", direction="out"
    )
    assert payment.status_code == 201, payment.text
    body = client.get(f"/api/v1/invoices/{ap['id']}", headers=HEAD).json()
    assert body["status"] == "paid"


def test_overpayment_splits_control_and_customer_deposit_without_double_count(
    client, accounts, bank
):
    invoice = _posted_invoice(client, accounts, number="ALLOC-OVERPAY")
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "120.00",
            "occurred_at": "2026-05-20",
            "memo": "ALLOC-OVERPAY",
            "account_id": accounts["1100"]["id"],
            "invoice_allocations": [
                {"invoice_id": invoice["id"], "amount": "110.00"}
            ],
            "unapplied_account_id": accounts["2050"]["id"],
        },
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["unapplied_amount"]) == Decimal("10.00")
    assert response.json()["unapplied_account_id"] == accounts["2050"]["id"]
    current = client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json()
    assert current["status"] == "paid"

    tb = client.get(
        "/api/v1/reports/trial-balance",
        headers=HEAD,
        params={"as_of": "2026-05-31"},
    ).json()
    assert tb["is_balanced"], tb
    rows = {row.get("ref_id"): row for row in tb["rows"]}
    assert Decimal(rows[accounts["1100"]["id"]]["net_debit"]) == 0
    assert Decimal(rows[accounts["2050"]["id"]]["credit_total"]) == Decimal(
        "10.00"
    )

    pnl = client.get(
        "/api/v1/reports/profit-loss",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()
    assert Decimal(pnl["total_income"]) == Decimal("100.00")
    bas = client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    ).json()
    assert Decimal(bas["g1_total_sales"]) == Decimal("110.00")
    assert Decimal(bas["one_a_gst_on_sales"]) == Decimal("10.00")


def test_mixed_tax_batch_uses_allocation_components_for_cash_bas(
    client, accounts, bank
):
    taxable = _posted_invoice(client, accounts, number="ALLOC-TAXABLE")
    gst_free = _posted_gst_free_invoice(client, accounts, number="ALLOC-GSTFREE")
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "220.00",
            "occurred_at": "2026-05-20",
            "account_id": accounts["1100"]["id"],
            "invoice_allocations": [
                {"invoice_id": taxable["id"], "amount": "110.00"},
                {"invoice_id": gst_free["id"], "amount": "110.00"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    components = {
        part["tax_code"]: Decimal(part["gross_amount"])
        for allocation in response.json()["invoice_allocations"]
        for part in allocation["tax_components"]
    }
    assert components == {
        "standard": Decimal("110.00"),
        "gst_free": Decimal("110.00"),
    }

    bas = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()
    assert Decimal(bas["g1_total_sales"]) == Decimal("220.00")
    assert Decimal(bas["g3_gst_free_sales"]) == Decimal("110.00")
    assert Decimal(bas["g6_sales_subject_to_gst"]) == Decimal("110.00")
    assert Decimal(bas["one_a_gst_on_sales"]) == Decimal("10.00")


def test_supplier_overpayment_splits_ap_and_prepayment_asset(
    client, accounts, bank
):
    invoice = _posted_invoice(
        client,
        accounts,
        number="ALLOC-AP-OVERPAY",
        direction="AP",
    )
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "out",
            "amount": "120.00",
            "occurred_at": "2026-05-20",
            "account_id": accounts["2000"]["id"],
            "invoice_allocations": [
                {"invoice_id": invoice["id"], "amount": "110.00"}
            ],
            "unapplied_account_id": accounts["1500"]["id"],
        },
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["unapplied_amount"]) == Decimal("10.00")

    tb = client.get(
        "/api/v1/reports/trial-balance",
        headers=HEAD,
        params={"as_of": "2026-05-31"},
    ).json()
    assert tb["is_balanced"], tb
    rows = {row.get("ref_id"): row for row in tb["rows"]}
    assert Decimal(rows[accounts["2000"]["id"]]["net_debit"]) == 0
    assert Decimal(rows[accounts["1500"]["id"]]["debit_total"]) == Decimal(
        "10.00"
    )
    bas = client.get(
        "/api/v1/reports/gst-exposure",
        headers=HEAD,
        params={"period_start": "2026-05-01", "period_end": "2026-05-31"},
    ).json()
    assert Decimal(bas["g11_non_capital_purchases"]) == Decimal("110.00")
    assert Decimal(bas["one_b_gst_on_purchases"]) == Decimal("10.00")


def test_control_accounts_fail_closed_without_complete_valid_allocations(client, accounts, bank):
    invoice = _posted_invoice(client, accounts, number="ALLOC-GUARD")
    base = {
        "direction": "in",
        "amount": "110.00",
        "occurred_at": "2026-05-20",
        "account_id": accounts["1100"]["id"],
    }
    missing = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json=base,
    )
    assert missing.status_code == 409, missing.text
    assert "invoice allocation" in missing.json()["detail"]

    short = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={**base, "invoice_allocations": [{"invoice_id": invoice["id"], "amount": "100"}]},
    )
    assert short.status_code == 409, short.text
    assert "customer-deposit LIABILITY" in short.json()["detail"]

    early = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            **base,
            "occurred_at": "2026-04-30",
            "invoice_allocations": [{"invoice_id": invoice["id"], "amount": "110"}],
        },
    )
    assert early.status_code == 409, early.text
    assert "before invoice" in early.json()["detail"]


def test_deleting_allocated_transaction_reopens_invoice_then_allows_void(client, accounts, bank):
    invoice = _posted_invoice(client, accounts, number="ALLOC-DELETE")
    payment = _manual_payment(client, bank, accounts, invoice, amount="110.00")
    assert payment.status_code == 201, payment.text
    blocked = client.post(f"/api/v1/invoices/{invoice['id']}/void", headers=HEAD)
    assert blocked.status_code == 409, blocked.text

    deleted = client.delete(
        f"/api/v1/bank-accounts/transactions/{payment.json()['id']}", headers=HEAD
    )
    assert deleted.status_code == 204, deleted.text
    reopened = client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json()
    assert reopened["status"] == "authorised"
    assert Decimal(reopened["paid_amount"]) == 0
    voided = client.post(f"/api/v1/invoices/{invoice['id']}/void", headers=HEAD)
    assert voided.status_code == 200, voided.text


@pytest.mark.parametrize("history_kind", ["baseline", "event"])
def test_history_blocks_recategorisation_and_deletion_without_state_change(
    client, accounts, bank, history_kind
):
    invoice = _posted_invoice(client, accounts, number=f"ALLOC-HISTORY-{history_kind}")
    payment = _manual_payment(client, bank, accounts, invoice, amount="40.00")
    assert payment.status_code == 201, payment.text
    transaction_id = payment.json()["id"]

    from app.db.company import company_session
    from app.services.allocation_history import capture_legacy_allocation_baseline

    with company_session("alloc") as db:
        if history_kind == "baseline":
            capture_legacy_allocation_baseline(
                db, transaction_id, datetime(2026, 10, 12)
            )
        else:
            db.execute(
                text(
                    "INSERT INTO allocation_history_events "
                    "(operation, bank_transaction_id) "
                    "VALUES ('legacy_history_unknown', :transaction_id)"
                ),
                {"transaction_id": transaction_id},
            )
        db.commit()

    def read_operational_state():
        with company_session("alloc") as db:
            return {
                "transaction": tuple(db.execute(
                    text("SELECT * FROM bank_transactions WHERE id=:id"),
                    {"id": transaction_id},
                ).one()),
                "allocations": tuple(db.execute(
                    text(
                        "SELECT * FROM invoice_payment_allocations "
                        "WHERE bank_transaction_id=:id ORDER BY id"
                    ),
                    {"id": transaction_id},
                ).all()),
                "tax_components": tuple(db.execute(
                    text(
                        "SELECT c.* FROM invoice_payment_tax_components c "
                        "JOIN invoice_payment_allocations a ON a.id=c.allocation_id "
                        "WHERE a.bank_transaction_id=:id ORDER BY c.id"
                    ),
                    {"id": transaction_id},
                ).all()),
                "invoice": tuple(db.execute(
                    text("SELECT * FROM invoices WHERE id=:id"),
                    {"id": invoice["id"]},
                ).one()),
            }

    def read_transaction():
        rows = client.get(
            f"/api/v1/bank-accounts/{bank['id']}/transactions", headers=HEAD
        ).json()
        return next(row for row in rows if row["id"] == transaction_id)

    transaction_before = read_transaction()
    invoice_before = client.get(
        f"/api/v1/invoices/{invoice['id']}", headers=HEAD
    ).json()
    cash_before = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0][
        "current_balance"
    ]
    operational_before = read_operational_state()

    account_only = client.patch(
        f"/api/v1/bank-accounts/transactions/{transaction_id}/categorise",
        headers=HEAD,
        json={"account_id": accounts["4000"]["id"]},
    )
    assert account_only.status_code == 409, account_only.text
    assert account_only.json()["detail"] == (
        "This bank transaction has allocation history and cannot be changed."
    )
    assert read_operational_state() == operational_before

    recategorised = client.patch(
        f"/api/v1/bank-accounts/transactions/{transaction_id}/categorise",
        headers=HEAD,
        json={
            "account_id": accounts["1100"]["id"],
            "tax_code": "none",
            "gst_amount": "0.00",
        },
    )
    assert recategorised.status_code == 409, recategorised.text
    assert recategorised.json()["detail"] == (
        "This bank transaction has allocation history and cannot be changed."
    )
    assert "SQLite" not in recategorised.text
    assert "trigger" not in recategorised.text.lower()

    deleted = client.delete(
        f"/api/v1/bank-accounts/transactions/{transaction_id}", headers=HEAD
    )
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"] == (
        "This bank transaction has allocation history and cannot be changed."
    )
    assert "SQLite" not in deleted.text
    assert "trigger" not in deleted.text.lower()

    assert read_transaction() == transaction_before
    assert client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json() == invoice_before
    assert client.get("/api/v1/bank-accounts", headers=HEAD).json()[0][
        "current_balance"
    ] == cash_before


def test_ambiguous_open_invoice_cash_cannot_be_double_counted_as_income(client, accounts, bank):
    _posted_invoice(client, accounts, number="ALLOC-AMB-A")
    _posted_invoice(client, accounts, number="ALLOC-AMB-B")
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "50.00",
            "occurred_at": "2026-05-20",
            "counter_party_name": "Allocation Customer",
            "account_id": accounts["4000"]["id"],
            "gst_amount": "4.55",
        },
    )
    assert response.status_code == 409, response.text
    assert "multiple open AR invoices" in response.json()["detail"]


def test_fully_settled_invoice_name_does_not_block_unrelated_income(
    client, accounts, bank
):
    invoice = _posted_invoice(client, accounts, number="ALLOC-CLOSED")
    settled = _manual_payment(
        client,
        bank,
        accounts,
        invoice,
        amount="110.00",
    )
    assert settled.status_code == 201, settled.text

    unrelated = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "25.00",
            "occurred_at": "2026-05-21",
            # A historical reference must not turn a fully-paid invoice into
            # an eternal text-based false positive.
            "memo": "Separate income ALLOC-CLOSED",
            "account_id": accounts["4000"]["id"],
            "tax_code": "standard",
            "gst_amount": "2.27",
        },
    )
    assert unrelated.status_code == 201, unrelated.text


def test_future_scheduled_cash_cannot_pre_pay_invoice_state(client, accounts, bank):
    invoice = _posted_invoice(client, accounts, number="ALLOC-FUTURE")
    scheduled = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "110.00",
            "occurred_at": "2026-12-31",
            "account_id": accounts["1100"]["id"],
            "invoice_allocations": [
                {"invoice_id": invoice["id"], "amount": "110.00"}
            ],
        },
    )
    assert scheduled.status_code == 409, scheduled.text
    assert "future-dated" in scheduled.json()["detail"]

    uncategorised = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "110.00",
            "occurred_at": "2026-12-31",
            "memo": "Scheduled ALLOC-FUTURE",
        },
    )
    assert uncategorised.status_code == 201, uncategorised.text
    current = client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json()
    assert current["status"] == "authorised"
    assert Decimal(current["paid_amount"]) == 0


def test_allocation_requires_verified_invoice_journal(client, accounts, bank):
    invoice = _posted_invoice(client, accounts, number="ALLOC-NO-JOURNAL")
    from app.db.company import company_session
    from app.models.company import Invoice, JournalEntry, JournalEntrySource

    with company_session("alloc") as db:
        inv = db.get(Invoice, invoice["id"])
        original = (
            db.query(JournalEntry)
            .filter(
                JournalEntry.source_id == inv.id,
                JournalEntry.source_type == JournalEntrySource.INVOICE_AR,
            )
            .one()
        )
        db.delete(original)
        db.commit()

    response = _manual_payment(
        client,
        bank,
        accounts,
        invoice,
        amount="110.00",
    )
    assert response.status_code == 409, response.text
    assert "no verified accrual journal" in response.json()["detail"]


def test_reconciliation_and_import_paths_require_and_apply_allocations(client, accounts, bank):
    invoice = _posted_invoice(client, accounts, number="ALLOC-RECAT")
    uncategorised = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=_manual_headers(),
        json={
            "direction": "in",
            "amount": "110.00",
            "occurred_at": "2026-05-20",
            "memo": "ALLOC-RECAT",
            "tax_code": "standard",
            "gst_amount": "10.00",
        },
    )
    assert uncategorised.status_code == 201, uncategorised.text
    recategorised = client.patch(
        f"/api/v1/bank-accounts/transactions/{uncategorised.json()['id']}/categorise",
        headers=HEAD,
        json={
            "account_id": accounts["1100"]["id"],
            "invoice_allocations": [{"invoice_id": invoice["id"], "amount": "110.00"}],
        },
    )
    assert recategorised.status_code == 200, recategorised.text
    assert recategorised.json()["invoice_allocations"][0]["invoice_id"] == invoice["id"]
    assert client.get(f"/api/v1/invoices/{invoice['id']}", headers=HEAD).json()["status"] == "paid"

    second = _posted_invoice(client, accounts, number="ALLOC-IMPORT")
    imported = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {
                    "occurred_at": "2026-05-21",
                    "direction": "in",
                    "amount": "110.00",
                    "memo": "ALLOC-IMPORT",
                    "account_id": accounts["1100"]["id"],
                    "tax_code": "standard",
                    "gst_amount": "10.00",
                    "invoice_allocations": [
                        {"invoice_id": second["id"], "amount": "110.00"}
                    ],
                }
            ]
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 1
    assert client.get(f"/api/v1/invoices/{second['id']}", headers=HEAD).json()["status"] == "paid"


def test_startup_reconciles_unverifiable_legacy_payment_state(
    client, accounts, bank
):
    invoice = _posted_invoice(client, accounts, number="ALLOC-LEGACY")
    from app.db.company import (
        company_session,
        dispose_company_engine,
        init_company_db,
    )
    from app.models.company import (
        BankTransaction,
        BankTxnDirection,
        Invoice,
        PaymentReconciliationEvent,
        TaxCode,
    )

    with company_session("alloc") as db:
        inv = db.get(Invoice, invoice["id"])
        inv.status = "partial"
        inv.paid_amount = Decimal("55.00")
        inv.paid_date = date(2026, 5, 20)
        db.add(
            BankTransaction(
                bank_account_id=bank["id"],
                direction=BankTxnDirection.IN,
                amount=Decimal("55.00"),
                occurred_at=date(2026, 5, 20),
                account_id=accounts["1100"]["id"],
                gst_amount=Decimal("5.00"),
                tax_code=TaxCode.STANDARD,
            )
        )
        db.commit()

    dispose_company_engine("alloc")
    _added, applied = init_company_db("alloc")
    assert "backup:payment_reconciliation" in applied
    assert any(step.startswith("reconcile:payments:") for step in applied)
    with company_session("alloc") as db:
        inv = db.get(Invoice, invoice["id"])
        assert inv.status == "authorised"
        assert Decimal(inv.paid_amount) == 0
        txn = db.query(BankTransaction).one()
        assert txn.account_id is None
        assert Decimal(txn.gst_amount) == 0
        assert db.query(PaymentReconciliationEvent).count() == 2


def test_startup_backfills_missing_allocation_tax_components(
    client, accounts, bank
):
    invoice = _posted_invoice(client, accounts, number="ALLOC-TAX-UPGRADE")
    payment = _manual_payment(client, bank, accounts, invoice, amount="110.00")
    assert payment.status_code == 201, payment.text
    from app.db.company import (
        company_session,
        dispose_company_engine,
        init_company_db,
    )
    from app.models.company import (
        InvoiceLine,
        InvoicePaymentTaxComponent,
    )

    with company_session("alloc") as db:
        db.query(InvoicePaymentTaxComponent).delete()
        line = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice["id"]).one()
        line.tax_code = "gst_free"
        db.commit()

    dispose_company_engine("alloc")
    _added, applied = init_company_db("alloc")
    assert "backfill:invoice_payment_tax_components:1" in applied
    with company_session("alloc") as db:
        component = db.query(InvoicePaymentTaxComponent).one()
        assert component.tax_code == "standard"
        assert Decimal(component.gross_amount) == Decimal("110.00")
        assert Decimal(component.gst_amount) == Decimal("10.00")


def test_parse_exact_money_accepts_valid_values_and_rejects_invalid_input():
    assert parse_exact_money("12.34") == 1234
    assert parse_exact_money("12") == 1200
    assert parse_exact_money(Decimal("44.44")) == 4444

    for value in (
        "1e2",
        1.23,
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",
        "INF",
        "-1.00",
        "1.005",
        "92233720368547758.08",
    ):
        with pytest.raises(InvalidExactMoneyInputError):
            parse_exact_money(value)

    assert parse_exact_money("2147483647.99") == 214748364799


def _synthetic_projector_session(tmp_path, name="projector.db"):
    db_path = tmp_path / name
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    CompanyBase.metadata.create_all(engine)
    run_company_migrations(engine)
    return engine


def _seed_synthetic_projector_parents(db):
    db.execute(
        text(
            "INSERT INTO bank_accounts (id, name, opening_balance, is_active) "
            "VALUES (1, 'Synthetic bank', 0, 1)"
        )
    )
    db.execute(
        text(
            "INSERT INTO contacts (id, kind, name, active, created_at) "
            "VALUES (1, 'customer', 'Synthetic customer', 1, CURRENT_TIMESTAMP)"
        )
    )
    db.execute(
        text(
            "INSERT INTO invoices (id, direction, contact_id, invoice_number, issue_date, "
            "currency, subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
            "source, created_at, updated_at) VALUES "
            "(99, 'AR', 1, 'SYN-PROJECTOR', '2026-10-01', 'AUD', 10, 1.23, 11.23, 1, "
            "'draft', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )


def _seed_synthetic_projector_rows(
    db, *, transaction_id, allocation_id=None, component_id=None
):
    db.execute(
        text(
            "INSERT INTO bank_transactions (id, bank_account_id, direction, amount, occurred_at, gst_amount, tax_code, unapplied_amount) "
            "VALUES (:transaction_id, 1, 'in', 11.23, '2026-10-01', 1.23, 'standard', 0)"
        ),
        {"transaction_id": transaction_id},
    )
    if allocation_id is not None:
        db.execute(
            text(
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount, created_at) "
                "VALUES (:allocation_id, :transaction_id, 99, 11.23, 1.23, "
                "'2026-10-01 08:00:00')"
            ),
            {"allocation_id": allocation_id, "transaction_id": transaction_id},
        )
    if component_id is not None:
        db.execute(
            text(
                "INSERT INTO invoice_payment_tax_components "
                "(id, allocation_id, tax_code, gross_amount, gst_amount) "
                "VALUES (:component_id, :allocation_id, 'standard', 11.23, 1.23)"
            ),
            {"component_id": component_id, "allocation_id": allocation_id},
        )


def test_projector_reports_complete_and_uncertain_states_for_baseline_visibility(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.db.company import begin_sqlite_immediate
    from app.db.migrations import allocation_baseline_integrity_hash
    from app.services.allocation_history import capture_legacy_allocation_baseline
    from app.services.allocation_projection import project_allocation_state

    observed_at = datetime(2026, 10, 12)
    engine = _synthetic_projector_session(tmp_path)
    with Session(engine) as db:
        begin_sqlite_immediate(db)
        _seed_synthetic_projector_parents(db)
        _seed_synthetic_projector_rows(db, transaction_id=2, allocation_id=22, component_id=222)
        _seed_synthetic_projector_rows(db, transaction_id=3)
        _seed_synthetic_projector_rows(db, transaction_id=4, allocation_id=24)
        db.commit()

    with Session(engine) as db:
        baseline = capture_legacy_allocation_baseline(db, 2, observed_at)
        assert baseline.status == "complete"
        assert baseline.incomplete_reasons == ()
        assert baseline.allocation_count == 1
        assert baseline.component_count == 1
        assert baseline.gross_total_cents == 1123
        assert baseline.gst_total_cents == 123

        allocation_binding = db.execute(
            text(
                "SELECT allocation_row_id, baseline_event_id, bank_transaction_id, "
                "source_kind, invoice_or_bill_id, allocated_gross_cents, "
                "allocated_gross_source, allocated_gst_cents, allocated_gst_source, "
                "allocation_created_at FROM allocation_history_baseline_bindings "
                "WHERE baseline_event_id = :event_id"
            ),
            {"event_id": baseline.baseline_event_id},
        ).one()
        assert tuple(allocation_binding) == (
            22, baseline.baseline_event_id, 2, "invoice", 99, 1123, "11.23", 123,
            "1.23", "2026-10-01 08:00:00",
        )
        component_binding = db.execute(
            text(
                "SELECT source_component_row_id, tax_code, component_gross_cents, "
                "component_gross_source, component_gst_cents, component_gst_source "
                "FROM allocation_history_baseline_tax_bindings tb "
                "JOIN allocation_history_baseline_bindings b "
                "ON b.id = tb.allocation_binding_id WHERE b.baseline_event_id = :event_id"
            ),
            {"event_id": baseline.baseline_event_id},
        ).one()
        assert tuple(component_binding) == (222, "standard", 1123, "11.23", 123, "1.23")
        finalization = db.execute(
            text(
                "SELECT observed_payment_date, CAST(observed_at AS TEXT), "
                "observed_allocation_count, observed_component_count, "
                "gross_allocation_total_cents, gst_total_cents, integrity_hash, status, "
                "incomplete_reason_codes FROM allocation_history_baseline_finalizations "
                "WHERE baseline_event_id = :event_id"
            ),
            {"event_id": baseline.baseline_event_id},
        ).one()
        assert tuple(finalization) == (
            "2026-10-01", "2026-10-12 00:00:00.000000", 1, 1, 1123, 123,
            baseline.integrity_hash, "complete", "[]",
        )
        assert len(baseline.integrity_hash) == 64
        db.commit()

    batch_values = {
        "bank_transaction_id": 2,
        "operation": "allocation_correction",
        "transaction_version": 1,
        "expected_prior_version": 0,
        "effective_date": "2026-10-01",
        "recorded_at": "2026-10-13 00:00:00.000000",
        "idempotency_key": "projector-baseline",
        "reason": "Synthetic projector test",
        "evidence_reference": "synthetic://projector",
        "evidence_sha256": None,
        "actor_placeholder": "Synthetic operator",
        "linked_prior_batch_id": None,
        "internal_correction_approval_id": None,
        "manual_review_status": "not_required",
        "residual_amount_cents": 0,
        "residual_account_id": None,
        "baseline_finalization_id": baseline.baseline_event_id,
    }
    batch_hash = allocation_baseline_integrity_hash([
        {"kind": "batch", **batch_values},
        {
            "kind": "line",
            "line_order": 0,
            "source_kind": "invoice",
            "invoice_or_bill_id": 99,
            "gross_cents": 1123,
            "gst_cents": 123,
        },
        {
            "kind": "component",
            "line_order": 0,
            "tax_code": "standard",
            "gross_cents": 1123,
            "gst_cents": 123,
        },
    ])
    with Session(engine) as db:
        begin_sqlite_immediate(db)
        from sqlalchemy.exc import IntegrityError

        insert_batch = text(
            "INSERT INTO allocation_batches "
            "(bank_transaction_id, operation, transaction_version, expected_prior_version, "
            "effective_date, recorded_at, idempotency_key, canonical_payload_hash, reason, "
            "evidence_reference, evidence_sha256, actor_placeholder, linked_prior_batch_id, "
            "internal_correction_approval_id, manual_review_status, residual_amount_cents, "
            "residual_account_id, baseline_finalization_id) VALUES "
            "(:bank_transaction_id, :operation, :transaction_version, :expected_prior_version, "
            ":effective_date, :recorded_at, :idempotency_key, :canonical_payload_hash, :reason, "
            ":evidence_reference, :evidence_sha256, :actor_placeholder, :linked_prior_batch_id, "
            ":internal_correction_approval_id, :manual_review_status, :residual_amount_cents, "
            ":residual_account_id, :baseline_finalization_id) RETURNING id"
        )
        for invalid_values in (
            {**batch_values, "idempotency_key": "projector-invalid-hash", "canonical_payload_hash": "bad"},
            {**batch_values, "idempotency_key": "projector-invalid-timestamp", "recorded_at": "not-a-timestamp", "canonical_payload_hash": batch_hash},
        ):
            with pytest.raises(IntegrityError):
                with db.begin_nested():
                    db.execute(insert_batch, invalid_values)

        batch_id = db.execute(
            insert_batch,
            {**batch_values, "canonical_payload_hash": batch_hash},
        ).scalar_one()
        line_id = db.execute(
            text(
                "INSERT INTO allocation_batch_lines "
                "(batch_id, line_order, source_kind, invoice_or_bill_id, gross_cents, gst_cents) "
                "VALUES (:batch_id, 0, 'invoice', 99, 1123, 123) RETURNING id"
            ),
            {"batch_id": batch_id},
        ).scalar_one()
        db.execute(
            text(
                "INSERT INTO allocation_batch_tax_components "
                "(allocation_batch_line_id, tax_code, gross_cents, gst_cents) "
                "VALUES (:line_id, 'standard', 1123, 123)"
            ),
            {"line_id": line_id},
        )
        db.commit()

    with Session(engine) as db:
        projection = project_allocation_state(
            db, 2, datetime(2026, 10, 1), datetime(2026, 10, 12, 23, 59, 59)
        )
        assert projection.certainty == "complete"
        assert projection.source == "complete_baseline"
        assert projection.transaction_id == 2
        assert projection.selected_batch_id is None
        assert projection.total_allocated_cents == 1123
        assert projection.total_gst_cents == 123
        assert projection.bank_cash_cents == 1123
        assert projection.residual_cents == 0
        assert len(projection.ordered_allocation_lines) == 1
        baseline_line = projection.ordered_allocation_lines[0]
        assert baseline_line.gross_cents == 1123
        assert baseline_line.gst_cents == 123
        assert baseline_line.tax_components == (("standard", 1123, 123),)

    with Session(engine) as db:
        projection = project_allocation_state(
            db, 2, datetime(2026, 9, 30), datetime(2026, 10, 12, 23, 59, 59)
        )
        assert projection.certainty == "uncertain"
        assert projection.source == "uncertain"
        assert projection.uncertainty_reason == "pre_baseline_operation"

    with Session(engine) as db:
        projection = project_allocation_state(
            db, 2, datetime(2026, 10, 1), datetime(2026, 10, 11, 23, 59, 59)
        )
        assert projection.certainty == "uncertain"
        assert projection.source == "uncertain"
        assert projection.uncertainty_reason == "pre_observation_knowledge"

    with Session(engine) as db:
        projection = project_allocation_state(
            db, 2, datetime(2026, 10, 1), datetime(2026, 10, 13, 0, 0, 30)
        )
        assert projection.certainty == "complete"
        assert projection.source == "complete_baseline"
        assert projection.selected_batch_id is None

    with Session(engine) as db:
        begin_sqlite_immediate(db)
        db.execute(
            text(
                "INSERT INTO allocation_batch_finalizations "
                "(batch_id, allocation_line_count, component_count, total_allocated_cents, "
                "total_gst_cents, residual_cents, bank_cash_cents, canonical_integrity_hash, finalized_at) "
                "VALUES (:batch_id, 1, 1, 1123, 123, 0, 1123, :canonical_integrity_hash, :finalized_at)"
            ),
            {
                "batch_id": batch_id,
                "canonical_integrity_hash": batch_hash,
                "finalized_at": "2026-10-13 00:01:00.000000",
            },
        )
        db.commit()

    with Session(engine) as db:
        projection = project_allocation_state(
            db, 2, datetime(2026, 10, 1), datetime(2026, 10, 13, 0, 1)
        )
        assert projection.certainty == "complete"
        assert projection.source == "finalized_batch"
        assert projection.selected_batch_id == batch_id
        assert projection.selected_batch_version == 1
        assert projection.total_allocated_cents == 1123
        assert projection.total_gst_cents == 123
        assert projection.bank_cash_cents == 1123
        assert projection.residual_cents == 0

    with Session(engine) as db:
        execute = db.execute

        def execute_with_orphaned_batch_lookup(statement, *args, **kwargs):
            parameters = args[0] if args else kwargs.get("params", {})
            if (
                "SELECT 1 FROM allocation_batches" in str(statement)
                and parameters.get("bank_transaction_id") == 3
            ):
                return SimpleNamespace(first=lambda: (1,))
            return execute(statement, *args, **kwargs)

        with patch.object(db, "execute", side_effect=execute_with_orphaned_batch_lookup):
            projection = project_allocation_state(db, 3, "2026-10-01", "2026-10-12")
        assert projection.certainty == "uncertain"
        assert projection.uncertainty_reason == "missing_baseline"

    with Session(engine) as db:
        incomplete = capture_legacy_allocation_baseline(db, 4, observed_at)
        assert incomplete.status == "incomplete"
        assert incomplete.incomplete_reasons == ("missing_tax_components",)
        stored_reasons = db.execute(
            text(
                "SELECT incomplete_reason_codes FROM allocation_history_baseline_finalizations "
                "WHERE baseline_event_id = :event_id"
            ),
            {"event_id": incomplete.baseline_event_id},
        ).scalar_one()
        assert stored_reasons == '["missing_tax_components"]'
        assert len(incomplete.integrity_hash) == 64
        db.commit()

    with Session(engine) as db:
        projection = project_allocation_state(db, 4, "2026-10-01", "2026-10-12")
        assert projection.certainty == "uncertain"
        assert projection.uncertainty_reason == "baseline_incomplete"

    assert not (tmp_path.parent / "companies").exists()
    engine.dispose()
