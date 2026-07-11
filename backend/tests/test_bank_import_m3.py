"""Tests for bank statement import + auto-categorisation rules (M3).

Covers:
  - CSV parsing with separate debit/credit columns
  - CSV parsing with signed amount column
  - Header auto-mapping
  - Dedup detection on re-import
  - Rule matching: priority, memo regex, amount range
  - Commit endpoint actually creates the txns and skips duplicates
  - Rules CRUD
"""

from __future__ import annotations

import io
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from decimal import Decimal

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
        import shutil
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app
    with TestClient(app) as c:
        company = c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        HEAD["X-Company-Generation"] = company.json()["generation_id"]
        yield c


@pytest.fixture()
def biz_bank(client):
    r = client.get("/api/v1/bank-accounts", headers=HEAD)
    return r.json()[0]


@pytest.fixture()
def accounts(client):
    r = client.get("/api/v1/accounts", headers=HEAD)
    return {a["code"]: a for a in r.json()}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _upload(client, bank_id, csv_text: str, *, filename: str = "stmt.csv"):
    return client.post(
        f"/api/v1/bank-accounts/{bank_id}/import/preview",
        headers=HEAD,
        files={"file": (filename, io.BytesIO(csv_text.encode("utf-8")), "text/csv")},
    )


def test_preview_separate_debit_credit_columns(client, biz_bank):
    csv = (
        "Date,Description,Debit,Credit\n"
        "2026-05-01,Zzqq refund,,5000.00\n"
        "2026-05-02,Office rent,1500.00,\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["occurred_at"] is not None
    assert body["mapping"]["debit"] is not None
    assert body["mapping"]["credit"] is not None
    rows = body["rows"]
    assert len(rows) == 2
    # First row: credit 5000 → IN
    assert rows[0]["parsed"]["direction"] == "in"
    assert rows[0]["parsed"]["amount"] == "5000.00"
    # Second row: debit 1500 → OUT
    assert rows[1]["parsed"]["direction"] == "out"
    assert rows[1]["parsed"]["amount"] == "1500.00"
    # New, no rules + a memo that matches no heuristic → no suggestion,
    # not duplicate. (Uses a nonsense memo: real words like "salary" now
    # legitimately match the salary/wages heuristic.)
    assert rows[0]["is_duplicate"] is False
    assert rows[0]["suggested_account_id"] is None


def test_preview_debit_amount_credit_amount_headers_keep_direction(client, biz_bank):
    csv = (
        "Bank Account,Date,Narrative,Debit Amount,Credit Amount,Balance\n"
        "123,2026-05-01,Office rent,1500.00,,8500.00\n"
        "123,2026-05-02,Customer payment,,2200.00,10700.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["debit"] == 3
    assert body["mapping"]["credit"] == 4
    assert body["mapping"]["amount"] is None
    assert body["rows"][0]["parsed"]["direction"] == "out"
    assert body["rows"][0]["parsed"]["amount"] == "1500.00"
    assert body["rows"][1]["parsed"]["direction"] == "in"
    assert body["rows"][1]["parsed"]["amount"] == "2200.00"


def test_preview_chinese_amount_headers_keep_direction(client, biz_bank):
    csv = (
        "\u65e5\u671f,\u6458\u8981,\u652f\u51fa\u91d1\u989d,\u6536\u5165\u91d1\u989d\n"
        "2026-05-01,\u623f\u79df,1500.00,\n"
        "2026-05-02,\u6536\u6b3e,,2200.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["debit"] == 2
    assert body["mapping"]["credit"] == 3
    assert body["mapping"]["amount"] is None
    assert body["rows"][0]["parsed"]["direction"] == "out"
    assert body["rows"][1]["parsed"]["direction"] == "in"


def test_short_hints_do_not_steal_amount_columns():
    """Bare 'in'/'out' hints must match whole words only: 'in' ⊂ 'incl' /
    'spending' used to let credit claim a lone amount column and import every
    expense as money-in."""
    from app.services.bank_import import propose_mapping

    m = propose_mapping(["Date", "Description", "Amount (incl GST)"])
    assert m["amount"] == 2 and m["credit"] is None and m["debit"] is None

    m = propose_mapping(["Date", "Description", "Spending Amount"])
    assert m["amount"] == 2 and m["credit"] is None and m["debit"] is None

    m = propose_mapping(["Date", "Description", "Payout Amount"])
    assert m["amount"] == 2 and m["debit"] is None


def test_money_in_money_out_headers_still_map_as_debit_credit():
    from app.services.bank_import import propose_mapping

    m = propose_mapping(["Date", "Description", "Money Out", "Money In"])
    assert m["debit"] == 2
    assert m["credit"] == 3
    assert m["amount"] is None


def test_preview_signed_amount_column(client, biz_bank):
    csv = (
        "Date,Narrative,Amount\n"
        "2026-05-01,Consulting,1100.00\n"
        "2026-05-02,Rent,-1500.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    body = r.json()
    assert body["rows"][0]["parsed"]["direction"] == "in"
    assert body["rows"][1]["parsed"]["direction"] == "out"
    assert body["rows"][1]["parsed"]["amount"] == "1500.00"  # unsigned


def test_preview_zero_amount_row_has_specific_issue(client, biz_bank):
    csv = "Date,Description,Amount\n2026-05-01,Zero amount adjustment,0.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["ok"] is False
    assert row["issue"] == "Zero-amount rows are skipped; bank transactions must be non-zero"


def test_dedup_marks_existing(client, biz_bank):
    csv = "Date,Description,Credit\n2026-05-01,Salary,5000.00\n"
    # First import: commit it.
    r = _upload(client, biz_bank["id"], csv)
    rows = r.json()["rows"]
    commit = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [
            {
                "occurred_at": rows[0]["parsed"]["occurred_at"],
                "direction": rows[0]["parsed"]["direction"],
                "amount": rows[0]["parsed"]["amount"],
                "dedup_key": rows[0]["dedup_key"],
                "memo": rows[0]["parsed"]["memo"],
            }
        ]},
    )
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"created": 1, "skipped_duplicates": 0}

    # Re-preview the same CSV → should mark as duplicate.
    r2 = _upload(client, biz_bank["id"], csv)
    assert r2.json()["rows"][0]["is_duplicate"] is True

    # And committing it should skip.
    rows2 = r2.json()["rows"]
    commit2 = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [
            {
                "occurred_at": rows2[0]["parsed"]["occurred_at"],
                "direction": rows2[0]["parsed"]["direction"],
                "amount": rows2[0]["parsed"]["amount"],
                "dedup_key": rows2[0]["dedup_key"],
                "memo": rows2[0]["parsed"]["memo"],
            }
        ]},
    )
    assert commit2.json() == {"created": 0, "skipped_duplicates": 1}


def test_dedup_flags_row_matching_manual_transaction(client, biz_bank):
    """A CSV row that duplicates a MANUALLY-entered transaction (which has no
    dedup_key) is flagged — while a same-amount/same-day row with different text
    is not. Guards the gap where re-importing over hand-typed rows silently
    double-counted money."""
    # Manually-entered transaction (no dedup_key), memo "Acme Client Payment".
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "5000.00",
            "occurred_at": "2026-07-01",
            "memo": "Acme Client Payment",
            "tax_code": "none",
        },
    )
    assert r.status_code == 201, r.text

    csv = (
        "Date,Description,Credit,Payee\n"
        # Same amount/date/direction; payee matches the manual memo → duplicate.
        "2026-07-01,Invoice payment received,5000.00,Acme Client Payment\n"
        # Same amount/date/direction but no text overlap → NOT a duplicate.
        "2026-07-01,Totally different deposit,5000.00,Someone Else\n"
    )
    r2 = _upload(client, biz_bank["id"], csv)
    assert r2.status_code == 200, r2.text
    rows = r2.json()["rows"]
    assert rows[0]["is_duplicate"] is True, rows[0]
    assert rows[1]["is_duplicate"] is False, rows[1]


def test_named_month_date_formats_parse(client, biz_bank):
    """`15-Jul-2026` (day-Mon-year) and `Jul 15 2026` (month-first) parse."""
    csv = (
        "Date,Description,Credit\n"
        "15-Jul-2026,Row A,100.00\n"
        "Jul 15 2026,Row B,200.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows[0]["ok"] is True and rows[0]["parsed"]["occurred_at"] == "2026-07-15", rows[0]
    assert rows[1]["ok"] is True and rows[1]["parsed"]["occurred_at"] == "2026-07-15", rows[1]


def test_far_future_date_rejected_on_manual_entry(client, biz_bank):
    """A transaction dated beyond the reportable BAS window (FY2000–FY2100) is
    rejected, so it can't become an orphan that shows in the trial balance but
    no BAS quarter."""
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={"direction": "in", "amount": "100.00", "occurred_at": "9999-12-31"},
    )
    assert r.status_code == 422, r.text
    # A normal date still works.
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={"direction": "in", "amount": "100.00", "occurred_at": "2026-07-01"},
    )
    assert r.status_code == 201, r.text


def test_ambiguous_debit_and_credit_row_flagged(client, biz_bank):
    """A row with BOTH a debit and a credit is flagged, not silently resolved to
    one direction."""
    csv = "Date,Description,Debit,Credit\n2026-05-01,Weird row,50.00,80.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["ok"] is False
    assert "both a debit and a credit" in row["issue"].lower()


def test_intra_file_duplicate_rows_flagged(client, biz_bank):
    """Two identical rows in the SAME file: the second is flagged duplicate so
    the preview count matches what commit will actually create (commit skips
    the second)."""
    csv = (
        "Date,Description,Credit\n"
        "2026-05-01,Salary,5000.00\n"
        "2026-05-01,Salary,5000.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows[0]["is_duplicate"] is False, rows[0]
    assert rows[1]["is_duplicate"] is True, rows[1]


def test_commit_skips_row_duplicating_existing_manual_transaction(client, biz_bank):
    """Server-side: a committed row that fingerprint-duplicates an existing
    MANUAL transaction (no dedup_key) is skipped, not created — even if the
    caller submits it. Guards against double-counting when the UI's default
    unchecking is bypassed."""
    m = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "5000.00",
            "occurred_at": "2026-07-01",
            "memo": "Acme Client Payment",
            "tax_code": "none",
        },
    )
    assert m.status_code == 201, m.text

    commit = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {
                    "occurred_at": "2026-07-01",
                    "direction": "in",
                    "amount": "5000.00",
                    "dedup_key": "a_key_not_in_the_db",
                    "memo": "Invoice payment received",
                    "counter_party_name": "Acme Client Payment",
                }
            ]
        },
    )
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"created": 0, "skipped_duplicates": 1}


def test_commit_imports_two_distinct_same_day_same_payee_payments(client, biz_bank):
    """Two genuine same-day, same-amount receipts from the same client for
    DIFFERENT invoices (different memos -> different dedup_keys) must BOTH be
    created. The commit-side fingerprint skip only guards against rows already
    on the account, never against another row inside the same payload."""
    commit = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {
                    "occurred_at": "2026-07-01",
                    "direction": "in",
                    "amount": "100.00",
                    "dedup_key": "key_invoice_123",
                    "memo": "Invoice 123",
                    "counter_party_name": "Acme Pty Ltd",
                },
                {
                    "occurred_at": "2026-07-01",
                    "direction": "in",
                    "amount": "100.00",
                    "dedup_key": "key_invoice_456",
                    "memo": "Invoice 456",
                    "counter_party_name": "Acme Pty Ltd",
                },
            ]
        },
    )
    assert commit.status_code == 200, commit.text
    assert commit.json() == {"created": 2, "skipped_duplicates": 0}


def test_headerless_commbank_csv(client, biz_bank):
    """CommBank NetBank CSV export has NO header row and signed '+'/'-' amounts:
    Date, Amount, Description, Balance. The first transaction must not be eaten
    as a header, and '+5000.00' must parse as money-in."""
    csv = (
        '06/07/2026,"+5000.00","Fast Transfer From Someone","+5004.43"\n'
        '05/07/2026,"-180.00","Transfer to xx0000 CommBank app","+4.43"\n'
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = [x for x in r.json()["rows"] if x.get("ok")]
    assert len(rows) == 2, r.json()  # first row kept, not consumed as a header
    first = rows[0]["parsed"]
    assert first["occurred_at"] == "2026-07-06"
    assert first["direction"] == "in"
    assert first["amount"] == "5000.00"
    assert "Fast Transfer" in (first["memo"] or "")
    assert rows[1]["parsed"]["direction"] == "out"


def test_pdf_statement_preview_and_commit(client, biz_bank):
    """A PDF statement flows through the same preview → commit pipeline, with a
    bank_format form field."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Courier", 10)
    y = 800
    for ln in [
        "Commonwealth Bank - Transaction listing",
        "01/07/2026  Salary ACME PTY LTD   5,000.00   6,000.00",
        "03/07/2026  Rent payment          1,200.00   4,800.00",
    ]:
        c.drawString(40, y, ln)
        y -= 16
    c.save()
    pdf = buf.getvalue()

    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/preview",
        headers=HEAD,
        files={"file": ("statement.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"bank_format": "auto"},
    )
    assert r.status_code == 200, r.text
    rows = [row for row in r.json()["rows"] if row.get("ok")]
    assert len(rows) == 2, r.json()
    dirs = {row["parsed"]["direction"] for row in rows}
    assert dirs == {"in", "out"}

    commit = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {
                    "occurred_at": row["parsed"]["occurred_at"],
                    "direction": row["parsed"]["direction"],
                    "amount": row["parsed"]["amount"],
                    "dedup_key": row["dedup_key"],
                    "memo": row["parsed"]["memo"],
                }
                for row in rows
            ]
        },
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["created"] == 2


def test_dedup_unique_index_exists_and_reimport_skips(client, biz_bank):
    from sqlalchemy import text

    from app.db.company import get_company_engine

    with get_company_engine("tc").connect() as conn:
        indexes = conn.execute(text("PRAGMA index_list(bank_transactions)")).fetchall()
    index_by_name = {row[1]: row for row in indexes}
    assert "uq_bank_txn_dedup" in index_by_name
    assert index_by_name["uq_bank_txn_dedup"][2] == 1

    csv = "Date,Description,Credit\n2026-05-01,Salary,5000.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]

    payload = {
        "rows": [
            {
                "occurred_at": row["parsed"]["occurred_at"],
                "direction": row["parsed"]["direction"],
                "amount": row["parsed"]["amount"],
                "dedup_key": row["dedup_key"],
                "memo": row["parsed"]["memo"],
            }
        ]
    }
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json=payload,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 1, "skipped_duplicates": 0}

    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json=payload,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 0, "skipped_duplicates": 1}


def test_commit_ignores_empty_and_forged_client_dedup_keys(client, biz_bank):
    base = {
        "occurred_at": "2026-05-20",
        "direction": "in",
        "amount": "321.00",
        "memo": "Server owned dedup marker",
        "counter_party_name": "Canonical Client",
    }
    response = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {**base, "dedup_key": ""},
                {**base, "dedup_key": "f" * 64},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"created": 1, "skipped_duplicates": 1}

    from app.db.company import company_session
    from app.models.company import BankTransaction

    with company_session("tc") as db:
        stored = (
            db.query(BankTransaction)
            .filter(BankTransaction.memo == base["memo"])
            .one()
        )
        assert stored.dedup_key
        assert len(stored.dedup_key) == 64
        assert stored.dedup_key != "f" * 64

    retry = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [{**base, "dedup_key": "retry-forgery"}]},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"created": 0, "skipped_duplicates": 1}


def test_commit_canonical_key_includes_counterparty(client, biz_bank):
    common = {
        "occurred_at": "2026-05-21",
        "direction": "in",
        "amount": "87.00",
        "memo": "Settlement",
        # Deliberately identical forged values: server identity must win.
        "dedup_key": "same-client-key",
    }
    payload = {
        "rows": [
            {**common, "counter_party_name": "Customer Alpha"},
            {**common, "counter_party_name": "Customer Beta"},
        ]
    }
    first = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json=payload,
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"created": 2, "skipped_duplicates": 0}

    retry = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json=payload,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"created": 0, "skipped_duplicates": 2}


def test_concurrent_commit_of_same_canonical_row_writes_once(client, biz_bank):
    base = {
        "occurred_at": "2026-05-22",
        "direction": "out",
        "amount": "42.50",
        "memo": "Concurrent canonical marker",
        "counter_party_name": "One Supplier",
    }

    def submit(dedup_key: str):
        return client.post(
            f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
            headers=HEAD,
            json={"rows": [{**base, "dedup_key": dedup_key}]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ("", "forged-concurrent-key")))

    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]
    assert sorted(response.json()["created"] for response in responses) == [0, 1]
    assert sorted(
        response.json()["skipped_duplicates"] for response in responses
    ) == [0, 1]

    listed = client.get(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
    ).json()
    assert sum(txn["memo"] == base["memo"] for txn in listed) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "not-a-number"),
        ("amount", "0"),
        ("amount", "-1.00"),
        ("amount", "1.001"),
        ("amount", "100000000000000.00"),
        ("gst_amount", "-0.01"),
        ("gst_amount", "1.001"),
        ("occurred_at", "not-a-date"),
        ("occurred_at", "1999-06-30"),
    ],
)
def test_commit_schema_rejects_bad_money_and_dates_without_500_or_row_echo(
    client, biz_bank, field, value,
):
    row = {
        "occurred_at": "2026-05-23",
        "direction": "in",
        "amount": "10.00",
        "gst_amount": "0.00",
        "memo": "SECRET-MEMO-MUST-NOT-ECHO",
        "counter_party_name": "SECRET-COUNTERPARTY-MUST-NOT-ECHO",
        field: value,
    }
    response = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [row]},
    )
    assert response.status_code == 422, response.text
    assert "SECRET-MEMO-MUST-NOT-ECHO" not in response.text
    assert "SECRET-COUNTERPARTY-MUST-NOT-ECHO" not in response.text


def test_commit_business_error_contains_only_row_index_and_field_reason(
    client, biz_bank,
):
    response = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {
                    "occurred_at": "2026-05-24",
                    "direction": "in",
                    "amount": "10.00",
                    "gst_amount": "11.00",
                    "memo": "SECRET-BUSINESS-ERROR-MEMO",
                    "counter_party_name": "SECRET-BUSINESS-ERROR-PARTY",
                }
            ]
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Row 1: gst_amount must not exceed amount"
    )
    assert "SECRET-BUSINESS-ERROR-MEMO" not in response.text
    assert "SECRET-BUSINESS-ERROR-PARTY" not in response.text


# ---------------------------------------------------------------------------
# Rules CRUD + matching
# ---------------------------------------------------------------------------


def test_rule_crud_round_trip(client, accounts):
    rent = accounts["6100"]

    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 50,
        "description": "Office rent → 6100",
        "match_direction": "out",
        "match_memo_regex": "(?i)rent",
        "set_account_id": rent["id"],
        "set_tax_code": "standard",
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    r = client.get("/api/v1/bank-rules", headers=HEAD)
    assert len(r.json()) == 1

    r = client.patch(f"/api/v1/bank-rules/{rid}", headers=HEAD, json={"priority": 10})
    assert r.json()["priority"] == 10

    r = client.delete(f"/api/v1/bank-rules/{rid}", headers=HEAD)
    assert r.status_code == 204
    assert client.get("/api/v1/bank-rules", headers=HEAD).json() == []


def test_rule_patch_can_clear_nullable_match_predicates(client, accounts):
    rent = accounts["6100"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "description": "Clearable predicates",
        "match_direction": "out",
        "match_amount_min": "100",
        "match_amount_max": "200",
        "match_memo_regex": "(?i)rent",
        "match_counter_party_regex": "Landlord",
        "set_account_id": rent["id"],
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    # Explicit null clears nullable predicates.  Clearing min in the same
    # request means max=50 is a valid range; omitted fields remain unchanged.
    r = client.patch(f"/api/v1/bank-rules/{rid}", headers=HEAD, json={
        "match_direction": None,
        "match_amount_min": None,
        "match_amount_max": "50",
        "match_memo_regex": None,
        "match_counter_party_regex": None,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_direction"] is None
    assert body["match_amount_min"] is None
    assert body["match_amount_max"] == "50.00"
    assert body["match_memo_regex"] is None
    assert body["match_counter_party_regex"] is None
    assert body["description"] == "Clearable predicates"


def test_rule_patch_rejects_null_for_required_columns(client, accounts):
    rent = accounts["6100"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "description": "Required fields",
        "set_account_id": rent["id"],
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    for field in (
        "priority", "is_active", "description", "set_account_id", "set_tax_code"
    ):
        r = client.patch(
            f"/api/v1/bank-rules/{rid}", headers=HEAD, json={field: None}
        )
        assert r.status_code == 422, (field, r.text)


def test_rule_bad_amount_range_rejected(client, accounts):
    rent = accounts["6100"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "description": "Bad range",
        "match_amount_min": "100",
        "match_amount_max": "50",
        "set_account_id": rent["id"],
    })
    assert r.status_code == 400


def test_rule_matching_suggests_account_on_preview(client, accounts, biz_bank):
    rent = accounts["6100"]
    # Create rule: any OUT memo matching "rent" → account 6100 (Rent)
    client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 50,
        "description": "Office rent",
        "match_direction": "out",
        "match_memo_regex": "(?i)rent",
        "set_account_id": rent["id"],
        "set_tax_code": "standard",
    })

    csv = (
        "Date,Description,Debit,Credit\n"
        "2026-05-01,Office rent May,1500.00,\n"
        "2026-05-02,Random other,200.00,\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    rows = r.json()["rows"]
    assert rows[0]["suggested_account_id"] == rent["id"]
    assert rows[0]["matched_rule_description"] == "Office rent"
    assert rows[1]["suggested_account_id"] is None


def test_rule_matching_suggests_gst_amount_for_gst_bearing_tax_codes(client, accounts, biz_bank):
    sales = accounts["4000"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 50,
        "description": "Invoice payment",
        "match_direction": "in",
        "match_memo_regex": "SINV",
        "set_account_id": sales["id"],
        "set_tax_code": "standard",
    })
    assert r.status_code == 201, r.text

    csv = "Date,Description,Credit\n2024-07-05,Invoice payment SINV-001,110.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["suggested_account_id"] == sales["id"]
    assert row["suggested_tax_code"] == "standard"
    assert row["suggested_gst_amount"] == "10.00"
    assert row["suggestion_source"] == "rule"


def test_rule_matching_suggests_zero_gst_for_non_gst_tax_codes(client, accounts, biz_bank):
    other = accounts["6900"]
    tax_codes = ["gst_free", "input_taxed", "none"]
    for i, tax_code in enumerate(tax_codes):
        r = client.post("/api/v1/bank-rules", headers=HEAD, json={
            "priority": 50 + i,
            "description": f"No GST {tax_code}",
            "match_direction": "out",
            "match_memo_regex": f"NO_GST_{i}",
            "set_account_id": other["id"],
            "set_tax_code": tax_code,
        })
        assert r.status_code == 201, r.text

    csv = (
        "Date,Description,Debit\n"
        "2024-07-05,NO_GST_0 item,110.00\n"
        "2024-07-06,NO_GST_1 item,220.00\n"
        "2024-07-07,NO_GST_2 item,330.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [row["suggested_tax_code"] for row in rows] == tax_codes
    assert [row["suggested_gst_amount"] for row in rows] == ["0.00", "0.00", "0.00"]


def test_preview_uses_memo_heuristics_when_no_rule_matches(client, accounts, biz_bank):
    csv = (
        "Date,Description,Debit,Credit\n"
        "2024-07-05,Telstra bill,110.00,\n"
        "2024-07-06,Officeworks receipt,55.00,\n"
        "2024-07-07,Rent payment,220.00,\n"
        "2024-07-08,Random transfer,10.00,\n"
        "2024-07-09,Payroll July,500.00,\n"
        "2024-07-10,Fresh morning tea,76.80,\n"
        "2024-07-11,Laptop purchase capital,2420.00,\n"
        "2024-07-12,Card settlement micro advisory,,990.00\n"
    )
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]

    assert rows[0]["suggested_account_id"] == accounts["6200"]["id"]
    assert rows[0]["suggested_tax_code"] == "standard"
    assert rows[0]["suggested_gst_amount"] == "10.00"
    assert rows[0]["suggestion_source"] == "heuristic"

    assert rows[1]["suggested_account_id"] == accounts["6400"]["id"]
    assert rows[1]["suggested_tax_code"] == "standard"
    assert rows[1]["suggestion_source"] == "heuristic"

    assert rows[2]["suggested_account_id"] == accounts["6100"]["id"]
    assert rows[2]["suggested_tax_code"] == "gst_free"
    assert rows[2]["suggested_gst_amount"] == "0.00"

    assert rows[3]["suggested_account_id"] is None
    assert rows[3]["suggestion_source"] is None

    assert rows[4]["suggested_account_id"] == accounts["6000"]["id"]
    assert rows[4]["suggested_tax_code"] == "none"
    assert rows[4]["suggested_gst_amount"] == "0.00"

    assert rows[5]["suggested_account_id"] == accounts["6400"]["id"]
    assert rows[5]["suggested_tax_code"] == "gst_free"

    assert rows[6]["suggested_account_id"] == accounts["1700"]["id"]
    assert rows[6]["suggested_tax_code"] == "capital"
    assert rows[6]["suggested_gst_amount"] == "220.00"

    assert rows[7]["suggested_account_id"] == accounts["4000"]["id"]
    assert rows[7]["suggested_tax_code"] == "standard"
    assert rows[7]["suggestion_source"] == "heuristic"


def test_inbound_client_payment_heuristic_stays_out_of_expense_accounts(client, accounts, biz_bank):
    csv = "Date,Description,Credit\n2024-07-12,Client payment invoice 123,3500.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["parsed"]["direction"] == "in"
    assert row["suggested_account_id"] == accounts["4000"]["id"]
    assert row["suggested_tax_code"] == "standard"
    assert row["suggestion_source"] == "heuristic"


def test_direction_unsafe_rule_is_ignored_before_bas(client, accounts, biz_bank):
    """A misconfigured automatic rule must not turn a customer receipt into a
    purchase refund. That cascades into negative G11/1B on BAS, so preview
    ignores direction-unsafe rule accounts and falls back to safe heuristics."""
    travel = accounts["6310"]
    sales = accounts["4000"]
    r = client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 1,
        "description": "Bad inbound client rule",
        "match_direction": "in",
        "match_memo_regex": "(?i)client payment",
        "set_account_id": travel["id"],
        "set_tax_code": "standard",
    })
    assert r.status_code == 201, r.text

    csv = "Date,Description,Credit\n2026-05-10,Client payment invoice 123,1100.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["parsed"]["direction"] == "in"
    assert row["suggested_account_id"] == sales["id"]
    assert row["suggested_tax_code"] == "standard"
    assert row["suggested_gst_amount"] == "100.00"
    assert row["suggestion_source"] == "heuristic"

    payload_row = {
        "occurred_at": row["parsed"]["occurred_at"],
        "direction": row["parsed"]["direction"],
        "amount": row["parsed"]["amount"],
        "dedup_key": row["dedup_key"],
        "memo": row["parsed"]["memo"],
        "counter_party_name": row["parsed"]["counter_party_name"],
        "account_id": row["suggested_account_id"],
        "tax_code": row["suggested_tax_code"],
        "gst_amount": row["suggested_gst_amount"],
    }
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [payload_row]},
    )
    assert r.status_code == 200, r.text

    r = client.get(
        "/api/v1/reports/bas",
        headers=HEAD,
        params={"fy_year": 2026, "quarter": 4},
    )
    assert r.status_code == 200, r.text
    bas = r.json()
    assert Decimal(bas["g1_total_sales"]) == Decimal("1100.00")
    assert Decimal(bas["one_a_gst_on_sales"]) == Decimal("100.00")
    assert Decimal(bas["total_purchases"]) == Decimal("0.00")
    assert Decimal(bas["one_b_gst_on_purchases"]) == Decimal("0.00")
    assert Decimal(bas["net_gst_payable"]) == Decimal("100.00")


def test_counter_party_header_with_hyphen_imports_and_persists(client, biz_bank):
    csv = "Date,Description,Counter-party,Credit\n2024-07-12,Invoice 123,Jane Sample,3500.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["counter_party_name"] is not None
    row = body["rows"][0]
    assert row["parsed"]["counter_party_name"] == "Jane Sample"

    payload_row = {
        "occurred_at": row["parsed"]["occurred_at"],
        "direction": row["parsed"]["direction"],
        "amount": row["parsed"]["amount"],
        "dedup_key": row["dedup_key"],
        "memo": row["parsed"]["memo"],
        "counter_party_name": row["parsed"]["counter_party_name"],
        "account_id": row["suggested_account_id"],
        "tax_code": row["suggested_tax_code"],
        "gst_amount": row["suggested_gst_amount"] or "0.00",
    }
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [payload_row]},
    )
    assert r.status_code == 200, r.text

    r = client.get(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
    )
    assert r.status_code == 200, r.text
    assert r.json()[0]["counter_party_name"] == "Jane Sample"


def test_rule_priority_wins(client, accounts, biz_bank):
    rent = accounts["6100"]
    other = accounts["6900"]   # Other Expenses
    # Two rules both match "rent"; lower priority should win.
    client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 100,
        "description": "Catch-all OUT → Other Expenses",
        "match_direction": "out",
        "set_account_id": other["id"],
    })
    client.post("/api/v1/bank-rules", headers=HEAD, json={
        "priority": 10,
        "description": "Rent → 6100",
        "match_direction": "out",
        "match_memo_regex": "(?i)rent",
        "set_account_id": rent["id"],
    })

    csv = "Date,Description,Debit,Credit\n2026-05-01,Office rent,1500.00,\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.json()["rows"][0]["suggested_account_id"] == rent["id"]


def test_commit_applies_account_and_tax_code(client, accounts, biz_bank):
    rent = accounts["6100"]
    csv = "Date,Description,Debit,Credit\n2026-05-01,Office rent,1500.00,\n"
    r = _upload(client, biz_bank["id"], csv)
    rows = r.json()["rows"]
    commit = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [
            {
                "occurred_at": rows[0]["parsed"]["occurred_at"],
                "direction": rows[0]["parsed"]["direction"],
                "amount": rows[0]["parsed"]["amount"],
                "dedup_key": rows[0]["dedup_key"],
                "memo": rows[0]["parsed"]["memo"],
                "account_id": rent["id"],
                "tax_code": "standard",
                "gst_amount": "136.36",
            }
        ]},
    )
    assert commit.json()["created"] == 1
    # Verify the txn lands with all the fields wired.
    listed = client.get(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=HEAD,
    ).json()
    assert len(listed) == 1
    txn = listed[0]
    assert txn["account_id"] == rent["id"]
    assert txn["tax_code"] == "standard"
    assert Decimal(txn["gst_amount"]) == Decimal("136.36")


def test_create_bank_account_happy_path_and_list(client):
    r = client.post(
        "/api/v1/bank-accounts",
        headers=HEAD,
        json={
            "name": "NAB savings",
            "opening_balance": "20000.00",
            "bsb": "082-001",
            "account_number": "123456789",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "NAB savings"
    assert Decimal(created["opening_balance"]) == Decimal("20000.00")

    listed = client.get("/api/v1/bank-accounts", headers=HEAD).json()
    assert any(a["id"] == created["id"] for a in listed)


def test_create_bank_account_duplicate_name_returns_409(client):
    payload = {"name": "ANZ receivables"}
    first = client.post("/api/v1/bank-accounts", headers=HEAD, json=payload)
    assert first.status_code == 201, first.text

    duplicate = client.post("/api/v1/bank-accounts", headers=HEAD, json=payload)
    assert duplicate.status_code == 409, duplicate.text


def test_patch_bank_account_renames_and_updates_details(client, biz_bank):
    r = client.patch(
        f"/api/v1/bank-accounts/{biz_bank['id']}",
        headers=HEAD,
        json={"name": "CBA business cheque", "bsb": "062-001", "account_number": "987654321"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "CBA business cheque"
    assert body["bsb"] == "062-001"
    assert body["account_number"] == "987654321"


def test_commit_rejects_missing_or_inactive_account(client, accounts, biz_bank):
    rent = accounts["6100"]
    csv = "Date,Description,Debit,Credit\n2026-05-01,Office rent,1500.00,\n"
    row = _upload(client, biz_bank["id"], csv).json()["rows"][0]

    payload_row = {
        "occurred_at": row["parsed"]["occurred_at"],
        "direction": row["parsed"]["direction"],
        "amount": row["parsed"]["amount"],
        "dedup_key": row["dedup_key"],
        "memo": row["parsed"]["memo"],
        "account_id": 999999,
        "tax_code": "standard",
        "gst_amount": "0.00",
    }
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [payload_row]},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "Row 1: account_id does not reference an existing account"
    )
    assert payload_row["memo"] not in r.json()["detail"]

    client.patch(
        f"/api/v1/accounts/{rent['id']}",
        headers=HEAD,
        json={"active": False},
    )
    payload_row["account_id"] = rent["id"]
    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [payload_row]},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "Row 1: account_id references an inactive account"
    )
    assert payload_row["memo"] not in r.json()["detail"]


def test_commit_skips_duplicate_rows_inside_same_payload(client, biz_bank):
    csv = "Date,Description,Credit\n2026-05-01,Salary,5000.00\n"
    row = _upload(client, biz_bank["id"], csv).json()["rows"][0]
    payload_row = {
        "occurred_at": row["parsed"]["occurred_at"],
        "direction": row["parsed"]["direction"],
        "amount": row["parsed"]["amount"],
        "dedup_key": row["dedup_key"],
        "memo": row["parsed"]["memo"],
    }

    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={"rows": [payload_row, payload_row]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 1, "skipped_duplicates": 1}


def test_inactive_bank_account_rejects_manual_entry_and_import(client, biz_bank):
    r = client.patch(
        f"/api/v1/bank-accounts/{biz_bank['id']}",
        headers=HEAD,
        json={"is_active": False},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/transactions",
        headers=manual_transaction_headers(HEAD),
        json={
            "direction": "in",
            "amount": "100.00",
            "occurred_at": "2026-05-01",
            "memo": "Late entry",
        },
    )
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]

    csv = "Date,Description,Credit\n2026-05-01,Salary,5000.00\n"
    r = _upload(client, biz_bank["id"], csv)
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]

    r = client.post(
        f"/api/v1/bank-accounts/{biz_bank['id']}/import/commit",
        headers=HEAD,
        json={
            "rows": [
                {
                    "occurred_at": "2026-05-01",
                    "direction": "in",
                    "amount": "5000.00",
                    "dedup_key": "inactive-test",
                    "memo": "Salary",
                }
            ]
        },
    )
    assert r.status_code == 400
    assert "inactive" in r.json()["detail"]


# --- Unit: amount parser edge cases (BUG-F6 parenthesised negatives, BUG-B7 sci notation) ---


def test_parse_decimal_edge_cases():
    from app.services.excel_import import _parse_decimal

    # Accounting-style negatives.
    assert _parse_decimal("(250.00)") == "-250.00"
    assert _parse_decimal("(1,234.50)") == "-1234.50"
    # Currency symbols + thousands separators still work.
    assert _parse_decimal("$1,000.00") == "1000.00"
    assert _parse_decimal("-500") == "-500.00"
    # Scientific notation is rejected (never a real bank amount).
    assert _parse_decimal("1e5") is None
    assert _parse_decimal("1E5") is None
    # Garbage / malformed.
    assert _parse_decimal("abc") is None
    assert _parse_decimal("1.2.3") is None
    # Empty / blank.
    assert _parse_decimal("") is None
    assert _parse_decimal(None) is None
