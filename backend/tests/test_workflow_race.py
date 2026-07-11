"""Race-condition coverage for document creation and numbering."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECT_ROOT = ROOT.parent
HDR = {"X-Company-Id": "acme"}


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
    from app.main import app  # noqa: WPS433

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def company_headers(client):
    r = client.post(
        "/api/v1/companies",
        json={"id": "acme", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Example Migration Services", "gst_registered": False},
    )
    assert r.status_code == 201, r.text
    HDR["X-Company-Generation"] = r.json()["generation_id"]
    return HDR


def _create_client(client, name: str) -> dict:
    r = client.post(
        "/api/v1/clients",
        headers=HDR,
        json={"display_name": name, "email": f"{name.lower().replace(' ', '.')}@example.com"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_receipt(
    client, name: str, *, override: str | None = None, amount: str = "100.00"
) -> dict:
    client_row = _create_client(client, name)
    payload = {
        "doc_type": "receipt",
        "issue_date": "2026-05-28",
        "client_ref_id": client_row["id"],
        "lines": [{"description": "Migration service", "quantity": "1", "unit_price": amount}],
    }
    if override is not None:
        payload["doc_number_override"] = override
    r = client.post("/api/v1/outgoing", headers=HDR, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_counter_allocation_under_concurrent_creates(client, company_headers):
    """Concurrent receipt creates receive unique numbers in one contiguous block."""
    clients = [_create_client(client, f"Counter Client {i}") for i in range(6)]

    def create(index: int):
        return client.post(
            "/api/v1/outgoing",
            headers=company_headers,
            json={
                "doc_type": "receipt",
                "issue_date": "2026-05-28",
                "client_ref_id": clients[index]["id"],
                "lines": [
                    {"description": "Migration service", "quantity": "1", "unit_price": "100.00"}
                ],
            },
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(create, range(6)))

    assert all(response.status_code == 201 for response in responses), [r.text for r in responses]
    numbers = sorted(response.json()["doc_number"] for response in responses)
    assert numbers == [f"RCT-2026-{serial:04d}-1" for serial in range(1, 7)]


def test_manual_override_advances_shared_counter(client, company_headers):
    """A standard manual override advances the shared sequence for later auto numbers."""
    receipt = _create_receipt(client, "Override Advance Co", override="RCT-2026-0050-1")
    assert receipt["doc_number"] == "RCT-2026-0050-1"

    nxt = _create_receipt(client, "Next Auto Co")
    assert nxt["doc_number"] == "RCT-2026-0051-1"


def test_manual_override_unrecognised_format_no_bump(client, company_headers):
    """A custom override format leaves the shared sequence untouched."""
    receipt = _create_receipt(client, "Custom Override Co", override="CUSTOM-X-Y")
    assert receipt["doc_number"] == "CUSTOM-X-Y"

    nxt = _create_receipt(client, "First Auto Co")
    assert nxt["doc_number"] == "RCT-2026-0001-1"
