from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _request_headers import manual_transaction_headers


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def api_client(monkeypatch, request):
    test_root = (PROJECT_ROOT / "tmp" / "tests").resolve()
    data_dir = (test_root / f"date_controls_{request.node.name}").resolve()
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
            response = client.post(
                "/api/v1/companies",
                json={
                    "id": "tc",
                    "name": "Date Controls Pty Ltd",
                    "gst_registered": True,
                },
            )
            assert response.status_code == 201, response.text
            headers = {
                "X-Company-Id": "tc",
                "X-Company-Generation": response.json()["generation_id"],
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

        dispose_company_engine("tc")
        master_engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)


def _invoice_payload(
    accounts,
    *,
    issue_date: str,
    number: str,
    direction: str = "AP",
):
    line_code = "6100" if direction == "AP" else "4000"
    return {
        "direction": direction,
        "contact_name": f"{number} Contact",
        "invoice_number": number,
        "issue_date": issue_date,
        "due_date": issue_date,
        "subtotal": "10.00",
        "gst_amount": "1.00",
        "total": "11.00",
        "lines": [
            {
                "description": number,
                "account_id": accounts[line_code]["id"],
                "line_subtotal": "10.00",
                "line_gst": "1.00",
                "line_total": "11.00",
            }
        ],
    }


def test_future_activity_stays_out_of_current_surfaces_but_reports_as_of_future(
    api_client, monkeypatch
):
    client, headers, accounts, bank = api_client
    frozen_today = date(2026, 7, 12)
    tomorrow = date(2026, 7, 13)

    from app.api.v1 import bank_accounts as bank_accounts_api
    from app.services import dashboard as dashboard_service

    monkeypatch.setattr(bank_accounts_api, "current_date", lambda: frozen_today)
    monkeypatch.setattr(dashboard_service, "current_date", lambda: frozen_today)

    transaction = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=manual_transaction_headers(headers),
        json={
            "direction": "in",
            "amount": "11.00",
            "occurred_at": tomorrow.isoformat(),
            "account_id": accounts["4000"]["id"],
            "gst_amount": "1.00",
            "tax_code": "standard",
            "memo": "Scheduled receipt",
        },
    )
    assert transaction.status_code == 201, transaction.text

    created = client.post(
        "/api/v1/invoices",
        headers=headers,
        json=_invoice_payload(
            accounts,
            issue_date=tomorrow.isoformat(),
            number="AP-TOMORROW",
        ),
    )
    assert created.status_code == 201, created.text
    posted = client.post(
        f"/api/v1/invoices/{created.json()['id']}/post",
        headers=headers,
    )
    assert posted.status_code == 200, posted.text

    current_bank = client.get("/api/v1/bank-accounts", headers=headers).json()[0]
    assert current_bank["current_balance"] == "0.00"

    dashboard = client.get("/api/v1/dashboard/summary", headers=headers).json()
    assert dashboard["as_of"] == frozen_today.isoformat()
    assert dashboard["business_total"] == "0.00"
    assert dashboard["unpaid_ap_total"] == "0.00"
    assert dashboard["recent_business_txns"] == []

    statement = client.get(
        "/api/v1/reports/bank-statement",
        headers=headers,
        params={
            "bank_account_id": bank["id"],
            "year": tomorrow.year,
            "month": tomorrow.month,
        },
    )
    assert statement.status_code == 200, statement.text
    assert statement.json()["closing_balance"] == "11.00"
    assert [row["memo"] for row in statement.json()["rows"]] == [
        "Scheduled receipt"
    ]

    pnl = client.get(
        "/api/v1/reports/profit-loss",
        headers=headers,
        params={
            "period_start": tomorrow.isoformat(),
            "period_end": tomorrow.isoformat(),
        },
    )
    assert pnl.status_code == 200, pnl.text
    assert pnl.json()["total_income"] == "10.00"
    assert pnl.json()["total_expense"] == "10.00"

    trial_balance = client.get(
        "/api/v1/reports/trial-balance",
        headers=headers,
        params={"as_of": tomorrow.isoformat()},
    )
    assert trial_balance.status_code == 200, trial_balance.text
    assert trial_balance.json()["is_balanced"] is True
    assert trial_balance.json()["total_debit"] != "0.00"


@pytest.mark.parametrize("invalid_date", ["1999-06-30", "2100-07-01"])
def test_invoice_create_rejects_dates_outside_reportable_window(
    api_client, invalid_date
):
    client, headers, accounts, _ = api_client
    response = client.post(
        "/api/v1/invoices",
        headers=headers,
        json=_invoice_payload(
            accounts,
            issue_date=invalid_date,
            number=f"OUTSIDE-{invalid_date}",
        ),
    )
    assert response.status_code == 422, response.text
    assert "issue_date must be between 1999-07-01 and 2100-06-30" in response.text


@pytest.mark.parametrize("boundary_date", ["1999-07-01", "2100-06-30"])
def test_invoice_create_allows_reportable_date_boundaries(
    api_client, boundary_date
):
    client, headers, accounts, _ = api_client
    response = client.post(
        "/api/v1/invoices",
        headers=headers,
        json=_invoice_payload(
            accounts,
            issue_date=boundary_date,
            number=f"BOUNDARY-{boundary_date}",
        ),
    )
    assert response.status_code == 201, response.text


def test_invoice_update_and_legacy_post_fail_cleanly_outside_date_window(
    api_client,
):
    client, headers, accounts, _ = api_client
    created = client.post(
        "/api/v1/invoices",
        headers=headers,
        json=_invoice_payload(
            accounts,
            issue_date="2100-06-30",
            number="LEGACY-DATE",
            direction="AR",
        ),
    )
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]

    update = client.patch(
        f"/api/v1/invoices/{invoice_id}",
        headers=headers,
        json={"issue_date": "2100-07-01"},
    )
    assert update.status_code == 422, update.text

    from app.db.company import company_session
    from app.models.company import Invoice, InvoiceStatus, JournalEntry

    with company_session("tc") as db:
        legacy = db.get(Invoice, invoice_id)
        legacy.issue_date = date(2100, 7, 1)
        db.commit()

    posting = client.post(
        f"/api/v1/invoices/{invoice_id}/post",
        headers=headers,
    )
    assert posting.status_code == 422, posting.text
    assert "issue_date must be between 1999-07-01 and 2100-06-30" in posting.text

    with company_session("tc") as db:
        legacy = db.get(Invoice, invoice_id)
        assert legacy.status == InvoiceStatus.DRAFT
        assert (
            db.query(JournalEntry)
            .filter(JournalEntry.source_id == invoice_id)
            .count()
            == 0
        )
