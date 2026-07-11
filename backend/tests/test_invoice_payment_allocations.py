"""Explicit bank-to-invoice allocation lifecycle regressions."""

from __future__ import annotations

import shutil
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


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
