"""P1-4: gst_registered=False is a server-side accounting invariant."""

from __future__ import annotations

import io
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from _request_headers import manual_transaction_headers


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROJECT_ROOT = ROOT.parent


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

    with TestClient(app) as test_client:
        yield test_client


def _company(client: TestClient, company_id: str, *, gst_registered: bool) -> dict:
    response = client.post(
        "/api/v1/companies",
        json={
            "id": company_id,
            "name": f"{company_id} Pty Ltd",
            "gst_registered": gst_registered,
        },
    )
    assert response.status_code == 201, response.text
    company = response.json()
    return {
        "X-Company-Id": company_id,
        "X-Company-Generation": company["generation_id"],
    }


def _accounts(client: TestClient, headers: dict) -> dict:
    response = client.get("/api/v1/accounts", headers=headers)
    assert response.status_code == 200, response.text
    return {row["code"]: row for row in response.json()}


def _bank(client: TestClient, headers: dict) -> dict:
    response = client.get("/api/v1/bank-accounts", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()[0]


def _invoice_payload(direction: str, account_id: int, *, gst: str) -> dict:
    subtotal = "100.00" if gst != "0" else "110.00"
    return {
        "direction": direction,
        "contact_name": f"{direction} Contact",
        "invoice_number": f"{direction}-{gst}",
        "issue_date": "2026-05-10",
        "subtotal": subtotal,
        "gst_amount": gst,
        "total": "110.00",
        "gst_inclusive": gst != "0",
        "status": "authorised",
        "lines": [
            {
                "description": "Service",
                "account_id": account_id,
                "line_subtotal": subtotal,
                "line_gst": gst,
                "line_total": "110.00",
            }
        ],
    }


def test_non_gst_manual_invoices_reject_gst_and_post_full_gross(client):
    headers = _company(client, "nogstinv", gst_registered=False)
    accounts = _accounts(client, headers)

    for direction, code in (("AR", "4000"), ("AP", "6400")):
        rejected = client.post(
            "/api/v1/invoices",
            headers=headers,
            json=_invoice_payload(direction, accounts[code]["id"], gst="10.00"),
        )
        assert rejected.status_code == 422, rejected.text
        assert "not GST-registered" in rejected.text

        accepted = client.post(
            "/api/v1/invoices",
            headers=headers,
            json=_invoice_payload(direction, accounts[code]["id"], gst="0"),
        )
        assert accepted.status_code == 201, accepted.text
        invoice = accepted.json()
        assert invoice["status"] == "authorised"
        assert Decimal(invoice["subtotal"]) == Decimal("110.00")
        assert Decimal(invoice["gst_amount"]) == 0
        assert Decimal(invoice["total"]) == Decimal("110.00")

    from app.db.company import company_session
    from app.models.company import Account, JournalLine

    with company_session("nogstinv") as db:
        gst_lines = (
            db.query(JournalLine)
            .join(Account, Account.id == JournalLine.account_id)
            .filter(Account.code.in_(["1200", "2100"]))
            .all()
        )
        assert gst_lines == []


def test_non_gst_excel_total_only_is_zero_gst_and_explicit_gst_is_skipped(client):
    headers = _company(client, "nogstexcel", gst_registered=False)
    total_only = {
        "mapping": {
            "contact_name": 0,
            "invoice_number": 1,
            "issue_date": 2,
            "total": 3,
        },
        "rows": [{"row_no": 2, "raw": ["Supplier", "N-1", "2026-05-10", "110.00"]}],
        "direction_default": "AP",
    }
    response = client.post(
        "/api/v1/invoices/import-excel-rows", headers=headers, json=total_only
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["created"]) == 1
    invoice = client.get("/api/v1/invoices", headers=headers).json()[0]
    assert Decimal(invoice["subtotal"]) == Decimal("110.00")
    assert Decimal(invoice["gst_amount"]) == 0
    assert Decimal(invoice["total"]) == Decimal("110.00")

    explicit = {
        "mapping": {
            "contact_name": 0,
            "invoice_number": 1,
            "issue_date": 2,
            "subtotal": 3,
            "gst_amount": 4,
            "total": 5,
        },
        "rows": [{"row_no": 3, "raw": ["Supplier", "N-2", "2026-05-11", "100", "10", "110"]}],
        "direction_default": "AP",
    }
    response = client.post(
        "/api/v1/invoices/import-excel-rows", headers=headers, json=explicit
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == []
    assert "not GST-registered" in response.json()["skipped"][0]["reason"]


def test_non_gst_bank_reconciliation_import_and_reports(client):
    headers = _company(client, "nogstbank", gst_registered=False)
    accounts = _accounts(client, headers)
    bank = _bank(client, headers)

    positive = {
        "direction": "in",
        "amount": "110.00",
        "occurred_at": "2026-05-10",
        "account_id": accounts["4000"]["id"],
        "tax_code": "standard",
        "gst_amount": "10.00",
    }
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(headers),
        json=positive,
    )
    assert response.status_code in (400, 422), response.text
    assert "not GST-registered" in response.text
    zero = {**positive, "account_id": None, "gst_amount": "0"}
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(headers),
        json=zero,
    )
    assert response.status_code == 201, response.text
    txn = response.json()
    assert txn["tax_code"] == "none"
    response = client.patch(
        f"/api/v1/bank-accounts/transactions/{txn['id']}/categorise",
        headers=headers,
        json={
            "account_id": accounts["4000"]["id"],
            "tax_code": "standard",
            "gst_amount": "10.00",
        },
    )
    assert response.status_code in (400, 422), response.text
    assert "not GST-registered" in response.text
    response = client.patch(
        f"/api/v1/bank-accounts/transactions/{txn['id']}/categorise",
        headers=headers,
        json={
            "account_id": accounts["4000"]["id"],
            "tax_code": "standard",
            "gst_amount": "0",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["tax_code"] == "none"

    csv_bytes = b"Date,Description,Debit,Credit\n2026-05-12,Electricity,110.00,\n"
    preview = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/import/preview",
        headers=headers,
        files={"file": ("statement.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    row = preview.json()["rows"][0]
    assert row["suggested_tax_code"] == "none"
    assert row["suggested_gst_amount"] == "0.00"

    commit_row = {
        "occurred_at": row["parsed"]["occurred_at"],
        "direction": row["parsed"]["direction"],
        "amount": row["parsed"]["amount"],
        "dedup_key": row["dedup_key"],
        "account_id": row["suggested_account_id"],
        "tax_code": "standard",
        "gst_amount": "10.00",
    }
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/import/commit",
        headers=headers,
        json={"rows": [commit_row]},
    )
    assert response.status_code == 400, response.text
    assert "not GST-registered" in response.text

    bas = client.get(
        "/api/v1/reports/bas",
        headers=headers,
        params={"fy_year": 2026, "quarter": 4},
    ).json()
    exposure = client.get(
        "/api/v1/reports/gst-exposure",
        headers=headers,
        params={"fy_year": 2026, "quarter": 4},
    ).json()
    assert bas["gst_registered"] is False
    assert exposure["gst_registered"] is False
    for field in (
        "g1_total_sales",
        "one_a_gst_on_sales",
        "one_b_gst_on_purchases",
        "net_gst_payable",
    ):
        assert Decimal(bas[field]) == 0
    assert all(
        Decimal(exposure[field]) == 0
        for field in (
            "g1_total_sales",
            "g10_capital_purchases",
            "g11_non_capital_purchases",
            "g14_gst_free_purchases",
            "one_a_gst_on_sales",
            "one_b_gst_on_purchases",
            "net_gst_payable",
        )
    )

    # Enabling GST later must not retroactively pull transactions recorded
    # while unregistered into a current/historical BAS period.
    response = client.patch(
        "/api/v1/companies/nogstbank",
        headers=headers,
        json={"gst_registered": True},
    )
    assert response.status_code == 200, response.text
    exposure_after_registration = client.get(
        "/api/v1/reports/gst-exposure",
        headers=headers,
        params={"fy_year": 2026, "quarter": 4},
    ).json()
    assert exposure_after_registration["gst_registered"] is True
    assert Decimal(exposure_after_registration["g1_total_sales"]) == 0
    assert Decimal(exposure_after_registration["one_a_gst_on_sales"]) == 0


def test_gst_policy_uses_profile_refreshed_inside_lifecycle_lock(
    client, monkeypatch
):
    """A request that read GST=true before waiting must not write GST after
    a concurrent profile update commits GST=false under the lifecycle lock.
    """
    headers = _company(client, "gstrace", gst_registered=True)
    accounts = _accounts(client, headers)
    bank = _bank(client, headers)

    from app import deps

    reached_stale_snapshot = Event()
    resume_financial_request = Event()
    real_lifecycle_lock = deps.company_lifecycle_lock

    @contextmanager
    def paused_before_lock(company_id):
        reached_stale_snapshot.set()
        assert resume_financial_request.wait(timeout=10)
        with real_lifecycle_lock(company_id):
            yield

    monkeypatch.setattr(deps, "company_lifecycle_lock", paused_before_lock)

    payload = {
        "direction": "in",
        "amount": "110.00",
        "occurred_at": "2026-05-10",
        "account_id": accounts["4000"]["id"],
        "tax_code": "standard",
        "gst_amount": "10.00",
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            client.post,
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=manual_transaction_headers(headers),
            json=payload,
        )
        assert reached_stale_snapshot.wait(timeout=10)

        changed = client.patch(
            "/api/v1/companies/gstrace",
            headers=headers,
            json={"gst_registered": False},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["gst_registered"] is False

        resume_financial_request.set()
        rejected = pending.result(timeout=10)

    assert rejected.status_code in (400, 422), rejected.text
    assert "not GST-registered" in rejected.text
    listed = client.get(
        f"/api/v1/bank-accounts/{bank['id']}/transactions", headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() == []


def test_outgoing_and_registered_company_control(client):
    no_gst = _company(client, "nogstout", gst_registered=False)
    client_row = client.post(
        "/api/v1/clients", headers=no_gst, json={"display_name": "Client"}
    ).json()
    receipt = client.post(
        "/api/v1/outgoing",
        headers=no_gst,
        json={
            "issue_date": "2026-05-10",
            "client_ref_id": client_row["id"],
            "lines": [{"description": "Service", "quantity": "1", "unit_price": "100"}],
        },
    )
    assert receipt.status_code == 201, receipt.text
    assert Decimal(receipt.json()["gst_amount"]) == 0
    assert Decimal(receipt.json()["total"]) == Decimal("100.00")

    registered = _company(client, "gstcontrol", gst_registered=True)
    accounts = _accounts(client, registered)
    bank = _bank(client, registered)
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(registered),
        json={
            "direction": "in",
            "amount": "110.00",
            "occurred_at": "2026-05-10",
            "account_id": accounts["4000"]["id"],
            "tax_code": "standard",
            "gst_amount": "10.00",
        },
    )
    assert response.status_code == 201, response.text
    bas = client.get(
        "/api/v1/reports/bas",
        headers=registered,
        params={"fy_year": 2026, "quarter": 4},
    ).json()
    assert Decimal(bas["one_a_gst_on_sales"]) == Decimal("10.00")


def test_registered_to_non_registered_refuses_historical_gst(client):
    headers = _company(client, "gstdowngrade", gst_registered=True)
    accounts = _accounts(client, headers)
    bank = _bank(client, headers)
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(headers),
        json={
            "direction": "in",
            "amount": "110.00",
            "occurred_at": "2026-05-10",
            "account_id": accounts["4000"]["id"],
            "tax_code": "standard",
            "gst_amount": "10.00",
        },
    )
    assert response.status_code == 201, response.text
    response = client.patch(
        "/api/v1/companies/gstdowngrade",
        headers=headers,
        json={"gst_registered": False},
    )
    assert response.status_code == 409, response.text
    assert "historical GST" in response.text
    company = client.get("/api/v1/companies/gstdowngrade", headers=headers).json()
    assert company["gst_registered"] is True

    empty_headers = _company(client, "gstempty", gst_registered=True)
    response = client.patch(
        "/api/v1/companies/gstempty",
        headers=empty_headers,
        json={"gst_registered": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["gst_registered"] is False


def test_non_gst_manual_journal_rejects_gst_control_accounts(client):
    headers = _company(client, "nogstjournal", gst_registered=False)
    accounts = _accounts(client, headers)

    normal_lines = [
        {"account_id": accounts["6400"]["id"], "debit_amount": "100.00"},
        {"account_id": accounts["3000"]["id"], "credit_amount": "100.00"},
    ]
    created = client.post(
        "/api/v1/journal",
        headers=headers,
        json={
            "entry_date": "2026-05-10",
            "memo": "Gross expense adjustment",
            "lines": normal_lines,
        },
    )
    assert created.status_code == 201, created.text

    gst_lines = [
        {"account_id": accounts["1200"]["id"], "debit_amount": "10.00"},
        {"account_id": accounts["3000"]["id"], "credit_amount": "10.00"},
    ]
    rejected = client.post(
        "/api/v1/journal",
        headers=headers,
        json={
            "entry_date": "2026-05-10",
            "memo": "Invented GST",
            "lines": gst_lines,
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert "not GST-registered" in rejected.text

    rejected_update = client.patch(
        f"/api/v1/journal/{created.json()['id']}",
        headers=headers,
        json={"lines": gst_lines},
    )
    assert rejected_update.status_code == 400, rejected_update.text
    assert "not GST-registered" in rejected_update.text
