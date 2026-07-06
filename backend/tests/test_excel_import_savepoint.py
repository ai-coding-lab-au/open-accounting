"""Tests for /api/v1/invoices/import-excel-rows.

Verifies the per-row SAVEPOINT pattern: when one row fails (e.g. duplicate
invoice_number violating the unique constraint), the rest of the batch still
commits. Before the fix, one IntegrityError poisoned the whole transaction
and every subsequent row was rejected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECT_ROOT = ROOT.parent
HDR = {"X-Company-Id": "tc"}


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
        r = c.post("/api/v1/companies", json={"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"})
        assert r.status_code == 201, r.text
        yield c


MAPPING = {
    "direction": 0,
    "contact_name": 1,
    "invoice_number": 2,
    "issue_date": 3,
    "total": 4,
}


def _row(row_no: int, *, direction: str, contact: str, number: str, total: str):
    return {
        "row_no": row_no,
        "raw": [direction, contact, number, "2026-05-01", total],
    }


def test_excel_import_with_empty_body_returns_422(client):
    r = client.post("/api/v1/invoices/import-excel-rows", headers=HDR, json={})
    assert r.status_code == 422


def test_duplicate_in_middle_does_not_poison_batch(client):
    # Three rows: A (ok), B (duplicate of A — same direction+number+contact),
    # C (ok). Without SAVEPOINTs, B's IntegrityError would abort A and C too.
    payload = {
        "mapping": MAPPING,
        "rows": [
            _row(2, direction="AP", contact="ACME Supplier", number="INV-001", total="100.00"),
            _row(3, direction="AP", contact="ACME Supplier", number="INV-001", total="100.00"),
            _row(4, direction="AP", contact="Other Supplier", number="INV-002", total="200.00"),
        ],
        "direction_default": "AP",
    }
    r = client.post("/api/v1/invoices/import-excel-rows", headers=HDR, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 2
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["row"] == 3


def test_validation_failure_in_middle_does_not_poison_batch(client):
    # Row 3 is missing invoice_number — it should be skipped with a reason,
    # rows 2 and 4 should still commit cleanly.
    payload = {
        "mapping": MAPPING,
        "rows": [
            _row(2, direction="AP", contact="ACME", number="INV-100", total="100.00"),
            {"row_no": 3, "raw": ["AP", "BetaCo", "", "2026-05-01", "150.00"]},
            _row(4, direction="AP", contact="ACME", number="INV-101", total="200.00"),
        ],
        "direction_default": "AP",
    }
    r = client.post("/api/v1/invoices/import-excel-rows", headers=HDR, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 2
    assert body["skipped"][0]["row"] == 3
    assert "invoice_number" in body["skipped"][0]["reason"].lower()


def test_rows_cap_enforced(client):
    # Pydantic max_length=10000 on rows. 10001 rows should be rejected.
    rows = [
        _row(n, direction="AP", contact=f"S{n}", number=f"X{n}", total="1.00")
        for n in range(10001)
    ]
    r = client.post(
        "/api/v1/invoices/import-excel-rows",
        headers=HDR,
        json={"mapping": MAPPING, "rows": rows, "direction_default": "AP"},
    )
    assert r.status_code == 422, r.text


def test_parse_date_accepts_iso_datetime_string():
    from app.services.excel_import import _parse_date

    assert _parse_date("2024-07-02T00:00:00") == "2024-07-02"
    assert _parse_date("2024-07-02T15:30:00") == "2024-07-02"
    assert _parse_date("2024-07-02") == "2024-07-02"
    assert _parse_date("garbage") is None
