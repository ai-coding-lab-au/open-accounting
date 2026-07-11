from __future__ import annotations

import sys
import shutil
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def api_client(monkeypatch, request):
    test_root = (PROJECT_ROOT / "tmp" / "tests").resolve()
    data_dir = (test_root / f"upload_caps_{request.node.name}").resolve()
    assert data_dir.parent == test_root
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("ALLOW_UNSAFE_DATA_DIR", "1")
    for module_name in list(sys.modules):
        if module_name.startswith("app"):
            del sys.modules[module_name]

    from app.main import app

    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/companies",
                json={
                    "id": "tc",
                    "marn": "1234567",
                    "registered_agent_name": "Test Agent",
                    "name": "Test Pty Ltd",
                },
            )
            assert response.status_code == 201, response.text
            headers = {
                "X-Company-Id": "tc",
                "X-Company-Generation": response.json()["generation_id"],
            }
            yield client, headers, data_dir
    finally:
        from app.db.company import dispose_company_engine
        from app.db.master import master_engine

        dispose_company_engine("tc")
        master_engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)


def _observe_upload_reads(monkeypatch):
    from starlette.datastructures import UploadFile

    calls: dict[str, list[tuple[int, int]]] = defaultdict(list)
    original_read = UploadFile.read

    async def observed_read(self, size: int = -1) -> bytes:
        chunk = await original_read(self, size)
        calls[str(self.filename)].append((size, len(chunk)))
        return chunk

    monkeypatch.setattr(UploadFile, "read", observed_read)
    return calls


def _use_tiny_cap(monkeypatch):
    from app.api.v1 import invoices as invoices_api

    assert invoices_api._MAX_UPLOAD_BYTES == 25 * 1024 * 1024
    assert invoices_api._UPLOAD_CHUNK_BYTES == 1024 * 1024
    monkeypatch.setattr(invoices_api, "_MAX_UPLOAD_BYTES", 5)
    monkeypatch.setattr(invoices_api, "_UPLOAD_CHUNK_BYTES", 2)
    return invoices_api


def test_pdf_and_excel_uploads_allow_exact_size_boundary(
    api_client, monkeypatch
):
    client, headers, _ = api_client
    _use_tiny_cap(monkeypatch)
    calls = _observe_upload_reads(monkeypatch)

    pdf_response = client.post(
        "/api/v1/invoices/upload-pdf",
        headers=headers,
        files={"file": ("boundary.pdf", b"%PDF-", "application/pdf")},
    )
    assert pdf_response.status_code == 200, pdf_response.text
    assert pdf_response.json()["size_bytes"] == 5

    csv_response = client.post(
        "/api/v1/invoices/upload-excel",
        headers=headers,
        files={"file": ("boundary.csv", b"a\n1\n2", "text/csv")},
    )
    assert csv_response.status_code == 200, csv_response.text
    assert csv_response.json()["headers"] == ["a"]

    expected_reads = [(2, 2), (2, 2), (2, 1), (2, 0)]
    assert calls["boundary.pdf"] == expected_reads
    assert calls["boundary.csv"] == expected_reads


def test_pdf_and_excel_uploads_keep_empty_file_errors(api_client, monkeypatch):
    client, headers, _ = api_client
    _use_tiny_cap(monkeypatch)
    calls = _observe_upload_reads(monkeypatch)

    for endpoint, filename, content_type in (
        ("upload-pdf", "empty.pdf", "application/pdf"),
        ("upload-excel", "empty.csv", "text/csv"),
    ):
        response = client.post(
            f"/api/v1/invoices/{endpoint}",
            headers=headers,
            files={"file": (filename, b"", content_type)},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Empty file"
        assert calls[filename] == [(2, 0)]


def test_pdf_and_excel_uploads_stop_reading_immediately_after_cap(
    api_client, monkeypatch
):
    client, headers, data_dir = api_client
    invoices_api = _use_tiny_cap(monkeypatch)
    calls = _observe_upload_reads(monkeypatch)

    def must_not_run(*args, **kwargs):
        raise AssertionError("oversized upload reached persistence or parsing")

    monkeypatch.setattr(invoices_api.attach_svc, "save_bytes", must_not_run)
    monkeypatch.setattr(invoices_api.excel_import, "parse_spreadsheet", must_not_run)

    for endpoint, filename, content_type in (
        ("upload-pdf", "oversized.pdf", "application/pdf"),
        ("upload-excel", "oversized.csv", "text/csv"),
    ):
        response = client.post(
            f"/api/v1/invoices/{endpoint}",
            headers=headers,
            files={"file": (filename, b"123456789", content_type)},
        )
        assert response.status_code == 413, response.text
        # Only six of the nine bytes are consumed: the helper rejects on the
        # first chunk that crosses the five-byte cap and never reads to EOF.
        assert calls[filename] == [(2, 2), (2, 2), (2, 2)]

    from app.db.company import company_session
    from app.models.company import Attachment

    with company_session("tc") as db:
        assert db.query(Attachment).count() == 0
    attachment_dir = data_dir / "companies" / "tc" / "attachments"
    assert not attachment_dir.exists()


def test_bank_import_upload_stops_reading_immediately_after_cap(
    api_client, monkeypatch
):
    client, headers, _ = api_client
    from app.api.v1 import bank_accounts as bank_api

    assert bank_api._MAX_IMPORT_UPLOAD_BYTES == 25 * 1024 * 1024
    assert bank_api._IMPORT_UPLOAD_CHUNK_BYTES == 1024 * 1024
    monkeypatch.setattr(bank_api, "_MAX_IMPORT_UPLOAD_BYTES", 5)
    monkeypatch.setattr(bank_api, "_IMPORT_UPLOAD_CHUNK_BYTES", 2)
    calls = _observe_upload_reads(monkeypatch)

    def must_not_run(*args, **kwargs):
        raise AssertionError("oversized bank statement reached parsing")

    monkeypatch.setattr(bank_api.bank_import_svc, "preview_import", must_not_run)
    bank = client.get("/api/v1/bank-accounts", headers=headers).json()[0]
    response = client.post(
        f"/api/v1/bank-accounts/{bank['id']}/import/preview",
        headers=headers,
        files={"file": ("oversized-bank.csv", b"123456789", "text/csv")},
    )
    assert response.status_code == 413, response.text
    assert calls["oversized-bank.csv"] == [(2, 2), (2, 2), (2, 2)]
