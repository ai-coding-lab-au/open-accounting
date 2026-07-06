"""Company creation must be atomic (P1-24).

The master Company row used to be committed BEFORE the per-company DB was
provisioned/seeded — a provisioning failure left a master record pointing
at a half-initialised ledger, and the retry hit 409. Now provisioning runs
first and the master row only commits on success.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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

    with TestClient(app) as c:
        yield c


def test_seed_failure_persists_no_company_row_and_retry_succeeds(client, monkeypatch):
    from app.api.v1 import companies as companies_api

    state = {"fail": True}
    real_seed = companies_api.seed_default_coa

    def flaky_seed(session):
        if state["fail"]:
            raise RuntimeError("simulated seeding failure")
        return real_seed(session)

    monkeypatch.setattr(companies_api, "seed_default_coa", flaky_seed)

    payload = {"id": "tc", "marn": "1234567", "registered_agent_name": "Test Agent", "name": "Test Pty Ltd"}
    with pytest.raises(RuntimeError, match="simulated seeding failure"):
        client.post("/api/v1/companies", json=payload)

    # No half-created master row.
    r = client.get("/api/v1/companies")
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()] == []

    # Retry succeeds once provisioning works — must not be a 409.
    state["fail"] = False
    r = client.post("/api/v1/companies", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "tc"


def test_company_signer_fields_are_retired(client):
    """Company creation no longer stores practitioner credentials.

    Signing credentials live on StaffMember rows and Service Agreements must
    explicitly select an active MARA staff member.
    """
    r = client.post("/api/v1/companies", json={"id": "nocred", "name": "No Cred Co"})
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/v1/companies",
        json={
            "id": "mara",
            "name": "MARA Co",
            "marn": "1234567",
            "registered_agent_name": "Jane Smith",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "marn" not in body
    assert "registered_agent_name" not in body


def test_init_master_db_drops_stale_columns_and_allows_new_company(monkeypatch, request):
    """An existing master.db from before the trust/signer refactor carries
    retired columns. bank_is_trust_account was NOT NULL — so INSERTing any new
    company hit a NOT NULL violation (the model no longer supplies it).
    init_master_db must drop the stale columns so creation works again.
    """
    import sqlite3

    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        import shutil

        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]

    from app.config import settings

    # Build an OLD master.db with the retired columns (bank_is_trust_account
    # NOT NULL, plus the 4 nullable signer columns).
    mp = settings.master_db_path
    mp.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(mp)
    con.executescript(
        """
        CREATE TABLE companies (
            id VARCHAR(32) PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            country VARCHAR(2) NOT NULL DEFAULT 'AU',
            base_currency VARCHAR(3) NOT NULL DEFAULT 'AUD',
            fy_start_month INTEGER NOT NULL DEFAULT 7,
            gst_registered BOOLEAN NOT NULL DEFAULT 1,
            bank_is_trust_account BOOLEAN NOT NULL,
            registered_agent_name VARCHAR(200),
            marn VARCHAR(20),
            registered_legal_practitioner_name VARCHAR(200),
            lpn VARCHAR(20),
            default_payment_terms_days INTEGER NOT NULL DEFAULT 28,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
        );
        INSERT INTO companies (id, name, bank_is_trust_account) VALUES ('old', 'Old Co', 0);
        """
    )
    con.commit()
    con.close()

    from app.db.master import MasterSession, init_master_db
    from app.models.master import Company

    init_master_db()

    con = sqlite3.connect(mp)
    cols = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    con.close()
    for stale in (
        "bank_is_trust_account",
        "registered_agent_name",
        "marn",
        "registered_legal_practitioner_name",
        "lpn",
    ):
        assert stale not in cols, f"{stale} should have been dropped"

    # Old data preserved, and a NEW company can now be inserted.
    s = MasterSession()
    try:
        s.add(Company(id="new", name="New Co Pty Ltd"))
        s.commit()
        ids = {c.id for c in s.query(Company).all()}
    finally:
        s.close()
    assert ids == {"old", "new"}

    # Idempotent: a second init drops nothing.
    assert not any(c.startswith("drop:") for c in init_master_db())


def test_delete_company_removes_db_registry_and_allows_recreate(client):
    from app.config import settings

    payload = {"id": "killme", "name": "Kill Me Pty", "marn": "1234567",
               "registered_agent_name": "Jane Smith"}
    assert client.post("/api/v1/companies", json=payload).status_code == 201

    folder = settings.company_dir("killme")
    assert folder.exists() and (folder / "books.db").exists()

    # Missing confirm → 422 (required query param).
    assert client.delete("/api/v1/companies/killme").status_code == 422
    # Wrong confirm → 400.
    assert client.delete("/api/v1/companies/killme?confirm=wrong").status_code == 400
    # Company still present after the rejected deletes.
    assert [c["id"] for c in client.get("/api/v1/companies").json()] == ["killme"]

    # Correct confirm → 204, folder gone, registry empty.
    assert client.delete("/api/v1/companies/killme?confirm=killme").status_code == 204
    assert not folder.exists()
    assert client.get("/api/v1/companies").json() == []

    # Deleting a non-existent company → 404.
    assert client.delete("/api/v1/companies/nope?confirm=nope").status_code == 404

    # The id is reusable — the cached engine was disposed, so re-create works.
    assert client.post("/api/v1/companies", json=payload).status_code == 201
