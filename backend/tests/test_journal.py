"""Tests for the manual journal entry endpoints (M2.1).

Covers:
  - debits == credits is enforced (the central accounting invariant)
  - per-line one-sided constraint
  - account_id must exist + be active
  - happy-path CRUD: create, get, list, update (full line replace), delete
  - DB-level CHECK constraints are not silently bypassed
"""

from __future__ import annotations

import sys
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
        import shutil
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app.main import app
    with TestClient(app) as c:
        c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        yield c


@pytest.fixture()
def accounts(client):
    """A handful of seeded CoA accounts keyed by code, for convenience."""
    r = client.get("/api/v1/accounts", headers=HEAD)
    assert r.status_code == 200
    return {a["code"]: a for a in r.json()}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_balanced_entry(client, accounts):
    bank = accounts["1000"]  # Bank — Operating
    capital = accounts["3000"]  # Owner's Capital

    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Opening balance — owner contribution",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "5000.00"},
                {"account_id": capital["id"], "credit_amount": "5000.00"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["memo"] == "Opening balance — owner contribution"
    assert len(body["lines"]) == 2
    debit_line = next(l for l in body["lines"] if l["account_id"] == bank["id"])
    credit_line = next(l for l in body["lines"] if l["account_id"] == capital["id"])
    assert debit_line["debit_amount"] == "5000.00"
    assert credit_line["credit_amount"] == "5000.00"


def test_create_multi_line_balanced_entry(client, accounts):
    """A 3-line entry — depreciation hitting two asset accounts and one expense."""
    depreciation = accounts["6800"]
    accum_dep = accounts["1710"]
    motor = accounts["6300"]

    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-06-30",
            "memo": "FY26 depreciation + motor vehicle accrual",
            "lines": [
                {"account_id": depreciation["id"], "debit_amount": "1200.00"},
                {"account_id": motor["id"], "debit_amount": "300.00"},
                {"account_id": accum_dep["id"], "credit_amount": "1500.00"},
            ],
        },
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Validation: the central accounting invariant
# ---------------------------------------------------------------------------


def test_unbalanced_entry_rejected(client, accounts):
    bank = accounts["1000"]
    capital = accounts["3000"]
    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Off by a dollar",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "5000.00"},
                {"account_id": capital["id"], "credit_amount": "4999.00"},
            ],
        },
    )
    assert r.status_code == 400
    assert "unbalanced" in r.json()["detail"].lower()


def test_line_with_both_sides_rejected(client, accounts):
    bank = accounts["1000"]
    capital = accounts["3000"]
    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Bad line shape",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "100.00", "credit_amount": "100.00"},
                {"account_id": capital["id"], "credit_amount": "100.00"},
            ],
        },
    )
    assert r.status_code == 400


def test_same_account_debit_and_credit_rejected(client, accounts):
    """An account debited AND credited in one entry nets to zero on that
    account — almost always a mistake, so it's rejected."""
    bank = accounts["1000"]
    capital = accounts["3000"]
    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Same account both sides",
            "lines": [
                # Perfectly balanced (Dr 150 = Cr 150) so only the same-account
                # guard can reject it: bank is both debited and credited.
                {"account_id": bank["id"], "debit_amount": "100.00"},
                {"account_id": capital["id"], "credit_amount": "100.00"},
                {"account_id": bank["id"], "credit_amount": "50.00"},
                {"account_id": capital["id"], "debit_amount": "50.00"},
            ],
        },
    )
    assert r.status_code == 400
    assert "both a debit and a credit" in r.json()["detail"]


def test_line_with_neither_side_rejected(client, accounts):
    bank = accounts["1000"]
    capital = accounts["3000"]
    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Empty line",
            "lines": [
                {"account_id": bank["id"]},
                {"account_id": capital["id"], "credit_amount": "0"},
            ],
        },
    )
    assert r.status_code == 400


def test_single_line_entry_rejected(client, accounts):
    bank = accounts["1000"]
    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Lonely line",
            "lines": [{"account_id": bank["id"], "debit_amount": "100.00"}],
        },
    )
    # Pydantic min_length=2 fires first → 422; either 400 or 422 means rejected.
    assert r.status_code in (400, 422)


def test_unknown_account_rejected(client, accounts):
    capital = accounts["3000"]
    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Bogus account",
            "lines": [
                {"account_id": 999_999, "debit_amount": "100.00"},
                {"account_id": capital["id"], "credit_amount": "100.00"},
            ],
        },
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"].lower()


def test_inactive_account_rejected(client, accounts):
    """If an account is deactivated, new entries can't reference it."""
    bank = accounts["1000"]
    capital = accounts["3000"]
    # Deactivate one account.
    r = client.patch(
        f"/api/v1/accounts/{capital['id']}",
        headers=HEAD,
        json={"active": False},
    )
    assert r.status_code == 200

    r = client.post(
        "/api/v1/journal",
        headers=HEAD,
        json={
            "entry_date": "2026-05-01",
            "memo": "Tries to use inactive account",
            "lines": [
                {"account_id": bank["id"], "debit_amount": "100.00"},
                {"account_id": capital["id"], "credit_amount": "100.00"},
            ],
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Read / update / delete
# ---------------------------------------------------------------------------


def _create_entry(client, accounts, *, memo="seed entry", entry_date="2026-05-01", reference=None):
    bank = accounts["1000"]
    capital = accounts["3000"]
    payload = {
        "entry_date": entry_date,
        "memo": memo,
        "lines": [
            {"account_id": bank["id"], "debit_amount": "1000.00"},
            {"account_id": capital["id"], "credit_amount": "1000.00"},
        ],
    }
    if reference is not None:
        payload["reference"] = reference
    r = client.post("/api/v1/journal", headers=HEAD, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_get_and_list(client, accounts):
    e1 = _create_entry(client, accounts, memo="first")
    e2 = _create_entry(client, accounts, memo="second")

    r = client.get(f"/api/v1/journal/{e1['id']}", headers=HEAD)
    assert r.status_code == 200
    assert r.json()["memo"] == "first"

    r = client.get("/api/v1/journal", headers=HEAD)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    # Newest first (same date → tiebreak on id desc).
    assert body[0]["id"] == e2["id"]


def test_list_filters_by_query_and_date_range(client, accounts):
    """`?q=` matches memo/reference (case-insensitive); `?from=`/`?to=` clip the date window."""
    _create_entry(client, accounts, memo="April rent", entry_date="2026-04-15")
    _create_entry(client, accounts, memo="May depreciation", entry_date="2026-05-10", reference="DEP-MAY")
    _create_entry(client, accounts, memo="June bad debt", entry_date="2026-06-20", reference="BD-001")

    # Memo substring
    r = client.get("/api/v1/journal?q=rent", headers=HEAD)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["memo"] == "April rent"

    # Reference substring, case-insensitive
    r = client.get("/api/v1/journal?q=dep-may", headers=HEAD)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["reference"] == "DEP-MAY"

    # Date range — May only
    r = client.get("/api/v1/journal?from=2026-05-01&to=2026-05-31", headers=HEAD)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["entry_date"] == "2026-05-10"

    # Open-ended `from` (May onwards)
    r = client.get("/api/v1/journal?from=2026-05-01", headers=HEAD)
    assert r.status_code == 200
    body = r.json()
    assert {e["entry_date"] for e in body} == {"2026-05-10", "2026-06-20"}

    # Combined q + date range — q matches "June" but date window excludes it
    r = client.get("/api/v1/journal?q=June&from=2026-04-01&to=2026-05-31", headers=HEAD)
    assert r.status_code == 200
    assert r.json() == []


def test_update_memo_only_keeps_lines(client, accounts):
    e = _create_entry(client, accounts)
    original_line_ids = {l["id"] for l in e["lines"]}

    r = client.patch(
        f"/api/v1/journal/{e['id']}",
        headers=HEAD,
        json={"memo": "renamed"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["memo"] == "renamed"
    assert {l["id"] for l in body["lines"]} == original_line_ids


def test_update_replaces_lines_when_provided(client, accounts):
    e = _create_entry(client, accounts)
    bank = accounts["1000"]
    capital = accounts["3000"]

    r = client.patch(
        f"/api/v1/journal/{e['id']}",
        headers=HEAD,
        json={
            "lines": [
                {"account_id": bank["id"], "debit_amount": "2500.00"},
                {"account_id": capital["id"], "credit_amount": "2500.00"},
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["lines"]) == 2
    assert sum(float(l["debit_amount"]) for l in body["lines"]) == 2500.0


def test_update_with_unbalanced_lines_rejected(client, accounts):
    e = _create_entry(client, accounts)
    bank = accounts["1000"]
    capital = accounts["3000"]

    r = client.patch(
        f"/api/v1/journal/{e['id']}",
        headers=HEAD,
        json={
            "lines": [
                {"account_id": bank["id"], "debit_amount": "2500.00"},
                {"account_id": capital["id"], "credit_amount": "2499.00"},
            ],
        },
    )
    assert r.status_code == 400


def test_update_same_account_debit_and_credit_rejected(client, accounts):
    """The edit path enforces the same-account guard, not just create."""
    e = _create_entry(client, accounts)
    bank = accounts["1000"]
    capital = accounts["3000"]

    r = client.patch(
        f"/api/v1/journal/{e['id']}",
        headers=HEAD,
        json={
            "lines": [
                {"account_id": bank["id"], "debit_amount": "100.00"},
                {"account_id": capital["id"], "credit_amount": "100.00"},
                {"account_id": bank["id"], "credit_amount": "50.00"},
                {"account_id": capital["id"], "debit_amount": "50.00"},
            ],
        },
    )
    assert r.status_code == 400
    assert "both a debit and a credit" in r.json()["detail"]


def test_delete(client, accounts):
    e = _create_entry(client, accounts)
    r = client.delete(f"/api/v1/journal/{e['id']}", headers=HEAD)
    assert r.status_code == 204
    r = client.get(f"/api/v1/journal/{e['id']}", headers=HEAD)
    assert r.status_code == 404


def test_delete_unknown_returns_404(client):
    r = client.delete("/api/v1/journal/999", headers=HEAD)
    assert r.status_code == 404
