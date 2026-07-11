"""Fail-closed coverage for the master/company SQLite file registry."""

from __future__ import annotations

import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient


def _load_app(monkeypatch, data_dir):
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]
    from app.main import app

    return app


def _create_company(client, company_id="tc"):
    response = client.post(
        "/api/v1/companies",
        json={"id": company_id, "name": f"{company_id} Pty Ltd"},
    )
    assert response.status_code == 201, response.text
    company = response.json()
    return {
        "X-Company-Id": company_id,
        "X-Company-Generation": company["generation_id"],
    }


def test_fresh_first_run_and_normal_restart_are_idempotent(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path / "fresh")
    from app.config import settings

    assert not settings.master_db_path.exists()
    with TestClient(app) as client:
        headers = _create_company(client)
        assert settings.master_db_path.is_file()
        assert settings.company_db_path("tc").is_file()
        accounts = client.get("/api/v1/accounts", headers=headers)
        assert accounts.status_code == 200
        account_ids = [row["id"] for row in accounts.json()]

    with TestClient(app) as client:
        assert [row["id"] for row in client.get("/api/v1/companies").json()] == [
            "tc"
        ]
        assert [
            row["id"]
            for row in client.get("/api/v1/accounts", headers=headers).json()
        ] == account_ids


def test_registered_company_missing_books_fails_startup_without_recreation(
    monkeypatch, tmp_path
):
    app = _load_app(monkeypatch, tmp_path / "missing-books")
    from app.config import settings
    from app.db.company import dispose_company_engine
    from app.db.errors import DataRecoveryRequiredError

    with TestClient(app) as client:
        _create_company(client, "lost")
    books_path = settings.company_db_path("lost")
    dispose_company_engine("lost")
    books_path.unlink()

    with pytest.raises(
        DataRecoveryRequiredError, match="registered companies missing books.db: lost"
    ):
        with TestClient(app):
            pass
    assert not books_path.exists()


def test_missing_master_with_physical_books_fails_without_modifying_orphan(
    monkeypatch, tmp_path
):
    app = _load_app(monkeypatch, tmp_path / "missing-master")
    from app.config import settings
    from app.db.errors import DataRecoveryRequiredError

    books_path = settings.company_db_path("orphan")
    books_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(books_path) as connection:
        connection.execute("CREATE TABLE recovery_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO recovery_marker VALUES ('preserve-me')")
    original = books_path.read_bytes()

    assert not settings.master_db_path.exists()
    with pytest.raises(
        DataRecoveryRequiredError,
        match="Master company registry is missing while physical company databases exist",
    ):
        with TestClient(app):
            pass
    assert not settings.master_db_path.exists()
    assert books_path.read_bytes() == original


def test_existing_master_with_orphan_books_fails_without_modification(
    monkeypatch, tmp_path
):
    app = _load_app(monkeypatch, tmp_path / "orphan-books")
    from app.config import settings
    from app.db.errors import DataRecoveryRequiredError

    with TestClient(app):
        pass
    books_path = settings.company_db_path("unregistered")
    books_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(books_path) as connection:
        connection.execute("CREATE TABLE recovery_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO recovery_marker VALUES ('preserve-me')")
    original = books_path.read_bytes()

    with pytest.raises(
        DataRecoveryRequiredError,
        match="unregistered orphan books.db files: unregistered",
    ):
        with TestClient(app):
            pass
    assert books_path.read_bytes() == original


def test_cached_company_engine_cannot_recreate_externally_deleted_books(
    monkeypatch, tmp_path
):
    app = _load_app(monkeypatch, tmp_path / "cached-books")
    from app.config import settings
    from app.db.company import get_company_engine

    with TestClient(app) as client:
        headers = _create_company(client, "cached")
        engine = get_company_engine("cached")
        books_path = settings.company_db_path("cached")

        # Keep the Engine object in the registry but close its pooled handles so
        # Windows permits an external-delete simulation.
        engine.dispose()
        books_path.unlink()

        response = client.get("/api/v1/accounts", headers=headers)
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "DATA_RECOVERY_REQUIRED"
        assert "refused to create an empty replacement" in response.json()[
            "detail"
        ]["message"]
        assert not books_path.exists()


def test_cached_master_engine_cannot_recreate_externally_deleted_registry(
    monkeypatch, tmp_path
):
    app = _load_app(monkeypatch, tmp_path / "cached-master")
    from app.config import settings
    from app.db.master import master_engine

    with TestClient(app) as client:
        assert client.get("/api/v1/companies").status_code == 200
        master_engine.dispose()
        settings.master_db_path.unlink()

        response = client.get("/api/v1/companies")
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "DATA_RECOVERY_REQUIRED"
        assert not settings.master_db_path.exists()


def test_live_provisioning_refuses_preexisting_orphan_directory(
    monkeypatch, tmp_path
):
    app = _load_app(monkeypatch, tmp_path / "live-orphan")
    from app.config import settings

    with TestClient(app) as client:
        books_path = settings.company_db_path("rogue")
        books_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(books_path) as connection:
            connection.execute("CREATE TABLE recovery_marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO recovery_marker VALUES ('preserve-me')")
        original = books_path.read_bytes()

        response = client.post(
            "/api/v1/companies", json={"id": "rogue", "name": "Rogue Pty"}
        )
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "DATA_RECOVERY_REQUIRED"
        assert books_path.read_bytes() == original
        assert client.get("/api/v1/companies").json() == []
