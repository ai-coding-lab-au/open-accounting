"""Client API input, PATCH, conflict, and concurrency consistency tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECT_ROOT = ROOT.parent
HEAD = {"X-Company-Id": "client-consistency"}


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
        company = test_client.post(
            "/api/v1/companies",
            json={
                "id": HEAD["X-Company-Id"],
                "marn": "1234567",
                "registered_agent_name": "Test Agent",
                "name": "Client Consistency Pty Ltd",
            },
        )
        assert company.status_code == 201, company.text
        HEAD["X-Company-Generation"] = company.json()["generation_id"]
        yield test_client


def _create(client, name: str, **overrides) -> dict:
    payload = {
        "display_name": name,
        "email": "client@example.test",
        "phone": "0400 000 000",
        "address": "1 Client Street",
        "client_ref": "CLIENT-001",
        "notes": "Client note",
    }
    payload.update(overrides)
    response = client.post("/api/v1/clients", headers=HEAD, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_client_create_normalises_ui_payload(client):
    body = _create(
        client,
        "  Alpha Migration Client  ",
        client_ref="  CLIENT-001  ",
    )
    assert body["display_name"] == "Alpha Migration Client"
    assert body["client_ref"] == "CLIENT-001"
    assert body["phone"] == "0400000000"

    empty_ref = _create(
        client,
        "No Reference Client",
        client_ref="   ",
        email=None,
    )
    assert empty_ref["client_ref"] is None

    blank = client.post(
        "/api/v1/clients",
        headers=HEAD,
        json={"display_name": "   "},
    )
    assert blank.status_code == 422, blank.text

    patch_blank = client.patch(
        f"/api/v1/clients/{body['id']}",
        headers=HEAD,
        json={"display_name": "   "},
    )
    assert patch_blank.status_code == 422, patch_blank.text


def test_client_ui_patch_null_clears_and_omission_preserves(client):
    body = _create(client, "Full UI Client")

    # ClientDetailDrawer.tsx sends this full shape and uses null for optional
    # inputs the user cleared.
    updated = client.patch(
        f"/api/v1/clients/{body['id']}",
        headers=HEAD,
        json={
            "display_name": "  Full UI Client Renamed  ",
            "email": None,
            "phone": None,
            "address": None,
            "client_ref": None,
            "notes": None,
            "is_active": False,
        },
    )
    assert updated.status_code == 200, updated.text
    result = updated.json()
    assert result["display_name"] == "Full UI Client Renamed"
    assert result["is_active"] is False
    for field in ("email", "phone", "address", "client_ref", "notes"):
        assert result[field] is None

    omitted = client.patch(
        f"/api/v1/clients/{body['id']}",
        headers=HEAD,
        json={"email": "restored@example.test"},
    )
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["display_name"] == "Full UI Client Renamed"
    assert omitted.json()["is_active"] is False


@pytest.mark.parametrize("field", ["display_name", "is_active"])
def test_client_patch_rejects_explicit_null_for_required_fields(client, field):
    body = _create(client, f"Required {field}")
    response = client.patch(
        f"/api/v1/clients/{body['id']}",
        headers=HEAD,
        json={field: None},
    )
    assert response.status_code == 422, response.text


def test_client_name_conflicts_are_case_insensitive_on_create_and_patch(client):
    alpha = _create(client, "Alpha Client")

    duplicate = client.post(
        "/api/v1/clients",
        headers=HEAD,
        json={"display_name": "  ALPHA CLIENT  "},
    )
    assert duplicate.status_code == 409, duplicate.text

    bravo = _create(client, "Bravo Client", client_ref="CLIENT-002")
    clash = client.patch(
        f"/api/v1/clients/{bravo['id']}",
        headers=HEAD,
        json={"display_name": "alpha client"},
    )
    assert clash.status_code == 409, clash.text

    same_record = client.patch(
        f"/api/v1/clients/{alpha['id']}",
        headers=HEAD,
        json={"display_name": "  ALPHA CLIENT  "},
    )
    assert same_record.status_code == 200, same_record.text
    assert same_record.json()["display_name"] == "ALPHA CLIENT"


def test_concurrent_client_name_check_is_serialised(client):
    def create(name: str):
        return client.post(
            "/api/v1/clients",
            headers=HEAD,
            json={"display_name": name},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, ("Race Client", "race client")))

    assert sorted(response.status_code for response in responses) == [201, 409]
    rows = client.get(
        "/api/v1/clients",
        headers=HEAD,
        params={"q": "Race Client", "active_only": False},
    )
    assert rows.status_code == 200, rows.text
    assert len(rows.json()) == 1


def test_client_integrity_error_is_a_safe_conflict(client, monkeypatch):
    body = _create(client, "Integrity Client")

    from sqlalchemy.orm import Session

    def fail_commit(_session):
        raise IntegrityError("SECRET SQL", {}, RuntimeError("raw database detail"))

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = client.patch(
        f"/api/v1/clients/{body['id']}",
        headers=HEAD,
        json={"notes": "changed"},
    )
    assert response.status_code == 409, response.text
    assert "SECRET SQL" not in response.text
    assert "raw database detail" not in response.text
