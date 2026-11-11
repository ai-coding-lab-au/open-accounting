from __future__ import annotations

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _request_headers import manual_transaction_headers

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
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        assert r.status_code == 201, r.text
        HEAD["X-Company-Generation"] = r.json()["generation_id"]
        yield c


@pytest.fixture()
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HEAD)
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


def _invoice_payload(accounts, *, number="INV-API-1"):
    return {
        "direction": "AR",
        "contact_name": "API Customer",
        "invoice_number": number,
        "issue_date": "2026-05-31",
        "subtotal": "100.00",
        "gst_amount": "10.00",
        "total": "110.00",
        "lines": [
            {
                "description": "Services",
                "account_id": accounts["4000"]["id"],
                "quantity": "1",
                "unit_price": "100.00",
                "gst_rate": "0.10",
                "line_subtotal": "100.00",
                "line_gst": "10.00",
                "line_total": "110.00",
            }
        ],
    }


def _create_invoice(client, accounts, *, number="INV-API-1"):
    r = client.post("/api/v1/invoices", headers=HEAD, json=_invoice_payload(accounts, number=number))
    assert r.status_code == 201, r.text
    return r.json()


def _create_contact(
    client,
    *,
    name,
    kind,
    active=True,
    abn=None,
    address=None,
    email=None,
    phone=None,
):
    response = client.post(
        "/api/v1/contacts",
        headers=HEAD,
        json={
            "name": name,
            "kind": kind,
            "active": active,
            "abn": abn,
            "address": address,
            "email": email,
            "phone": phone,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _linked_invoice_payload(accounts, *, contact_id, direction="AR", number="LINKED-1"):
    payload = _invoice_payload(accounts, number=number)
    payload.pop("contact_name")
    payload["contact_id"] = contact_id
    payload["direction"] = direction
    if direction == "AP":
        payload["lines"][0]["account_id"] = accounts["6100"]["id"]
    return payload


def _invoice_state(client, invoice_id):
    response = client.get(f"/api/v1/invoices/{invoice_id}", headers=HEAD)
    assert response.status_code == 200, response.text
    invoice = response.json()

    from app.db.company import company_session
    from app.models.company import Invoice

    with company_session("tc") as db:
        stored = db.get(Invoice, invoice_id)
        lines = [
            (
                line.id,
                line.account_id,
                str(line.quantity),
                str(line.unit_price),
                str(line.line_subtotal),
                str(line.line_gst),
                str(line.line_total),
            )
            for line in sorted(stored.lines, key=lambda line: line.id)
        ]

    return {
        "direction": invoice["direction"],
        "contact_id": invoice["contact_id"],
        "subtotal": invoice["subtotal"],
        "gst_amount": invoice["gst_amount"],
        "total": invoice["total"],
        "lines": lines,
    }


def _invoice_snapshots(invoice_id):
    from app.db.company import company_session
    from app.models.company import Invoice

    fields = (
        "contact_name_snapshot",
        "contact_abn_snapshot",
        "contact_address_snapshot",
        "contact_email_snapshot",
        "contact_phone_snapshot",
    )
    with company_session("tc") as db:
        invoice = db.get(Invoice, invoice_id)
        return tuple(getattr(invoice, field) for field in fields)


def _contact_snapshot(contact):
    return (
        contact["name"],
        contact["abn"],
        contact["address"],
        contact["email"],
        contact["phone"],
    )


def test_contact_id_create_persists_contact_snapshots(client, accounts):
    contact = _create_contact(
        client,
        name="Snapshot customer",
        kind="customer",
        abn="12345678901",
        address="10 Snapshot Street",
        email="billing@snapshot.test",
        phone="0400000000",
    )
    response = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(accounts, contact_id=contact["id"], number="CONTACT-SNAPSHOT"),
    )
    assert response.status_code == 201, response.text
    assert _invoice_snapshots(response.json()["id"]) == _contact_snapshot(contact)


def test_legacy_contact_create_persists_contact_snapshots(client, accounts):
    payload = _invoice_payload(accounts, number="LEGACY-SNAPSHOT")
    payload["contact_name"] = "Legacy Snapshot Customer"
    payload["contact_abn"] = "12 345 678 901"
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text
    assert _invoice_snapshots(response.json()["id"]) == (
        "Legacy Snapshot Customer",
        "12345678901",
        None,
        None,
        None,
    )


def test_excel_import_persists_resolved_contact_snapshots(client):
    mapping = {
        "direction": 0,
        "contact_name": 1,
        "invoice_number": 2,
        "issue_date": 3,
        "total": 4,
        "contact_abn": 5,
    }
    response = client.post(
        "/api/v1/invoices/import-excel-rows",
        headers=HEAD,
        json={
            "mapping": mapping,
            "rows": [{
                "row_no": 2,
                "raw": ["AR", "Excel Snapshot Customer", "EXCEL-SNAPSHOT", "2026-05-01", "110.00", "12345678901"],
            }],
            "direction_default": "AR",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["created"]) == 1
    assert body["skipped"] == []
    assert _invoice_snapshots(body["created"][0]) == (
        "Excel Snapshot Customer",
        "12345678901",
        None,
        None,
        None,
    )


def test_draft_contact_change_refreshes_snapshots(client, accounts):
    original = _create_contact(
        client,
        name="Original Snapshot Customer",
        kind="customer",
        abn="11111111111",
        address="1 Original Street",
        email="original@snapshot.test",
        phone="0400000001",
    )
    replacement = _create_contact(
        client,
        name="Replacement Snapshot Customer",
        kind="customer",
        abn="22222222222",
        address="2 Replacement Street",
        email="replacement@snapshot.test",
        phone="0400000002",
    )
    created = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(accounts, contact_id=original["id"], number="PATCH-SNAPSHOT"),
    )
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]

    updated = client.patch(
        f"/api/v1/invoices/{invoice_id}",
        headers=HEAD,
        json={"contact_id": replacement["id"]},
    )
    assert updated.status_code == 200, updated.text
    assert _invoice_snapshots(invoice_id) == _contact_snapshot(replacement)


def test_unrelated_draft_edit_preserves_contact_snapshots(client, accounts):
    contact = _create_contact(
        client,
        name="Stable Snapshot Customer",
        kind="customer",
        abn="33333333333",
        address="3 Stable Street",
        email="stable@snapshot.test",
        phone="0400000003",
    )
    created = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(accounts, contact_id=contact["id"], number="STABLE-SNAPSHOT"),
    )
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]
    expected = _contact_snapshot(contact)

    live_edit = client.patch(
        f"/api/v1/contacts/{contact['id']}",
        headers=HEAD,
        json={
            "name": "Live Contact Renamed",
            "abn": "44444444444",
            "address": "4 Live Street",
            "email": "live@snapshot.test",
            "phone": "0400000004",
        },
    )
    assert live_edit.status_code == 200, live_edit.text
    assert _invoice_snapshots(invoice_id) == expected

    detail = client.get(f"/api/v1/invoices/{invoice_id}", headers=HEAD)
    assert detail.status_code == 200, detail.text
    assert detail.json()["contact_name"] == "Stable Snapshot Customer"

    listed = client.get("/api/v1/invoices", headers=HEAD)
    assert listed.status_code == 200, listed.text
    listed_invoice = next(invoice for invoice in listed.json() if invoice["id"] == invoice_id)
    assert listed_invoice["contact_name"] == "Stable Snapshot Customer"

    unrelated_edit = client.patch(
        f"/api/v1/invoices/{invoice_id}", headers=HEAD, json={"notes": "Updated note"}
    )
    assert unrelated_edit.status_code == 200, unrelated_edit.text
    assert _invoice_snapshots(invoice_id) == expected


def test_posting_uses_contact_name_snapshot_for_journal_memo(client, accounts):
    contact = _create_contact(
        client,
        name="Original Posting Contact",
        kind="customer",
    )
    created = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(
            accounts,
            contact_id=contact["id"],
            number="POSTING-SNAPSHOT-MEMO",
        ),
    )
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]

    renamed = client.patch(
        f"/api/v1/contacts/{contact['id']}",
        headers=HEAD,
        json={"name": "Renamed Posting Contact"},
    )
    assert renamed.status_code == 200, renamed.text
    posted = client.post(f"/api/v1/invoices/{invoice_id}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text

    journal_entry = posted.json()["journal_entry"]
    assert journal_entry["memo"] == "Invoice POSTING-SNAPSHOT-MEMO — Original Posting Contact"


def test_snapshot_field_injection_cannot_override_post_or_patch(client, accounts):
    original = _create_contact(
        client,
        name="Protected Snapshot Customer",
        kind="customer",
        abn="55555555555",
        address="5 Protected Street",
        email="protected@snapshot.test",
        phone="0400000005",
    )
    injected = {
        "contact_name_snapshot": "Forged Name",
        "contact_abn_snapshot": "99999999999",
        "contact_address_snapshot": "Forged Address",
        "contact_email_snapshot": "forged@snapshot.test",
        "contact_phone_snapshot": "0499999999",
    }
    payload = _linked_invoice_payload(
        accounts, contact_id=original["id"], number="INJECTED-SNAPSHOT"
    )
    payload.update(injected)
    created = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]
    expected = _contact_snapshot(original)
    assert _invoice_snapshots(invoice_id) == expected

    patched = client.patch(
        f"/api/v1/invoices/{invoice_id}", headers=HEAD, json={**injected, "notes": "Still protected"}
    )
    assert patched.status_code == 200, patched.text
    assert _invoice_snapshots(invoice_id) == expected


def test_contact_id_create_accepts_only_active_compatible_roles(client, accounts):
    cases = (
        ("customer", "AR"),
        ("supplier", "AP"),
        ("both", "AR"),
        ("both", "AP"),
    )
    for index, (kind, direction) in enumerate(cases):
        contact = _create_contact(
            client,
            name=f"Fictional {kind} {direction} {index}",
            kind=kind,
        )
        payload = _linked_invoice_payload(
            accounts,
            contact_id=contact["id"],
            direction=direction,
            number=f"LINKED-ACCEPT-{index}",
        )
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 201, response.text
        assert response.json()["contact_id"] == contact["id"]


def test_contact_id_create_rejects_unknown_inactive_and_incompatible_contacts(client, accounts):
    customer = _create_contact(client, name="Fictional customer", kind="customer")
    supplier = _create_contact(client, name="Fictional supplier", kind="supplier")
    inactive = _create_contact(
        client,
        name="Fictional inactive supplier",
        kind="supplier",
        active=False,
    )
    cases = (
        (_linked_invoice_payload(accounts, contact_id=customer["id"], direction="AP"), 422),
        (_linked_invoice_payload(accounts, contact_id=supplier["id"], direction="AR"), 422),
        (_linked_invoice_payload(accounts, contact_id=inactive["id"], direction="AP"), 409),
        (_linked_invoice_payload(accounts, contact_id=999999, direction="AR"), 422),
    )
    for payload, expected_status in cases:
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == expected_status, response.text


def test_contact_id_create_rejects_ambiguous_legacy_fields(client, accounts):
    contact = _create_contact(client, name="Fictional linked contact", kind="customer")
    for field, value in (("contact_name", "Fictional typed name"), ("contact_abn", "12 345 678 901")):
        payload = _linked_invoice_payload(accounts, contact_id=contact["id"])
        payload[field] = value
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 422, response.text


def test_typed_contact_name_legacy_create_remains_supported(client, accounts):
    payload = _invoice_payload(accounts, number="LEGACY-TYPED-CONTACT")
    payload["contact_name"] = "Fictional Legacy Contact"
    payload["contact_abn"] = "12 345 678 901"
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text
    invoice = response.json()
    assert invoice["contact_name"] == "Fictional Legacy Contact"
    contacts_response = client.get(
        "/api/v1/contacts",
        headers=HEAD,
        params={"q": "Fictional Legacy Contact"},
    )
    assert contacts_response.status_code == 200, contacts_response.text
    matches = [
        contact for contact in contacts_response.json()
        if contact["name"] == "Fictional Legacy Contact"
    ]
    assert len(matches) == 1
    assert matches[0]["abn"] == "12345678901"
    assert invoice["direction"] == "AR"
    assert matches[0]["kind"] == "customer"


def test_draft_patch_validates_effective_contact_and_direction(client, accounts):
    customer = _create_contact(client, name="Fictional draft customer", kind="customer")
    supplier = _create_contact(client, name="Fictional draft supplier", kind="supplier")
    inactive_supplier = _create_contact(
        client,
        name="Fictional inactive draft supplier",
        kind="supplier",
        active=False,
    )
    payload = _linked_invoice_payload(accounts, contact_id=customer["id"], number="PATCH-CONTACT")
    created = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]

    expected_state = _invoice_state(client, invoice_id)
    incompatible_direction = client.patch(
        f"/api/v1/invoices/{invoice_id}", headers=HEAD, json={"direction": "AP"}
    )
    assert incompatible_direction.status_code == 422, incompatible_direction.text
    assert _invoice_state(client, invoice_id) == expected_state

    ap_lines = _linked_invoice_payload(
        accounts,
        contact_id=supplier["id"],
        direction="AP",
    )["lines"]
    switched = client.patch(
        f"/api/v1/invoices/{invoice_id}",
        headers=HEAD,
        json={"direction": "AP", "contact_id": supplier["id"], "lines": ap_lines},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["direction"] == "AP"
    assert switched.json()["contact_id"] == supplier["id"]
    expected_state = _invoice_state(client, invoice_id)

    for contact_id, expected_status in ((customer["id"], 422), (inactive_supplier["id"], 409), (999999, 422)):
        expected_state = _invoice_state(client, invoice_id)
        response = client.patch(
            f"/api/v1/invoices/{invoice_id}",
            headers=HEAD,
            json={"contact_id": contact_id},
        )
        assert response.status_code == expected_status, response.text
        assert _invoice_state(client, invoice_id) == expected_state


def test_posted_invoice_contact_and_direction_remain_locked(client, accounts):
    customer = _create_contact(client, name="Fictional locked customer", kind="customer")
    supplier = _create_contact(client, name="Fictional locked supplier", kind="supplier")
    created = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(accounts, contact_id=customer["id"], number="LOCKED-CONTACT"),
    )
    assert created.status_code == 201, created.text
    invoice = created.json()
    assert client.post(f"/api/v1/invoices/{invoice['id']}/post", headers=HEAD).status_code == 200

    response = client.patch(
        f"/api/v1/invoices/{invoice['id']}",
        headers=HEAD,
        json={"direction": "AP", "contact_id": supplier["id"]},
    )
    assert response.status_code == 422, response.text


def test_duplicate_invoice_number_is_scoped_by_direction_and_contact(client, accounts):
    contact = _create_contact(client, name="Fictional both-role contact", kind="both")
    other_customer = _create_contact(
        client,
        name="Fictional separate customer",
        kind="customer",
    )
    number = "DIRECTION-CONTACT-DUPLICATE"
    ar = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(accounts, contact_id=contact["id"], number=number),
    )
    assert ar.status_code == 201, ar.text
    ap = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(accounts, contact_id=contact["id"], direction="AP", number=number),
    )
    assert ap.status_code == 201, ap.text

    other_contact_ar = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(
            accounts,
            contact_id=other_customer["id"],
            number=number,
        ),
    )
    assert other_contact_ar.status_code == 201, other_contact_ar.text

    duplicate = client.post(
        "/api/v1/invoices",
        headers=HEAD,
        json=_linked_invoice_payload(accounts, contact_id=contact["id"], number=number),
    )
    assert duplicate.status_code == 409, duplicate.text


def _explicit_payload(
    accounts,
    *,
    number,
    amount_mode,
    tax_code="standard",
    direction="AR",
    account_code=None,
    quantity="1",
    unit_price="100.00",
    subtotal="100.00",
    gst="10.00",
    total="110.00",
):
    account_code = account_code or ("4000" if direction == "AR" else "6100")
    return {
        "direction": direction,
        "contact_name": "Explicit Mode Contact",
        "invoice_number": number,
        "issue_date": "2026-05-31",
        "subtotal": subtotal,
        "gst_amount": gst,
        "total": total,
        "gst_inclusive": True,
        "amount_mode": amount_mode,
        "lines": [
            {
                "description": "Fictional service",
                "account_id": accounts[account_code]["id"],
                "quantity": quantity,
                "unit_price": unit_price,
                "line_subtotal": subtotal,
                "line_gst": gst,
                "line_total": total,
                "tax_code": tax_code,
            }
        ],
    }


def test_explicit_exclusive_inclusive_and_no_tax_examples_create_drafts(client, accounts):
    examples = (
        _explicit_payload(
            accounts,
            number="MODE-EXCLUSIVE",
            amount_mode="exclusive",
            unit_price="1000.00",
            subtotal="1000.00",
            gst="100.00",
            total="1100.00",
        ),
        _explicit_payload(
            accounts,
            number="MODE-INCLUSIVE",
            amount_mode="inclusive",
            unit_price="1100.00",
            subtotal="1000.00",
            gst="100.00",
            total="1100.00",
        ),
        _explicit_payload(
            accounts,
            number="MODE-GST-FREE",
            amount_mode="inclusive",
            tax_code="gst_free",
            quantity="2",
            unit_price="55.00",
            subtotal="110.00",
            gst="0.00",
            total="110.00",
        ),
    )

    for payload in examples:
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 201, response.text
        invoice = response.json()
        assert invoice["status"] == "draft"
        assert invoice["journal_entries"] == []
        assert (invoice["subtotal"], invoice["gst_amount"], invoice["total"]) == (
            payload["subtotal"], payload["gst_amount"], payload["total"]
        )

    journals = client.get(
        "/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD
    )
    assert journals.status_code == 200
    assert journals.json() == []


def test_explicit_tax_code_mode_matrix_and_no_tax_normalization(client, accounts):
    cases = []
    for amount_mode, unit_price, subtotal, gst, total in (
        ("exclusive", "100.00", "100.00", "10.00", "110.00"),
        ("inclusive", "110.00", "100.00", "10.00", "110.00"),
    ):
        for tax_code in ("standard", "capital"):
            cases.append((amount_mode, tax_code, unit_price, subtotal, gst, total))
        for tax_code in ("gst_free", "input_taxed", "none"):
            cases.append((amount_mode, tax_code, "55.00", "55.00", "0.00", "55.00"))

    for amount_mode, tax_code, unit_price, subtotal, gst, total in cases:
        direction = "AP" if tax_code == "capital" else "AR"
        account_code = "1700" if tax_code == "capital" else "4000"
        payload = _explicit_payload(
            accounts,
            number=f"MATRIX-{amount_mode}-{tax_code}",
            amount_mode=amount_mode,
            tax_code=tax_code,
            direction=direction,
            account_code=account_code,
            unit_price=unit_price,
            subtotal=subtotal,
            gst=gst,
            total=total,
        )
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 201, response.text

    no_tax = _explicit_payload(
        accounts,
        number="MATRIX-NO-TAX",
        amount_mode="none",
        tax_code="standard",
        unit_price="55.00",
        subtotal="55.00",
        gst="0.00",
        total="55.00",
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=no_tax)
    assert response.status_code == 201, response.text
    assert response.json()["gst_inclusive"] is False

    from app.db.company import company_session
    from app.models.company import Invoice

    with company_session("tc") as db:
        stored = db.query(Invoice).filter_by(invoice_number="MATRIX-NO-TAX").one()
        assert stored.lines[0].tax_code == "none"


def test_explicit_amount_mode_rejects_incomplete_or_mismatched_lines(client, accounts):
    valid = _explicit_payload(
        accounts,
        number="MODE-VALIDATION-BASE",
        amount_mode="exclusive",
    )
    invalid_payloads = []

    wrong_subtotal = {**valid, "invoice_number": "MODE-WRONG-SUBTOTAL"}
    wrong_subtotal["lines"] = [{**valid["lines"][0], "line_subtotal": "99.00"}]
    invalid_payloads.append(wrong_subtotal)

    wrong_gst = {**valid, "invoice_number": "MODE-WRONG-GST"}
    wrong_gst["lines"] = [{**valid["lines"][0], "line_gst": "9.00"}]
    invalid_payloads.append(wrong_gst)

    wrong_header = {**valid, "invoice_number": "MODE-WRONG-HEADER", "total": "109.00"}
    invalid_payloads.append(wrong_header)

    missing_price = {**valid, "invoice_number": "MODE-MISSING-PRICE"}
    missing_price["lines"] = [{key: value for key, value in valid["lines"][0].items() if key != "unit_price"}]
    invalid_payloads.append(missing_price)

    invalid_tax_code = {**valid, "invoice_number": "MODE-INVALID-TAX-CODE"}
    invalid_tax_code["lines"] = [{**valid["lines"][0], "tax_code": "gst_free", "line_gst": "10.00"}]
    invalid_payloads.append(invalid_tax_code)

    null_tax_code = {**valid, "invoice_number": "MODE-NULL-TAX-CODE"}
    null_tax_code["lines"] = [{**valid["lines"][0], "tax_code": None}]
    invalid_payloads.append(null_tax_code)

    capital_ar = _explicit_payload(
        accounts,
        number="MODE-CAPITAL-AR",
        amount_mode="exclusive",
        tax_code="capital",
        direction="AR",
        account_code="4000",
    )
    invalid_payloads.append(capital_ar)

    capital_expense = _explicit_payload(
        accounts,
        number="MODE-CAPITAL-EXPENSE",
        amount_mode="exclusive",
        tax_code="capital",
        direction="AP",
        account_code="6100",
    )
    invalid_payloads.append(capital_expense)

    ar_expense = _explicit_payload(
        accounts,
        number="MODE-AR-EXPENSE",
        amount_mode="exclusive",
        direction="AR",
        account_code="6100",
    )
    invalid_payloads.append(ar_expense)

    ap_income = _explicit_payload(
        accounts,
        number="MODE-AP-INCOME",
        amount_mode="exclusive",
        direction="AP",
        account_code="4000",
    )
    invalid_payloads.append(ap_income)

    for payload in invalid_payloads:
        response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
        assert response.status_code == 422, (payload["invoice_number"], response.text)


def test_explicit_rounds_each_line_before_header_summation(client, accounts):
    payload = _explicit_payload(
        accounts,
        number="MODE-LINE-ROUNDING",
        amount_mode="exclusive",
        unit_price="0.05",
        subtotal="0.10",
        gst="0.02",
        total="0.12",
    )
    payload["lines"] = [
        {
            **payload["lines"][0],
            "description": f"Line {index}",
            "line_subtotal": "0.05",
            "line_gst": "0.01",
            "line_total": "0.06",
        }
        for index in (1, 2)
    ]
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text


def test_explicit_quantity_scale_and_half_up_extended_amount(client, accounts):
    payload = _explicit_payload(
        accounts,
        number="MODE-QUANTITY-SCALE",
        amount_mode="exclusive",
        quantity="1.2345",
        unit_price="0.01",
        subtotal="0.01",
        gst="0.00",
        total="0.01",
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text

    half_cent = _explicit_payload(
        accounts,
        number="MODE-QUANTITY-HALF-CENT",
        amount_mode="exclusive",
        quantity="1.5",
        unit_price="0.01",
        subtotal="0.02",
        gst="0.00",
        total="0.02",
    )
    response = client.post("/api/v1/invoices", headers=HEAD, json=half_cent)
    assert response.status_code == 201, response.text


def test_legacy_payload_without_amount_mode_keeps_existing_contract(client, accounts):
    payload = _invoice_payload(accounts, number="LEGACY-AMOUNT-MODE-OMITTED")
    payload["gst_inclusive"] = True
    response = client.post("/api/v1/invoices", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text
    invoice = response.json()
    assert invoice["status"] == "draft"
    assert invoice["gst_inclusive"] is True
    assert invoice["journal_entries"] == []


def test_post_endpoint_creates_journal_and_list_filter_finds_it(client, accounts):
    inv = _create_invoice(client, accounts)
    assert inv["status"] == "draft"
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invoice"]["status"] == "authorised"
    assert body["invoice"]["subtotal"] == "100.00"
    assert body["invoice"]["gst_amount"] == "10.00"
    assert body["invoice"]["total"] == "110.00"
    assert body["invoice"]["paid_amount"] == "0.00"
    assert body["journal_entry"]["source_type"] == "invoice_ar"
    assert body["journal_entry"]["source_id"] == inv["id"]

    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200, r.text
    assert [e["id"] for e in r.json()] == [body["journal_entry"]["id"]]


def test_post_already_authorised_returns_409(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-2")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200
    r = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    assert r.status_code == 409


def test_void_endpoint_creates_reversal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-3")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invoice"]["status"] == "void"
    assert body["journal_entry"]["source_type"] == "invoice_reversal"
    assert body["journal_entry"]["reverses_entry_id"] is not None


def test_invoice_journals_reject_manual_mutations_and_keep_provenance(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-IMMUTABLE")
    posted = client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    assert posted.status_code == 200, posted.text
    original = posted.json()["journal_entry"]

    voided = client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)
    assert voided.status_code == 200, voided.text
    reversal = voided.json()["journal_entry"]

    assert original["source_type"] == "invoice_ar"
    assert original["source_id"] == inv["id"]
    assert original["reverses_entry_id"] is None
    assert reversal["source_type"] == "invoice_reversal"
    assert reversal["source_id"] == inv["id"]
    assert reversal["reverses_entry_id"] == original["id"]

    for entry in (original, reversal):
        patch = client.patch(
            f"/api/v1/journal/{entry['id']}",
            headers=HEAD,
            json={"memo": "Unauthorized fictional journal edit"},
        )
        assert patch.status_code == 409, patch.text

        delete = client.delete(f"/api/v1/journal/{entry['id']}", headers=HEAD)
        assert delete.status_code == 409, delete.text

        retrieved = client.get(f"/api/v1/journal/{entry['id']}", headers=HEAD)
        assert retrieved.status_code == 200, retrieved.text
        assert retrieved.json()["memo"] == entry["memo"]
        assert retrieved.json()["source_type"] == entry["source_type"]
        assert retrieved.json()["source_id"] == inv["id"]
        assert retrieved.json()["reverses_entry_id"] == entry["reverses_entry_id"]


def test_patch_financial_field_on_authorised_invoice_rejected(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-4")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HEAD, json={"subtotal": "90.00"})
    assert r.status_code == 422


def test_delete_draft_hard_deletes_without_journal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-5")
    r = client.delete(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 204, r.text
    assert client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD).status_code == 404
    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200
    assert r.json() == []


def test_delete_authorised_voids_and_posts_reversal(client, accounts):
    inv = _create_invoice(client, accounts, number="INV-API-6")
    client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)
    r = client.delete(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 204, r.text
    r = client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD)
    assert r.status_code == 200
    assert r.json()["status"] == "void"
    r = client.get("/api/v1/journal/entries?source_type=invoice_reversal", headers=HEAD)
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_concurrent_post_one_wins_other_409_not_500(client, accounts):
    """Round-3 P2: two simultaneous /post calls → [200, 409], one journal entry.
    The loser must not surface the DB unique-index IntegrityError as a 500.
    """
    inv = _create_invoice(client, accounts, number="INV-API-RACE-POST")

    def post():
        return client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(), range(2)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    r = client.get("/api/v1/journal/entries?source_type=invoice_ar", headers=HEAD)
    assert r.status_code == 200
    entries = [e for e in r.json() if e["source_id"] == inv["id"]]
    assert len(entries) == 1


def test_concurrent_void_one_wins_other_409_not_500(client, accounts):
    """Round-3 P2: two simultaneous /void calls → [200, 409], one reversal entry."""
    inv = _create_invoice(client, accounts, number="INV-API-RACE-VOID")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200

    def void():
        return client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: void(), range(2)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    r = client.get("/api/v1/journal/entries?source_type=invoice_reversal", headers=HEAD)
    assert r.status_code == 200
    entries = [e for e in r.json() if e["source_id"] == inv["id"]]
    assert len(entries) == 1


def test_concurrent_settlement_and_void_have_one_safe_winner(client, accounts):
    """A settle/void race must never leave both the bank settlement and the
    invoice reversal committed. SQLite's immediate lock serialises the two
    invariant checks; the loser receives a recoverable 409."""
    inv = _create_invoice(client, accounts, number="INV-API-RACE-SETTLE-VOID")
    assert client.post(f"/api/v1/invoices/{inv['id']}/post", headers=HEAD).status_code == 200
    bank = client.get("/api/v1/bank-accounts", headers=HEAD).json()[0]

    def settle():
        return client.post(
            f"/api/v1/bank-accounts/{bank['id']}/transactions",
            headers=manual_transaction_headers(HEAD),
            json={
                "direction": "in",
                "amount": "50.00",
                "occurred_at": "2026-06-01",
                "account_id": accounts["1100"]["id"],
                "tax_code": "standard",
                "gst_amount": "0",
            },
        )

    def void():
        return client.post(f"/api/v1/invoices/{inv['id']}/void", headers=HEAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        settle_future = pool.submit(settle)
        void_future = pool.submit(void)
        settle_response = settle_future.result()
        void_response = void_future.result()

    assert sorted((settle_response.status_code, void_response.status_code)) in (
        [200, 409],
        [201, 409],
    ), (settle_response.text, void_response.text)

    current = client.get(f"/api/v1/invoices/{inv['id']}", headers=HEAD).json()
    transactions = client.get(
        f"/api/v1/bank-accounts/{bank['id']}/transactions",
        headers=HEAD,
    ).json()
    matching_settlements = [
        txn
        for txn in transactions
        if txn["account_id"] == accounts["1100"]["id"]
        and txn["amount"] == "50.00"
    ]
    if current["status"] == "void":
        assert matching_settlements == []
        assert settle_response.status_code == 409
    else:
        assert len(matching_settlements) == 1
        assert settle_response.status_code == 201
        assert void_response.status_code == 409
