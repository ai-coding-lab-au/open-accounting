"""Company creation must be atomic (P1-24).

The master Company row used to be committed BEFORE the per-company DB was
provisioned/seeded — a provisioning failure left a master record pointing
at a half-initialised ledger, and the retry hit 409. Now provisioning runs
first and the master row only commits on success.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from uuid import UUID

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
    from app.config import settings

    assert not settings.company_dir("tc").exists()
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
        INSERT INTO companies (id, name, bank_is_trust_account) VALUES ('old2', 'Old Co 2', 0);
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
        companies = {c.id: c for c in s.query(Company).all()}
    finally:
        s.close()
    assert set(companies) == {"old", "old2", "new"}
    assert UUID(companies["old"].generation_id)
    assert UUID(companies["old2"].generation_id)
    assert UUID(companies["new"].generation_id)
    generations = {company.generation_id for company in companies.values()}
    assert len(generations) == 3

    # Idempotent: a second init drops nothing and preserves every generation.
    assert init_master_db() == []
    with MasterSession() as s:
        generations_after = {c.generation_id for c in s.query(Company).all()}
    assert generations_after == generations


def test_delete_company_removes_db_registry_and_allows_recreate(client):
    from app.config import settings

    payload = {"id": "killme", "name": "Kill Me Pty", "marn": "1234567",
               "registered_agent_name": "Jane Smith"}
    created = client.post("/api/v1/companies", json=payload)
    assert created.status_code == 201
    generation_1 = created.json()["generation_id"]
    headers_1 = {
        "X-Company-Id": "killme",
        "X-Company-Generation": generation_1,
    }

    folder = settings.company_dir("killme")
    assert folder.exists() and (folder / "books.db").exists()

    # Missing confirm → 422 (required query param).
    assert client.delete("/api/v1/companies/killme").status_code == 422
    # Wrong confirm → 400.
    assert client.delete(
        "/api/v1/companies/killme?confirm=wrong", headers=headers_1
    ).status_code == 400
    # Company still present after the rejected deletes.
    assert [c["id"] for c in client.get("/api/v1/companies").json()] == ["killme"]

    # Correct confirm → 204, folder gone, registry empty.
    assert client.delete(
        "/api/v1/companies/killme?confirm=killme", headers=headers_1
    ).status_code == 204
    assert not folder.exists()
    assert client.get("/api/v1/companies").json() == []

    # Deleting a non-existent company → 404.
    assert client.delete("/api/v1/companies/nope?confirm=nope").status_code == 404

    # The id is reusable — the cached engine was disposed, so re-create works.
    recreated = client.post("/api/v1/companies", json=payload)
    assert recreated.status_code == 201
    assert recreated.json()["generation_id"] != generation_1


def test_delete_master_commit_failure_restores_staged_company(
    client, monkeypatch
):
    from app.api.v1 import companies as companies_api
    from app.config import settings

    created = client.post(
        "/api/v1/companies", json={"id": "restoreme", "name": "Restore Me Pty"}
    )
    assert created.status_code == 201, created.text
    headers = {
        "X-Company-Id": "restoreme",
        "X-Company-Generation": created.json()["generation_id"],
    }
    company_dir = settings.company_dir("restoreme")
    books_path = settings.company_db_path("restoreme")
    account_ids = [
        row["id"]
        for row in client.get("/api/v1/accounts", headers=headers).json()
    ]

    def fail_commit(_db):
        raise RuntimeError("simulated master delete commit failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(companies_api, "_commit_company_delete", fail_commit)
        with pytest.raises(
            RuntimeError, match="simulated master delete commit failure"
        ):
            client.delete(
                "/api/v1/companies/restoreme?confirm=restoreme", headers=headers
            )

    assert company_dir.is_dir()
    assert books_path.is_file()
    assert list(company_dir.parent.glob(".restoreme.deleting-*")) == []
    assert [row["id"] for row in client.get("/api/v1/companies").json()] == [
        "restoreme"
    ]
    assert [
        row["id"]
        for row in client.get("/api/v1/accounts", headers=headers).json()
    ] == account_ids

    deleted = client.delete(
        "/api/v1/companies/restoreme?confirm=restoreme", headers=headers
    )
    assert deleted.status_code == 204, deleted.text
    assert not company_dir.exists()


def test_generation_rejects_stale_reused_company_before_db_open(client, monkeypatch):
    payload = {"id": "reuse", "name": "First Generation Pty Ltd"}
    first = client.post("/api/v1/companies", json=payload)
    assert first.status_code == 201, first.text
    generation_1 = first.json()["generation_id"]
    headers_1 = {
        "X-Company-Id": "reuse",
        "X-Company-Generation": generation_1,
    }

    valid = client.post(
        "/api/v1/contacts",
        headers=headers_1,
        json={"name": "First Supplier", "kind": "supplier"},
    )
    assert valid.status_code == 201, valid.text

    missing = client.post(
        "/api/v1/contacts",
        headers={"X-Company-Id": "reuse"},
        json={"name": "Missing Generation", "kind": "supplier"},
    )
    assert missing.status_code == 400, missing.text
    assert missing.json()["detail"]["code"] == "MISSING_COMPANY_GENERATION"

    wrong = client.get(
        "/api/v1/accounts",
        headers={
            "X-Company-Id": "reuse",
            "X-Company-Generation": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["detail"]["code"] == "COMPANY_GENERATION_MISMATCH"

    path_mismatch = client.get(
        "/api/v1/companies/reuse",
        headers={
            "X-Company-Id": "some-other-company",
            "X-Company-Generation": generation_1,
        },
    )
    assert path_mismatch.status_code == 409, path_mismatch.text
    assert path_mismatch.json()["detail"]["code"] == "COMPANY_ID_MISMATCH"

    deleted = client.delete(
        "/api/v1/companies/reuse?confirm=reuse", headers=headers_1
    )
    assert deleted.status_code == 204, deleted.text

    second = client.post(
        "/api/v1/companies",
        json={"id": "reuse", "name": "Second Generation Pty Ltd"},
    )
    assert second.status_code == 201, second.text
    generation_2 = second.json()["generation_id"]
    assert generation_2 != generation_1

    stale_metadata = client.get(
        "/api/v1/companies/reuse", headers=headers_1
    )
    assert stale_metadata.status_code == 409, stale_metadata.text
    assert stale_metadata.json()["detail"]["code"] == "COMPANY_GENERATION_MISMATCH"

    stale_delete = client.delete(
        "/api/v1/companies/reuse?confirm=reuse", headers=headers_1
    )
    assert stale_delete.status_code == 409, stale_delete.text
    assert [row["id"] for row in client.get("/api/v1/companies").json()] == [
        "reuse"
    ]

    # A stale request must be rejected by the master lookup, before even
    # constructing a session/engine for the newly-created company database.
    from app import deps

    opened = False

    def forbidden_company_session(_company_id):
        nonlocal opened
        opened = True
        raise AssertionError("stale request opened the company database")

    with monkeypatch.context() as patcher:
        patcher.setattr(deps, "company_session", forbidden_company_session)
        stale = client.post(
            "/api/v1/contacts",
            headers=headers_1,
            json={"name": "Must Not Land", "kind": "supplier"},
        )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "COMPANY_GENERATION_MISMATCH"
    assert opened is False

    headers_2 = {
        "X-Company-Id": "reuse",
        "X-Company-Generation": generation_2,
    }
    current = client.post(
        "/api/v1/contacts",
        headers=headers_2,
        json={"name": "Second Supplier", "kind": "supplier"},
    )
    assert current.status_code == 201, current.text
    assert [row["name"] for row in client.get(
        "/api/v1/contacts", headers=headers_2
    ).json()] == ["Second Supplier"]


def test_optional_company_header_rejects_generation_without_id():
    from fastapi import HTTPException

    from app.deps import get_current_company_optional

    with pytest.raises(HTTPException) as exc:
        get_current_company_optional(None, "orphan-generation")
    assert exc.value.status_code == 400


def test_generation_is_unique_and_immutable_in_master_db(client):
    from sqlalchemy.exc import IntegrityError

    from app.db.master import MasterSession
    from app.models.master import Company

    first = client.post(
        "/api/v1/companies", json={"id": "immutable-a", "name": "A Pty Ltd"}
    ).json()
    second = client.post(
        "/api/v1/companies", json={"id": "immutable-b", "name": "B Pty Ltd"}
    ).json()
    assert first["generation_id"] != second["generation_id"]

    with MasterSession() as db:
        company = db.get(Company, "immutable-a")
        company.generation_id = second["generation_id"]
        with pytest.raises(IntegrityError, match="generation_id is immutable"):
            db.commit()
        db.rollback()

    with MasterSession() as db:
        db.add(
            Company(
                id="immutable-c",
                name="C Pty Ltd",
                generation_id=second["generation_id"],
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_generation_is_rechecked_after_waiting_for_lifecycle_lock(
    client, monkeypatch
):
    first = client.post(
        "/api/v1/companies", json={"id": "race", "name": "Race A1 Pty Ltd"}
    )
    assert first.status_code == 201, first.text
    headers_1 = {
        "X-Company-Id": "race",
        "X-Company-Generation": first.json()["generation_id"],
    }

    from app import deps

    reached_post_validation_boundary = Event()
    resume_stale_request = Event()
    real_lifecycle_lock = deps.company_lifecycle_lock
    real_company_session = deps.company_session
    opened_company_sessions: list[str] = []

    @contextmanager
    def paused_before_lock(company_id):
        # get_current_company has already accepted A1. Pause before acquiring
        # the lifecycle lock so the main thread can delete A1 and create A2.
        reached_post_validation_boundary.set()
        assert resume_stale_request.wait(timeout=10)
        with real_lifecycle_lock(company_id):
            yield

    def tracked_company_session(company_id):
        opened_company_sessions.append(company_id)
        return real_company_session(company_id)

    monkeypatch.setattr(deps, "company_lifecycle_lock", paused_before_lock)
    monkeypatch.setattr(deps, "company_session", tracked_company_session)

    with ThreadPoolExecutor(max_workers=1) as pool:
        stale_future = pool.submit(
            client.post,
            "/api/v1/contacts",
            headers=headers_1,
            json={"name": "Must Not Reach A2", "kind": "supplier"},
        )
        assert reached_post_validation_boundary.wait(timeout=10)

        deleted = client.delete(
            "/api/v1/companies/race?confirm=race", headers=headers_1
        )
        assert deleted.status_code == 204, deleted.text
        second = client.post(
            "/api/v1/companies", json={"id": "race", "name": "Race A2 Pty Ltd"}
        )
        assert second.status_code == 201, second.text
        assert second.json()["generation_id"] != first.json()["generation_id"]

        resume_stale_request.set()
        stale = stale_future.result(timeout=10)

    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "COMPANY_GENERATION_MISMATCH"
    assert opened_company_sessions == []

    headers_2 = {
        "X-Company-Id": "race",
        "X-Company-Generation": second.json()["generation_id"],
    }
    contacts = client.get("/api/v1/contacts", headers=headers_2)
    assert contacts.status_code == 200, contacts.text
    assert contacts.json() == []


def test_active_company_session_blocks_delete_until_teardown(client, monkeypatch):
    created = client.post(
        "/api/v1/companies", json={"id": "active", "name": "Active Pty Ltd"}
    )
    assert created.status_code == 201, created.text
    generation = created.json()["generation_id"]
    headers = {
        "X-Company-Id": "active",
        "X-Company-Generation": generation,
    }

    from app import deps
    from app.api.v1 import companies as companies_api

    company = deps.get_current_company("active", generation)
    session_dependency = deps.get_company_db(company)
    next(session_dependency)  # acquires and holds the lifecycle lock

    delete_reached_lock = Event()
    real_lifecycle_lock = companies_api.company_lifecycle_lock

    @contextmanager
    def observed_delete_lock(company_id):
        delete_reached_lock.set()
        with real_lifecycle_lock(company_id):
            yield

    monkeypatch.setattr(
        companies_api, "company_lifecycle_lock", observed_delete_lock
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            delete_future = pool.submit(
                client.delete,
                "/api/v1/companies/active?confirm=active",
                headers=headers,
            )
            assert delete_reached_lock.wait(timeout=10)
            with pytest.raises(FutureTimeoutError):
                delete_future.result(timeout=0.2)

            session_dependency.close()
            deleted = delete_future.result(timeout=10)
    finally:
        session_dependency.close()

    assert deleted.status_code == 204, deleted.text
    assert client.get("/api/v1/companies").json() == []
