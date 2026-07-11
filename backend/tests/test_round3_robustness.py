"""Round-3 robustness regressions (docs/audits/2026-06-10-deep-audit.md ROUND 3).

Closes gaps left by round-2's partial fixes:

* P1 — null-PATCH guards were incomplete. The InvoiceUpdate guard covered only
  5 of its NOT-NULL fields, and ClientUpdate had no guard at all, so an explicit
  JSON null on a NOT-NULL column still 500'd. Each NOT-NULL field sent as an
  explicit null must now 422; omitting it must still 200.
* P2 — the ReDoS heuristic only caught nested quantifiers `(a+)+`. Quantified
  alternations `(a|a)*` / `(\\d|\\d)+` and backreferences `(a+)\\1` were accepted
  yet backtrack catastrophically. They must now be rejected, while legit
  patterns (top-level `|`, unquantified groups, `\\d{4}-\\d{2}`, …) still pass.
"""

from __future__ import annotations

import shutil
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
    return {a["code"]: a for a in r.json()}


def _invoice_payload(accounts, *, number="R3-1"):
    return {
        "direction": "AR",
        "contact_name": "R3 Customer",
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


def _create_invoice(client, accounts, *, number="R3-1"):
    r = client.post("/api/v1/invoices", headers=HEAD, json=_invoice_payload(accounts, number=number))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# P1: every InvoiceUpdate NOT-NULL field rejects explicit null; omit still works
# ---------------------------------------------------------------------------

# The fields round-2 missed (round-2 already covered direction/issue_date/
# subtotal/gst_amount/total; here we assert the full NOT-NULL set).
_INVOICE_NOT_NULL_FIELDS = (
    "direction",
    "contact_id",
    "invoice_number",
    "issue_date",
    "subtotal",
    "gst_amount",
    "total",
    "gst_inclusive",
    "status",
    "paid_amount",
)


def test_invoice_patch_explicit_null_on_all_not_null_fields_rejected(client, accounts):
    inv = _create_invoice(client, accounts, number="R3-NULL")
    for field in _INVOICE_NOT_NULL_FIELDS:
        r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HEAD, json={field: None})
        assert r.status_code == 422, f"{field} null should be 422: {r.text}"


def test_invoice_patch_omitting_not_null_fields_still_works(client, accounts):
    inv = _create_invoice(client, accounts, number="R3-OMIT")
    # Omitting every NOT-NULL field (a no-op PATCH of an allowed nullable field)
    # must still succeed and leave the values intact.
    r = client.patch(f"/api/v1/invoices/{inv['id']}", headers=HEAD, json={"notes": "kept"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notes"] == "kept"
    assert body["invoice_number"] == "R3-OMIT"
    assert body["gst_inclusive"] is True


# ---------------------------------------------------------------------------
# P1: ClientUpdate NOT-NULL fields reject explicit null; omit still works
# ---------------------------------------------------------------------------


def _create_client(client, *, name="R3 Client"):
    r = client.post("/api/v1/clients", headers=HEAD, json={"display_name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_client_patch_explicit_null_on_not_null_fields_rejected(client):
    cl = _create_client(client, name="R3 Null Client")
    for field in ("display_name", "is_active"):
        r = client.patch(f"/api/v1/clients/{cl['id']}", headers=HEAD, json={field: None})
        assert r.status_code == 422, f"{field} null should be 422: {r.text}"


def test_client_patch_omitting_not_null_fields_still_works(client):
    cl = _create_client(client, name="R3 Omit Client")
    # A nullable field can still be patched (and set to null) without touching
    # the NOT-NULL fields.
    r = client.patch(f"/api/v1/clients/{cl['id']}", headers=HEAD, json={"email": "a@b.co"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "a@b.co"
    assert body["display_name"] == "R3 Omit Client"
    assert body["is_active"] is True


# ---------------------------------------------------------------------------
# P2: broadened ReDoS guard rejects quantified alternation + backreferences,
# while legit patterns still pass.
# ---------------------------------------------------------------------------


def _make_rule(accounts, regex):
    return {
        "priority": 50,
        "description": "redos test",
        "match_memo_regex": regex,
        "set_account_id": accounts["6100"]["id"],
        "set_tax_code": "standard",
    }


_CATASTROPHIC_REGEXES = (
    r"(a|a)*$",      # quantified alternation, overlapping branches
    r"(\d|\d)+$",    # quantified alternation, duplicate branches
    r"(a+)\1",       # backreference
    r"(a+)+$",       # existing nested-quantifier case still rejected
)

_LEGIT_REGEXES = (
    r"(?i)rent|lease",        # top-level | (group is not quantified)
    r"Officeworks|rent",      # bare top-level alternation
    r"(rent|lease|mortgage)",  # unquantified group with alternation
    r"(abc)+",                 # quantified group, no alternation
    r"\d{4}-\d{2}",
    r"INV-\d+",
)


def test_catastrophic_regexes_rejected(client, accounts):
    for regex in _CATASTROPHIC_REGEXES:
        r = client.post("/api/v1/bank-rules", headers=HEAD, json=_make_rule(accounts, regex))
        assert r.status_code == 422, f"{regex!r} should be rejected: {r.text}"


def test_legit_regexes_still_accepted(client, accounts):
    for i, regex in enumerate(_LEGIT_REGEXES):
        rule = _make_rule(accounts, regex)
        rule["priority"] = 50 + i  # keep each rule distinct
        r = client.post("/api/v1/bank-rules", headers=HEAD, json=rule)
        assert r.status_code == 201, f"{regex!r} should be accepted: {r.text}"
