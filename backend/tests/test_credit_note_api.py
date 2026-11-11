from __future__ import annotations

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

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
    from app.main import app

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


def _source_line(accounts, *, tax_code="standard", quantity="2", unit_price="100.00", account_code="4000", mode="exclusive"):
    quantity_d = Decimal(quantity)
    unit_price_d = Decimal(unit_price)
    extended = (quantity_d * unit_price_d).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if tax_code in {"gst_free", "input_taxed", "none"}:
        subtotal, gst, total = extended, Decimal("0.00"), extended
        gst_rate = "0.0000"
    elif mode == "inclusive":
        total = extended
        gst = (total / Decimal("11")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        subtotal = total - gst
        gst_rate = "0.1000"
    else:
        subtotal = extended
        gst = (subtotal * Decimal("0.10")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        total = subtotal + gst
        gst_rate = "0.1000"
    return {
        "description": f"Fictional {tax_code} source item",
        "account_id": accounts[account_code]["id"],
        "quantity": quantity,
        "unit_price": unit_price,
        "gst_rate": gst_rate,
        "line_subtotal": f"{subtotal:.2f}",
        "line_gst": f"{gst:.2f}",
        "line_total": f"{total:.2f}",
        "tax_code": tax_code,
    }


def _create_source(
    client,
    accounts,
    *,
    number="FICTIONAL-SOURCE-1",
    direction="AR",
    contact_name="Fictional Customer",
    mode="exclusive",
    lines=None,
):
    if lines is None:
        lines = [_source_line(accounts, mode=mode)]
    subtotal = sum((Decimal(line["line_subtotal"]) for line in lines), Decimal("0"))
    gst = sum((Decimal(line["line_gst"]) for line in lines), Decimal("0"))
    total = sum((Decimal(line["line_total"]) for line in lines), Decimal("0"))
    payload = {
        "direction": direction,
        "contact_name": contact_name,
        "invoice_number": number,
        "issue_date": "2026-05-31",
        "subtotal": f"{subtotal:.2f}",
        "gst_amount": f"{gst:.2f}",
        "total": f"{total:.2f}",
        "gst_inclusive": mode == "inclusive",
        "amount_mode": mode,
        "lines": lines,
    }
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text
    invoice = response.json()
    posted = client.post(f"/api/v1/invoices/{invoice['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    posted_invoice = posted.json()["invoice"]
    snapshot = _get_source_snapshot(client, posted_invoice["id"])
    return {**posted_invoice, "lines": snapshot["lines"]}


def _get_source_snapshot(client, invoice_id):
    response = client.get(
        f"/api/v1/credit-notes/source-invoices/{invoice_id}", headers=HEAD
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_credit_note(
    client,
    invoice,
    *,
    number="FICTIONAL-CN-1",
    lines=None,
    **overrides,
):
    if lines is None:
        lines = [
            {
                "source_invoice_line_id": invoice["lines"][0]["source_invoice_line_id"],
                "quantity": "1.0000",
            }
        ]
    payload = {
        "source_invoice_id": invoice["id"],
        "credit_note_number": number,
        "issue_date": "2026-06-01",
        "lines": lines,
        **overrides,
    }
    return client.post("/api/v1/credit-notes", headers=HEAD, json=payload)


def _journal_entries(client):
    response = client.get("/api/v1/journal/entries", headers=HEAD)
    assert response.status_code == 200, response.text
    return response.json()


def test_credit_note_application_and_reversal_are_reconciled(client, accounts):
    source = _create_source(client, accounts, number="APP-AR-SOURCE")
    credit_note = _create_credit_note(client, source, number="APP-AR-CREDIT")
    assert credit_note.status_code == 201, credit_note.text
    posted_credit = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/post",
        headers=HEAD,
    )
    assert posted_credit.status_code == 200, posted_credit.text

    application = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/applications",
        headers={**HEAD, "Idempotency-Key": "application-1"},
        json={
            "invoice_id": source["id"],
            "amount": "110.00",
            "application_date": "2026-06-30",
        },
    )
    assert application.status_code == 201, application.text
    assert application.json()["status"] == "active"
    invoice = client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json()
    assert Decimal(invoice["credit_applied_amount"]) == Decimal("110.00")
    assert Decimal(invoice["outstanding_amount"]) == Decimal("110.00")

    reversed_application = client.post(
        f"/api/v1/credit-note-applications/{application.json()['id']}/reverse",
        headers=HEAD,
        json={"reversal_date": "2026-07-01"},
    )
    assert reversed_application.status_code == 200, reversed_application.text
    assert reversed_application.json()["status"] == "reversed"
    invoice = client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json()
    assert Decimal(invoice["credit_applied_amount"]) == Decimal("0.00")
    assert Decimal(invoice["outstanding_amount"]) == Decimal("220.00")


def test_concurrent_applications_to_different_invoices_are_atomic(client, accounts):
    from app.db.company import company_session
    from app.services import credit_notes as credit_note_service

    first_source = _create_source(client, accounts, number="CONCURRENT-DIFFERENT-1")
    second_source = _create_source(client, accounts, number="CONCURRENT-DIFFERENT-2")
    credit_note = _create_credit_note(
        client, first_source, number="CONCURRENT-DIFFERENT-CN"
    )
    assert credit_note.status_code == 201, credit_note.text
    posted_credit = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/post", headers=HEAD
    )
    assert posted_credit.status_code == 200, posted_credit.text
    credit_note_id = credit_note.json()["id"]
    amount = min(Decimal("60.00"), Decimal(credit_note.json()["remaining_amount"]))

    def apply(key, invoice_id):
        response = client.post(
            f"/api/v1/credit-notes/{credit_note_id}/applications",
            headers={**HEAD, "Idempotency-Key": key},
            json={
                "invoice_id": invoice_id,
                "amount": f"{amount:.2f}",
                "application_date": "2026-06-30",
            },
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                apply,
                ("concurrent-different-a", "concurrent-different-b"),
                (first_source["id"], second_source["id"]),
            )
        )

    assert sorted(status for status, _ in results) == [201, 201]
    with company_session("tc") as session:
        assert session.query(credit_note_service.CreditNoteApplication).filter_by(
            credit_note_id=credit_note_id
        ).count() == 2


def test_concurrent_applications_to_same_invoice_are_atomic(client, accounts):
    from app.db.company import company_session
    from app.services import credit_notes as credit_note_service

    source = _create_source(client, accounts, number="CONCURRENT-SAME-SOURCE")
    credit_note = _create_credit_note(client, source, number="CONCURRENT-SAME-CN")
    assert credit_note.status_code == 201, credit_note.text
    posted_credit = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/post", headers=HEAD
    )
    assert posted_credit.status_code == 200, posted_credit.text
    credit_note_id = credit_note.json()["id"]
    amount = min(Decimal("60.00"), Decimal(credit_note.json()["remaining_amount"]))

    def apply(key):
        response = client.post(
            f"/api/v1/credit-notes/{credit_note_id}/applications",
            headers={**HEAD, "Idempotency-Key": key},
            json={
                "invoice_id": source["id"],
                "amount": f"{amount:.2f}",
                "application_date": "2026-06-30",
            },
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, ("concurrent-same-a", "concurrent-same-b")))

    assert sorted(status for status, _ in results) == [201, 409]
    with company_session("tc") as session:
        assert session.query(credit_note_service.CreditNoteApplication).filter_by(
            credit_note_id=credit_note_id
        ).count() == 1


def test_exact_boundary_application_and_no_partial_state(client, accounts):
    from app.db.company import company_session
    from app.services import credit_notes as credit_note_service

    source = _create_source(client, accounts, number="BOUNDARY-SOURCE")
    credit_note = _create_credit_note(client, source, number="BOUNDARY-CN")
    assert credit_note.status_code == 201, credit_note.text
    posted_credit = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/post", headers=HEAD
    )
    assert posted_credit.status_code == 200, posted_credit.text
    remaining = Decimal(credit_note.json()["remaining_amount"])

    boundary = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/applications",
        headers={**HEAD, "Idempotency-Key": "boundary-exact"},
        json={
            "invoice_id": source["id"],
            "amount": f"{remaining:.2f}",
            "application_date": "2026-06-30",
        },
    )
    assert boundary.status_code == 201, boundary.text
    rejected = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/applications",
        headers={**HEAD, "Idempotency-Key": "boundary-rejected"},
        json={
            "invoice_id": source["id"],
            "amount": "0.01",
            "application_date": "2026-06-30",
        },
    )
    assert rejected.status_code == 409, rejected.text
    with company_session("tc") as session:
        assert session.query(credit_note_service.CreditNoteApplication).filter_by(
            credit_note_id=credit_note.json()["id"]
        ).count() == 1


def test_application_rejects_target_without_verified_invoice_journal(client, accounts):
    from app.db.company import company_session
    from app.models.company import JournalEntry, JournalEntrySource

    source = _create_source(client, accounts, number="PROVENANCE-SOURCE")
    credit_note = _create_credit_note(client, source, number="PROVENANCE-CN")
    assert credit_note.status_code == 201, credit_note.text
    posted_credit = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/post", headers=HEAD
    )
    assert posted_credit.status_code == 200, posted_credit.text
    with company_session("tc") as session:
        entry = (
            session.query(JournalEntry)
            .filter_by(source_id=source["id"], source_type=JournalEntrySource.INVOICE_AR)
            .one()
        )
        session.delete(entry)
        session.commit()

    response = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/applications",
        headers={**HEAD, "Idempotency-Key": "missing-provenance"},
        json={
            "invoice_id": source["id"],
            "amount": "10.00",
            "application_date": "2026-06-30",
        },
    )
    assert response.status_code == 409, response.text


def test_application_and_refund_dates_are_reportable(client, accounts):
    source = _create_source(client, accounts, number="DATE-SOURCE")
    credit_note = _create_credit_note(client, source, number="DATE-CN")
    assert credit_note.status_code == 201, credit_note.text
    posted_credit = client.post(
        f"/api/v1/credit-notes/{credit_note.json()['id']}/post", headers=HEAD
    )
    assert posted_credit.status_code == 200, posted_credit.text

    for field, value in (
        ("application_date", "1999-06-30"),
        ("refund_date", "2100-07-01"),
        ("reversal_date", "1999-06-30"),
    ):
        endpoint = (
            f"/api/v1/credit-notes/{credit_note.json()['id']}/applications"
            if field == "application_date"
            else f"/api/v1/credit-notes/{credit_note.json()['id']}/refunds"
            if field == "refund_date"
            else f"/api/v1/credit-note-applications/999/reverse"
        )
        body = {
            "invoice_id": source["id"],
            "amount": "10.00",
            field: value,
        }
        if field == "reversal_date":
            body = {field: value}
        response = client.post(
            endpoint,
            headers={**HEAD, "Idempotency-Key": f"invalid-date-{field}"},
            json=body,
        )
        assert response.status_code == 422, (field, response.text)


def test_source_snapshot_lifecycle_and_ar_ap_sources(client, accounts):
    missing = client.get("/api/v1/credit-notes/source-invoices/999", headers=HEAD)
    assert missing.status_code == 404

    draft_payload = {
        "direction": "AR",
        "contact_name": "Fictional Draft Customer",
        "invoice_number": "FICTIONAL-DRAFT-SOURCE",
        "issue_date": "2026-05-31",
        "subtotal": "200.00",
        "gst_amount": "20.00",
        "total": "220.00",
        "lines": [
            {
                "description": "Fictional draft item",
                "account_id": accounts["4000"]["id"],
                "quantity": "2",
                "unit_price": "100.00",
                "line_subtotal": "200.00",
                "line_gst": "20.00",
                "line_total": "220.00",
            }
        ],
    }
    draft = client.post("/api/v1/invoices", headers=HEAD, json=draft_payload)
    assert draft.status_code == 201, draft.text
    draft_snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{draft.json()['id']}", headers=HEAD
    )
    assert draft_snapshot.status_code == 409

    ar = _create_source(client, accounts, number="FICTIONAL-AR-SNAPSHOT")
    ap = _create_source(
        client,
        accounts,
        number="FICTIONAL-AP-SNAPSHOT",
        direction="AP",
        contact_name="Fictional Supplier",
        lines=[_source_line(accounts, account_code="6100")],
    )
    before_invoice = client.get(f"/api/v1/invoices/{ar['id']}", headers=HEAD).json()
    before_journals = _journal_entries(client)
    for invoice, direction in ((ar, "AR"), (ap, "AP")):
        source = _get_source_snapshot(client, invoice["id"])
        assert source["direction"] == direction
        assert source["source_invoice_id"] == invoice["id"]
        assert source["lines"][0]["source_invoice_line_id"] == invoice["lines"][0]["source_invoice_line_id"]
        assert source["lines"][0]["remaining_creditable_quantity"] == "2.0000"
    assert _journal_entries(client) == before_journals

    void_source = _create_source(client, accounts, number="FICTIONAL-VOID-SOURCE")
    voided = client.post(
        f"/api/v1/invoices/{void_source['id']}/void", headers=HEAD
    )
    assert voided.status_code == 200, voided.text
    void_snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{void_source['id']}", headers=HEAD
    )
    assert void_snapshot.status_code == 409
    assert client.get(f"/api/v1/invoices/{ar['id']}", headers=HEAD).json() == before_invoice


@pytest.mark.parametrize(
    ("direction", "account_code", "contact_kind"),
    [("AR", "4000", "customer"), ("AP", "6100", "supplier")],
)
def test_source_identity_uses_invoice_snapshot_for_draft_credit_notes(
    client, accounts, direction, account_code, contact_kind
):
    source = _create_source(
        client,
        accounts,
        number=f"FICTIONAL-SNAPSHOT-IDENTITY-{direction}",
        direction=direction,
        contact_name=f"Original {direction} Contact",
        lines=[_source_line(accounts, account_code=account_code)],
    )
    contact_id = source["contact_id"]
    renamed = client.patch(
        f"/api/v1/contacts/{contact_id}",
        headers=HEAD,
        json={"name": f"Renamed {direction} Contact"},
    )
    assert renamed.status_code == 200, renamed.text

    source_snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{source['id']}",
        headers=HEAD,
    )
    assert source_snapshot.status_code == 200, source_snapshot.text
    source_body = source_snapshot.json()
    assert source_body["contact_id"] == contact_id
    assert source_body["contact_name"] == f"Original {direction} Contact"

    created = _create_credit_note(client, source)
    assert created.status_code == 201, created.text
    note = created.json()
    assert note["direction"] == direction
    assert note["contact_id"] == contact_id
    assert note["contact_name"] == f"Original {direction} Contact"
    assert note["status"] == "draft"

    read = client.get(f"/api/v1/credit-notes/{note['id']}", headers=HEAD)
    assert read.status_code == 200, read.text
    assert read.json()["contact_name"] == f"Original {direction} Contact"
    assert read.json()["contact_id"] == contact_id


@pytest.mark.parametrize(
    ("mode", "tax_code", "direction", "account_code", "unit_price", "expected"),
    [
        ("exclusive", "standard", "AR", "4000", "100.00", ("100.00", "10.00", "110.00")),
        ("inclusive", "standard", "AR", "4000", "110.00", ("100.00", "10.00", "110.00")),
        ("exclusive", "gst_free", "AR", "4000", "10.00", ("10.00", "0.00", "10.00")),
        ("inclusive", "input_taxed", "AR", "4000", "10.00", ("10.00", "0.00", "10.00")),
        ("exclusive", "none", "AR", "4000", "10.00", ("10.00", "0.00", "10.00")),
        ("inclusive", "capital", "AP", "1700", "110.00", ("100.00", "10.00", "110.00")),
    ],
)
def test_authoritative_calculation_and_source_derivation(
    client, accounts, mode, tax_code, direction, account_code, unit_price, expected
):
    source_line = _source_line(
        accounts,
        tax_code=tax_code,
        quantity="2",
        unit_price=unit_price,
        account_code=account_code,
        mode=mode,
    )
    source = _create_source(
        client,
        accounts,
        number=f"FICTIONAL-{direction}-{tax_code}-{mode}",
        direction=direction,
        contact_name=f"Fictional {direction} {tax_code} Contact",
        mode=mode,
        lines=[source_line],
    )
    source_line_snapshot = source["lines"][0]
    before_invoice = client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json()
    before_journals = _journal_entries(client)
    response = _create_credit_note(client, source)
    assert response.status_code == 201, response.text
    note = response.json()
    line = note["lines"][0]
    assert (note["direction"], note["contact_id"]) == (
        direction,
        source["contact_id"],
    )
    assert note["status"] == "draft"
    assert note["currency"] == "AUD"
    assert (note["subtotal"], note["gst_amount"], note["total"]) == expected
    assert line["quantity"] == "1.0000"
    assert line["source_invoice_line_id"] == source_line_snapshot["source_invoice_line_id"]
    assert line["description"] == source_line_snapshot["description"]
    assert line["account_id"] == source_line_snapshot["account_id"]
    assert line["unit_price"] == source_line_snapshot["unit_price"]
    assert line["gst_rate"] == source_line_snapshot["gst_rate"]
    assert line["tax_code"] == source_line_snapshot["tax_code"]
    assert all(Decimal(value) > 0 for value in (line["quantity"], line["line_subtotal"], line["line_total"]))
    assert note["lines"][0]["line_gst"] == expected[1]
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == before_invoice
    assert _journal_entries(client) == before_journals


def test_multiple_tax_treatments_reserve_quantities_and_update_draft(client, accounts):
    source_lines = [
        _source_line(accounts, tax_code="standard", unit_price="10.00"),
        _source_line(accounts, tax_code="gst_free", unit_price="10.00", account_code="4010"),
        _source_line(accounts, tax_code="input_taxed", unit_price="10.00", account_code="4010"),
        _source_line(accounts, tax_code="none", unit_price="10.00", account_code="4010"),
    ]
    source = _create_source(
        client,
        accounts,
        number="FICTIONAL-MIXED-TAX-SOURCE",
        mode="exclusive",
        lines=source_lines,
    )
    requested = [
        {"source_invoice_line_id": line["source_invoice_line_id"], "quantity": "1.0000"}
        for line in source["lines"]
    ]
    created = _create_credit_note(client, source, number="FICTIONAL-MIXED-CN", lines=requested)
    assert created.status_code == 201, created.text
    note = created.json()
    assert [line["tax_code"] for line in note["lines"]] == [
        "standard", "gst_free", "input_taxed", "none"
    ]
    assert note["gst_amount"] == "1.00"
    snapshot = client.get(
        f"/api/v1/credit-notes/source-invoices/{source['id']}", headers=HEAD
    ).json()
    assert all(line["remaining_creditable_quantity"] == "1.0000" for line in snapshot["lines"])

    replacement = [
        {
            "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
            "quantity": "1.5000",
        }
    ]
    updated = client.patch(
        f"/api/v1/credit-notes/{note['id']}",
        headers=HEAD,
        json={
            "credit_note_number": "FICTIONAL-MIXED-CN-UPDATED",
            "issue_date": "2026-06-02",
            "notes": "Fictional draft note",
            "lines": replacement,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["credit_note_number"] == "FICTIONAL-MIXED-CN-UPDATED"
    assert updated.json()["notes"] == "Fictional draft note"
    assert updated.json()["lines"][0]["quantity"] == "1.5000"
    assert updated.json()["total"] == "16.50"
    assert updated.json()["gst_amount"] == "1.50"

    second = _create_credit_note(
        client,
        source,
        number="FICTIONAL-MIXED-CN-SECOND",
        lines=[{
            "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
            "quantity": "0.5000",
        }],
    )
    assert second.status_code == 201, second.text
    over_reserved = _create_credit_note(
        client,
        source,
        number="FICTIONAL-MIXED-CN-OVER",
        lines=[{
            "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
            "quantity": "0.0001",
        }],
    )
    assert over_reserved.status_code == 409


def test_validation_rejects_bad_quantities_lines_overrides_and_number_conflicts(client, accounts):
    source = _create_source(client, accounts, number="FICTIONAL-VALIDATION-SOURCE")
    other = _create_source(
        client,
        accounts,
        number="FICTIONAL-OTHER-SOURCE",
        contact_name="Fictional Other Customer",
    )
    line_id = source["lines"][0]["source_invoice_line_id"]

    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-ZERO-QTY",
        lines=[{"source_invoice_line_id": line_id, "quantity": "0"}],
    ).status_code == 422
    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-FOREIGN-LINE",
        lines=[{
            "source_invoice_line_id": other["lines"][0]["source_invoice_line_id"],
            "quantity": "1",
        }],
    ).status_code == 422
    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-DUPLICATE-LINE",
        lines=[
            {"source_invoice_line_id": line_id, "quantity": "0.5"},
            {"source_invoice_line_id": line_id, "quantity": "0.5"},
        ],
    ).status_code == 409
    assert _create_credit_note(
        client,
        source,
        number="FICTIONAL-OVER-QTY",
        lines=[{"source_invoice_line_id": line_id, "quantity": "2.0001"}],
    ).status_code == 409
    forbidden_header_fields = {
        "direction": "AP",
        "contact_id": source["contact_id"],
        "currency": "USD",
        "gst_inclusive": False,
        "subtotal": "1.00",
        "gst_amount": "0.00",
        "total": "1.00",
        "status": "draft",
    }
    for index, (field, value) in enumerate(forbidden_header_fields.items()):
        response = _create_credit_note(
            client,
            source,
            number=f"FICTIONAL-HEADER-OVERRIDE-{index}",
            **{field: value},
        )
        assert response.status_code == 422, (field, response.text)
    forbidden_line_fields = {
        "account_id": source["lines"][0]["account_id"],
        "description": "Caller supplied description",
        "unit_price": "1.00",
        "gst_rate": "0.0000",
        "tax_code": "none",
        "line_subtotal": "1.00",
        "line_gst": "0.00",
        "line_total": "1.00",
    }
    for index, (field, value) in enumerate(forbidden_line_fields.items()):
        response = _create_credit_note(
            client,
            source,
            number=f"FICTIONAL-LINE-OVERRIDE-{index}",
            lines=[
                {
                    "source_invoice_line_id": line_id,
                    "quantity": "1",
                    field: value,
                }
            ],
        )
        assert response.status_code == 422, (field, response.text)
    assert _create_credit_note(client, source, number="FICTIONAL-DUP-NUMBER").status_code == 201
    assert _create_credit_note(client, source, number="FICTIONAL-DUP-NUMBER").status_code == 409

    same_number_ap = _create_source(
        client,
        accounts,
        number="FICTIONAL-SAME-NUMBER-AP-SOURCE",
        direction="AP",
        contact_name="Fictional Customer",
        lines=[_source_line(accounts, account_code="6100")],
    )
    permitted_other_direction = _create_credit_note(
        client,
        same_number_ap,
        number="FICTIONAL-DUP-NUMBER",
    )
    assert permitted_other_direction.status_code == 201, permitted_other_direction.text

    assert client.patch(
        f"/api/v1/credit-notes/{permitted_other_direction.json()['id']}",
        headers=HEAD,
        json={"status": "authorised"},
    ).status_code == 422
    assert client.patch(
        f"/api/v1/credit-notes/{permitted_other_direction.json()['id']}",
        headers=HEAD,
        json={"source_invoice_id": source["id"]},
    ).status_code == 422
    assert client.patch(
        f"/api/v1/credit-notes/{permitted_other_direction.json()['id']}",
        headers=HEAD,
        json={"unit_price": "1.00"},
    ).status_code == 422


def test_source_rejects_positive_paid_amount_and_explicit_allocation(client, accounts):
    from app.db.company import company_session
    from app.models.company import Invoice

    paid_source = _create_source(client, accounts, number="FICTIONAL-PAID-SOURCE")
    with company_session("tc") as db:
        invoice = db.get(Invoice, paid_source["id"])
        invoice.paid_amount = Decimal("1.00")
        db.commit()
    paid = client.get(
        f"/api/v1/credit-notes/source-invoices/{paid_source['id']}", headers=HEAD
    )
    assert paid.status_code == 409

    allocated_source = _create_source(client, accounts, number="FICTIONAL-ALLOCATED-SOURCE")
    bank_accounts = client.get("/api/v1/bank-accounts", headers=HEAD)
    assert bank_accounts.status_code == 200, bank_accounts.text
    bank_account_id = bank_accounts.json()[0]["id"]
    transaction = client.post(
        f"/api/v1/bank-accounts/{bank_account_id}/transactions",
        headers={**HEAD, "Idempotency-Key": "fictional-credit-note-settlement"},
        json={
            "direction": "in",
            "amount": "1.00",
            "occurred_at": "2026-06-01",
            "memo": "Fictional settlement evidence",
            "counter_party_name": "Fictional Customer",
            "account_id": accounts["1100"]["id"],
            "gst_amount": "0.00",
            "tax_code": "none",
            "invoice_allocations": [
                {"invoice_id": allocated_source["id"], "amount": "1.00"}
            ],
        },
    )
    assert transaction.status_code == 201, transaction.text
    allocated = client.get(
        f"/api/v1/credit-notes/source-invoices/{allocated_source['id']}", headers=HEAD
    )
    assert allocated.status_code == 409


def test_draft_delete_and_no_accounting_or_bas_effect(client, accounts):
    source = _create_source(client, accounts, number="FICTIONAL-NO-EFFECT-SOURCE")
    invoice_before = client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json()
    journals_before = _journal_entries(client)
    bas_before = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_before.status_code == 200, bas_before.text

    created = _create_credit_note(client, source, number="FICTIONAL-NO-EFFECT-CN")
    assert created.status_code == 201, created.text
    assert _journal_entries(client) == journals_before
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == invoice_before
    bas_created = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_created.status_code == 200, bas_created.text
    assert bas_created.json() == bas_before.json()
    updated = client.patch(
        f"/api/v1/credit-notes/{created.json()['id']}",
        headers=HEAD,
        json={"notes": "Still a draft"},
    )
    assert updated.status_code == 200, updated.text
    assert _journal_entries(client) == journals_before
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == invoice_before
    bas_updated = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_updated.status_code == 200, bas_updated.text
    assert bas_updated.json() == bas_before.json()
    deleted = client.delete(
        f"/api/v1/credit-notes/{created.json()['id']}", headers=HEAD
    )
    assert deleted.status_code == 204
    assert client.delete("/api/v1/credit-notes/999", headers=HEAD).status_code == 404
    assert client.get(f"/api/v1/invoices/{source['id']}", headers=HEAD).json() == invoice_before
    assert _journal_entries(client) == journals_before
    from app.db.company import company_session
    from app.models.company import Contact, CreditNote, CreditNoteLine, InvoiceLine

    with company_session("tc") as db:
        assert db.query(CreditNote).count() == 0
        assert db.query(CreditNoteLine).count() == 0
        assert db.query(InvoiceLine).filter_by(invoice_id=source["id"]).count() == 1
        assert db.get(Contact, source["contact_id"]) is not None
    bas_after = client.get(
        "/api/v1/reports/bas?fy_year=2026&quarter=4", headers=HEAD
    )
    assert bas_after.status_code == 200, bas_after.text
    assert bas_after.json() == bas_before.json()


def test_startup_creates_credit_note_tables_and_named_constraints(client):
    from app.db.company import get_company_engine

    database = inspect(get_company_engine("tc"))
    assert "credit_notes" in database.get_table_names()
    assert "credit_note_lines" in database.get_table_names()
    header_unique = database.get_unique_constraints("credit_notes")
    assert any(
        constraint["name"] == "uq_credit_note_dir_contact_no"
        for constraint in header_unique
    )
    line_unique = database.get_unique_constraints("credit_note_lines")
    assert any(
        constraint["name"] == "uq_credit_note_line_note_source_line"
        and constraint["column_names"] == ["credit_note_id", "source_invoice_line_id"]
        for constraint in line_unique
    )
    header_checks = {
        constraint["name"] for constraint in database.get_check_constraints("credit_notes")
    }
    line_checks = {
        constraint["name"] for constraint in database.get_check_constraints("credit_note_lines")
    }
    assert {
        "ck_credit_note_direction",
        "ck_credit_note_currency_aud",
        "ck_credit_note_status_allowed",
        "ck_credit_note_subtotal_nonneg",
        "ck_credit_note_gst_nonneg",
        "ck_credit_note_total_nonneg",
        "ck_credit_note_total_positive",
    } <= header_checks
    status_check = next(
        constraint
        for constraint in database.get_check_constraints("credit_notes")
        if constraint["name"] == "ck_credit_note_status_allowed"
    )
    assert status_check["sqltext"] == "status IN ('draft', 'authorised', 'void')"
    assert {
        "ck_credit_note_line_quantity_positive",
        "ck_credit_note_line_unit_price_nonneg",
        "ck_credit_note_line_subtotal_nonneg",
        "ck_credit_note_line_gst_nonneg",
        "ck_credit_note_line_total_nonneg",
        "ck_credit_note_line_subtotal_positive",
        "ck_credit_note_line_total_positive",
        "ck_credit_note_line_gst_within",
        "ck_credit_note_line_tax_code",
    } <= line_checks
    line_columns = {
        column["name"]: column for column in database.get_columns("credit_note_lines")
    }
    assert line_columns["account_id"]["nullable"] is False
    header_fks = database.get_foreign_keys("credit_notes")
    assert any(
        foreign_key["referred_table"] == "invoices"
        and foreign_key["options"].get("ondelete") == "RESTRICT"
        for foreign_key in header_fks
    )
    line_fks = database.get_foreign_keys("credit_note_lines")
    assert any(
        foreign_key["referred_table"] == "credit_notes"
        and foreign_key["constrained_columns"] == ["credit_note_id"]
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in line_fks
    )
    assert any(
        foreign_key["referred_table"] == "invoice_lines"
        and foreign_key["constrained_columns"] == ["source_invoice_line_id"]
        and foreign_key["options"].get("ondelete") == "RESTRICT"
        for foreign_key in line_fks
    )


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (
            "AR",
            {
                "4000": ("100.00", "0.00"),
                "4010": ("100.00", "0.00"),
                "2100": ("10.00", "0.00"),
                "1100": ("0.00", "210.00"),
            },
        ),
        (
            "AP",
            {
                "2000": ("220.00", "0.00"),
                "6100": ("0.00", "100.00"),
                "1700": ("0.00", "100.00"),
                "1200": ("0.00", "20.00"),
            },
        ),
    ],
)
def test_post_creates_exact_mixed_ar_ap_journal(
    client, accounts, direction, expected
):
    source_lines = (
        [
            _source_line(accounts, tax_code="standard", account_code="4000"),
            _source_line(accounts, tax_code="none", account_code="4010"),
        ]
        if direction == "AR"
        else [
            _source_line(accounts, tax_code="standard", account_code="6100"),
            _source_line(accounts, tax_code="capital", account_code="1700"),
        ]
    )

    source = _create_source(
        client,
        accounts,
        number=f"FICTIONAL-POST-{direction}",
        direction=direction,
        contact_name=f"Fictional {direction} Posting Contact",
        lines=source_lines,
    )
    created = _create_credit_note(
        client,
        source,
        number=f"FICTIONAL-POST-CN-{direction}",
        lines=[
            {"source_invoice_line_id": line["source_invoice_line_id"], "quantity": "1"}
            for line in source["lines"]
        ],
    )
    assert created.status_code == 201, created.text
    note = created.json()

    posted = client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "authorised"

    from app.db.company import company_session
    from app.models.company import Account, CreditNote, JournalEntry, JournalEntrySource

    with company_session("tc") as db:
        saved_note = db.get(CreditNote, note["id"])
        source_type = (
            JournalEntrySource.CREDIT_NOTE_AR
            if direction == "AR"
            else JournalEntrySource.CREDIT_NOTE_AP
        )
        entries = db.query(JournalEntry).filter_by(
            source_type=source_type.value, source_id=note["id"]
        ).all()
        assert saved_note.status == "authorised"
        assert len(entries) == 1
        entry = entries[0]
        assert entry.entry_date.isoformat() == "2026-06-01"
        assert entry.source_id == note["id"]
        account_codes = {account.id: account.code for account in db.query(Account).all()}
        actual = {
            account_codes[line.account_id]: (
                f"{line.debit_amount:.2f}",
                f"{line.credit_amount:.2f}",
            )
            for line in entry.lines
        }
        assert actual == expected

    assert client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD).status_code == 409
    assert client.patch(
        f"/api/v1/credit-notes/{note['id']}", headers=HEAD, json={"notes": "changed"}
    ).status_code == 409
    assert client.delete(f"/api/v1/credit-notes/{note['id']}", headers=HEAD).status_code == 409


def test_zero_gst_credit_note_omits_gst_control_line(client, accounts):
    source = _create_source(
        client,
        accounts,
        number="FICTIONAL-ZERO-GST-SOURCE",
        lines=[_source_line(accounts, tax_code="gst_free", account_code="4000")],
    )
    note = _create_credit_note(client, source, number="FICTIONAL-ZERO-GST-CN").json()
    posted = client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text

    from app.db.company import company_session
    from app.models.company import Account, JournalEntry

    with company_session("tc") as db:
        entry = db.query(JournalEntry).filter_by(
            source_type="credit_note_ar", source_id=note["id"]
        ).one()
        codes = {account.id: account.code for account in db.query(Account).all()}
        assert "2100" not in {codes[line.account_id] for line in entry.lines}


def test_locked_period_and_failed_post_leave_note_draft_without_journal(client, accounts):
    source = _create_source(client, accounts, number="FICTIONAL-LOCKED-CN-SOURCE")
    note = _create_credit_note(
        client,
        source,
        number="FICTIONAL-LOCKED-CN",
        issue_date="2026-06-01",
    ).json()
    locked = client.patch(
        "/api/v1/companies/tc", headers=HEAD, json={"books_locked_through": "2026-06-01"}
    )
    assert locked.status_code == 200, locked.text
    blocked = client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD)
    assert blocked.status_code == 409
    assert "locked" in blocked.json()["detail"].lower()

    from app.db.company import company_session
    from app.models.company import CreditNote, JournalEntry

    with company_session("tc") as db:
        saved_note = db.get(CreditNote, note["id"])
        assert saved_note.status == "draft"
        assert db.query(JournalEntry).filter_by(
            source_type="credit_note_ar", source_id=note["id"]
        ).count() == 0


def _posted_credit_note(client, accounts, *, direction, number, account_code, tax_code="standard"):
    source = _create_source(
        client,
        accounts,
        number=f"{number}-SOURCE",
        direction=direction,
        contact_name=f"Fictional {direction} Void Contact",
        lines=[_source_line(accounts, account_code=account_code, tax_code=tax_code)],
    )
    note = _create_credit_note(
        client,
        source,
        number=number,
        lines=[
            {
                "source_invoice_line_id": source["lines"][0]["source_invoice_line_id"],
                "quantity": "1.0000",
            }
        ],
    )
    assert note.status_code == 201, note.text
    posted = client.post(f"/api/v1/credit-notes/{note.json()['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    return note.json(), source


def _journal_snapshot(client, note_id, source_type):
    entries = _journal_entries(client)
    return [entry for entry in entries if entry["source_type"] == source_type and entry["source_id"] == note_id]


def test_void_ar_and_ap_reverse_original_journals_and_gst_controls(client, accounts):
    ar_note, ar_source = _posted_credit_note(
        client, accounts, direction="AR", number="VOID-AR", account_code="4000"
    )
    ap_note, ap_source = _posted_credit_note(
        client, accounts, direction="AP", number="VOID-AP", account_code="6100"
    )

    ar_original = _journal_snapshot(client, ar_note["id"], "credit_note_ar")[0]
    ap_original = _journal_snapshot(client, ap_note["id"], "credit_note_ap")[0]
    assert ar_original["entry_date"] == "2026-06-01"
    assert ap_original["entry_date"] == "2026-06-01"
    original_ar_lines = {line["account_id"]: line for line in ar_original["lines"]}
    original_ap_lines = {line["account_id"]: line for line in ap_original["lines"]}

    ar_response = client.post(
        f"/api/v1/credit-notes/{ar_note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-15"},
    )
    assert ar_response.status_code == 200, ar_response.text
    ar_reversal = _journal_snapshot(client, ar_note["id"], "credit_note_void_ar")[0]
    assert ar_reversal["entry_date"] == "2026-07-15"
    assert ar_reversal["reverses_entry_id"] == ar_original["id"]
    assert ar_reversal["source_id"] == ar_note["id"]
    assert [
        {
            "account_id": line["account_id"],
            "debit_amount": line["debit_amount"],
            "credit_amount": line["credit_amount"],
            "description": line["description"],
        }
        for line in ar_reversal["lines"]
    ] == [
        {
            "account_id": line["account_id"],
            "debit_amount": line["credit_amount"],
            "credit_amount": line["debit_amount"],
            "description": line["description"],
        }
        for line in ar_original["lines"]
    ]
    assert ar_response.json()["status"] == "void"
    assert ar_response.json()["updated_at"]
    assert client.get(f"/api/v1/credit-notes/{ar_note['id']}", headers=HEAD).json()["status"] == "void"
    assert client.get(f"/api/v1/credit-notes/source-invoices/{ar_source['id']}", headers=HEAD).json()["lines"][0]["remaining_creditable_quantity"] == "2.0000"
    assert ar_original["lines"] == list(original_ar_lines.values())

    ap_response = client.post(
        f"/api/v1/credit-notes/{ap_note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-16"},
    )
    assert ap_response.status_code == 200, ap_response.text
    ap_reversal = _journal_snapshot(client, ap_note["id"], "credit_note_void_ap")[0]
    assert ap_reversal["entry_date"] == "2026-07-16"
    assert ap_reversal["reverses_entry_id"] == ap_original["id"]
    assert ap_reversal["source_id"] == ap_note["id"]
    assert [
        {
            "account_id": line["account_id"],
            "debit_amount": line["debit_amount"],
            "credit_amount": line["credit_amount"],
            "description": line["description"],
        }
        for line in ap_reversal["lines"]
    ] == [
        {
            "account_id": line["account_id"],
            "debit_amount": line["credit_amount"],
            "credit_amount": line["debit_amount"],
            "description": line["description"],
        }
        for line in ap_original["lines"]
    ]
    assert ap_response.json()["status"] == "void"
    assert ap_original["lines"] == list(original_ap_lines.values())
    assert client.get(f"/api/v1/credit-notes/source-invoices/{ap_source['id']}", headers=HEAD).json()["lines"][0]["remaining_creditable_quantity"] == "2.0000"


def test_void_rejects_active_application_and_refund_but_allows_reversed_history(client, accounts):
    source = _create_source(client, accounts, number="VOID-HISTORY-SOURCE")
    note = _create_credit_note(client, source, number="VOID-HISTORY").json()
    posted = client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    application = client.post(
        f"/api/v1/credit-notes/{note['id']}/applications",
        headers={**HEAD, "Idempotency-Key": "void-active-application"},
        json={"invoice_id": source["id"], "amount": "100.00", "application_date": "2026-06-30"},
    )
    assert application.status_code == 201, application.text
    assert client.post(
        f"/api/v1/credit-notes/{note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-01"},
    ).status_code == 409

    reversed_application = client.post(
        f"/api/v1/credit-note-applications/{application.json()['id']}/reverse",
        headers=HEAD,
        json={"reversal_date": "2026-07-01"},
    )
    assert reversed_application.status_code == 200, reversed_application.text
    assert client.post(
        f"/api/v1/credit-notes/{note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-02"},
    ).status_code == 200

    refund_source = _create_source(client, accounts, number="VOID-REFUND-SOURCE")
    refund_note = _create_credit_note(client, refund_source, number="VOID-REFUND").json()
    assert client.post(f"/api/v1/credit-notes/{refund_note['id']}/post", headers=HEAD).status_code == 200
    bank_account_id = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]["id"]
    refund = client.post(
        f"/api/v1/credit-notes/{refund_note['id']}/refunds",
        headers={**HEAD, "Idempotency-Key": "void-active-refund"},
        json={
            "bank_account_id": bank_account_id,
            "amount": "100.00",
            "refund_date": "2026-06-30",
        },
    )
    assert refund.status_code == 201, refund.text
    assert client.post(
        f"/api/v1/credit-notes/{refund_note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-01"},
    ).status_code == 409
    reversed_refund = client.post(
        f"/api/v1/credit-notes/{refund_note['id']}/refunds/{refund.json()['id']}/reverse",
        headers=HEAD,
        json={"reversal_date": "2026-07-01"},
    )
    assert reversed_refund.status_code == 200, reversed_refund.text
    assert client.post(
        f"/api/v1/credit-notes/{refund_note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-02"},
    ).status_code == 200


def test_void_rejects_draft_duplicate_locked_date_and_missing_payload(client, accounts):
    source = _create_source(client, accounts, number="VOID-STATE-SOURCE")
    note = _create_credit_note(client, source, number="VOID-STATE").json()
    assert client.post(
        f"/api/v1/credit-notes/{note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-01"},
    ).status_code == 409
    assert client.post(
        f"/api/v1/credit-notes/{note['id']}/void",
        headers=HEAD,
        json={},
    ).status_code == 422

    posted_note = _posted_credit_note(
        client, accounts, direction="AR", number="VOID-DUPLICATE", account_code="4000"
    )[0]
    first = client.post(
        f"/api/v1/credit-notes/{posted_note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-01"},
    )
    assert first.status_code == 200, first.text
    assert client.post(
        f"/api/v1/credit-notes/{posted_note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-02"},
    ).status_code == 409

    locked_source = _create_source(client, accounts, number="VOID-LOCK-SOURCE")
    locked_note = _create_credit_note(client, locked_source, number="VOID-LOCK").json()
    assert client.post(f"/api/v1/credit-notes/{locked_note['id']}/post", headers=HEAD).status_code == 200
    locked = client.patch(
        "/api/v1/companies/tc", headers=HEAD, json={"books_locked_through": "2026-07-01"}
    )
    assert locked.status_code == 200, locked.text
    blocked = client.post(
        f"/api/v1/credit-notes/{locked_note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-01"},
    )
    assert blocked.status_code == 409
    assert "locked" in blocked.json()["detail"].lower()


def test_concurrent_void_requests_create_one_reversal(client, accounts):
    note, _ = _posted_credit_note(
        client, accounts, direction="AR", number="VOID-CONCURRENT", account_code="4000"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda _: client.post(
                f"/api/v1/credit-notes/{note['id']}/void",
                headers=HEAD,
                json={"void_date": "2026-07-20"},
            ),
            range(2),
        ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    reversals = _journal_snapshot(client, note["id"], "credit_note_void_ar")
    assert len(reversals) == 1
    assert reversals[0]["entry_date"] == "2026-07-20"
    assert reversals[0]["reverses_entry_id"] is not None


def test_void_releases_source_quantity_and_excludes_open_credit_report(client, accounts):
    source = _create_source(client, accounts, number="VOID-REPORT-SOURCE")
    note = _create_credit_note(client, source, number="VOID-REPORT").json()
    assert client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD).status_code == 200
    before = client.get(
        "/api/v1/reports/trial-balance",
        headers=HEAD,
        params={"as_of": "2026-06-30"},
    )
    assert before.status_code == 200, before.text
    assert Decimal(before.json()["supplementary"]["ar_open_credit_total"]) == Decimal("110.00")
    assert client.post(
        f"/api/v1/credit-notes/{note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-01"},
    ).status_code == 200
    after = client.get(
        "/api/v1/reports/trial-balance",
        headers=HEAD,
        params={"as_of": "2026-07-31"},
    )
    assert after.status_code == 200, after.text
    assert Decimal(after.json()["supplementary"]["ar_open_credit_total"]) == Decimal("0.00")
    source_after = client.get(
        f"/api/v1/credit-notes/source-invoices/{source['id']}", headers=HEAD
    ).json()
    assert source_after["lines"][0]["remaining_creditable_quantity"] == "2.0000"


def test_void_journal_provenance_preserves_original(client, accounts):
    note, _ = _posted_credit_note(
        client, accounts, direction="AR", number="VOID-PROVENANCE", account_code="4000"
    )
    original = _journal_snapshot(client, note["id"], "credit_note_ar")[0]
    original_lines = [dict(line) for line in original["lines"]]
    original_date = original["entry_date"]
    assert client.post(
        f"/api/v1/credit-notes/{note['id']}/void",
        headers=HEAD,
        json={"void_date": "2026-07-01"},
    ).status_code == 200
    original_after = _journal_snapshot(client, note["id"], "credit_note_ar")[0]
    assert original_after["entry_date"] == original_date
    assert original_after["lines"] == original_lines
    reversal = _journal_snapshot(client, note["id"], "credit_note_void_ar")[0]
    assert reversal["source_type"] == "credit_note_void_ar"
    assert reversal["source_id"] == note["id"]
    assert reversal["reverses_entry_id"] == original["id"]


def test_post_rechecks_settlement_added_after_draft_creation(client, accounts):
    from app.db.company import company_session
    from app.models.company import Invoice

    source = _create_source(client, accounts, number="FICTIONAL-LATE-SETTLEMENT-SOURCE")
    note = _create_credit_note(client, source, number="FICTIONAL-LATE-SETTLEMENT-CN").json()
    with company_session("tc") as db:
        invoice = db.get(Invoice, source["id"])
        invoice.paid_amount = Decimal("1.00")
        db.commit()

    failed = client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD)
    assert failed.status_code == 409
    with company_session("tc") as db:
        assert db.get(Invoice, source["id"]).paid_amount == Decimal("1.00")


def test_post_rechecks_allocation_added_after_draft_creation(client, accounts):
    source = _create_source(client, accounts, number="FICTIONAL-LATE-ALLOCATION-SOURCE")
    note = _create_credit_note(client, source, number="FICTIONAL-LATE-ALLOCATION-CN").json()
    bank_account_id = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]["id"]
    transaction = client.post(
        f"/api/v1/bank-accounts/{bank_account_id}/transactions",
        headers={**HEAD, "Idempotency-Key": "fictional-late-credit-allocation"},
        json={
            "direction": "in",
            "amount": "1.00",
            "occurred_at": "2026-06-01",
            "memo": "Fictional late allocation",
            "account_id": accounts["1100"]["id"],
            "gst_amount": "0.00",
            "tax_code": "none",
            "invoice_allocations": [{"invoice_id": source["id"], "amount": "1.00"}],
        },
    )
    assert transaction.status_code == 201, transaction.text
    failed = client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD)
    assert failed.status_code == 409


def test_authorised_and_draft_notes_both_reserve_source_quantity(client, accounts):
    source = _create_source(client, accounts, number="FICTIONAL-AUTHORISED-RESERVATION-SOURCE")
    line_id = source["lines"][0]["source_invoice_line_id"]
    first = _create_credit_note(
        client,
        source,
        number="FICTIONAL-AUTHORISED-RESERVATION-1",
        lines=[{"source_invoice_line_id": line_id, "quantity": "1.0000"}],
    ).json()
    second = _create_credit_note(
        client,
        source,
        number="FICTIONAL-AUTHORISED-RESERVATION-2",
        lines=[{"source_invoice_line_id": line_id, "quantity": "1.0000"}],
    )
    assert second.status_code == 201, second.text
    assert client.post(f"/api/v1/credit-notes/{first['id']}/post", headers=HEAD).status_code == 200

    exhausted = _create_credit_note(
        client,
        source,
        number="FICTIONAL-AUTHORISED-RESERVATION-3",
        lines=[{"source_invoice_line_id": line_id, "quantity": "0.0001"}],
    )
    assert exhausted.status_code == 409
    source_after = _get_source_snapshot(client, source["id"])
    assert source_after["lines"][0]["quantity_reserved"] == "2.0000"
    assert source_after["lines"][0]["remaining_creditable_quantity"] == "0.0000"


def test_corrupt_draft_totals_fail_atomically(client, accounts):
    from app.db.company import company_session
    from app.models.company import CreditNote, JournalEntry

    source = _create_source(client, accounts, number="FICTIONAL-FAILED-POST-SOURCE")
    note = _create_credit_note(client, source, number="FICTIONAL-FAILED-POST-CN").json()
    with company_session("tc") as db:
        saved_note = db.get(CreditNote, note["id"])
        saved_note.total += Decimal("1.00")
        db.commit()

    failed = client.post(f"/api/v1/credit-notes/{note['id']}/post", headers=HEAD)
    assert failed.status_code == 422
    with company_session("tc") as db:
        assert db.get(CreditNote, note["id"]).status == "draft"
        assert db.query(JournalEntry).filter_by(
            source_type="credit_note_ar", source_id=note["id"]
        ).count() == 0