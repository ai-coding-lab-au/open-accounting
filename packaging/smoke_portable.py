"""Post-build smoke test for the Windows portable bundle.

Launches the built exe headless against a throwaway data dir, then exercises
the paths a fresh user hits — including PDF rendering, which only exists at
runtime (ReportLab lives in the PyInstaller archive, so a build that silently
dropped it would pass every static check and fail only here).

    python packaging/smoke_portable.py dist\\OpenAccounting\\OpenAccounting.exe
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = 18123
BASE = f"http://127.0.0.1:{PORT}"
COMPANY = "smoketest"


def _request(method: str, path: str, body: dict | None = None,
             company: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(BASE + path, method=method)
    if company:
        req.add_header("X-Company-Id", company)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        raise SystemExit(f"PORTABLE SMOKE FAILED: {label} {detail}")


def main() -> None:
    exe = sys.argv[1]
    data_dir = tempfile.mkdtemp(prefix="oa-portable-smoke-")
    proc = subprocess.Popen(
        [exe, "--no-browser", "--port", str(PORT), "--data-dir", data_dir],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 40
        status = None
        while time.monotonic() < deadline:
            try:
                status, _ = _request("GET", "/health")
                if status == 200:
                    break
            except OSError:
                time.sleep(0.5)
        _check("backend healthy", status == 200, f"last status={status}")

        status, body = _request("GET", "/")
        _check("frontend index served", status == 200 and b"<title>" in body)

        status, _ = _request("GET", "/api/nope")
        _check("API 404 not swallowed by SPA fallback", status == 404)

        status, _ = _request("POST", "/api/v1/companies",
                             {"id": COMPANY, "name": "Smoke Test Pty Ltd"})
        _check("create company", status in (200, 201), f"status={status}")

        status, body = _request("POST", "/api/v1/clients",
                                {"display_name": "Smoke Client"}, company=COMPANY)
        _check("create client", status in (200, 201), f"status={status}")
        client_id = json.loads(body)["id"]

        status, body = _request(
            "POST", "/api/v1/outgoing",
            {
                "issue_date": "2026-01-15",
                "client_ref_id": client_id,
                "lines": [{"description": "Smoke service", "quantity": "1",
                           "unit_price": "100.00"}],
            },
            company=COMPANY,
        )
        _check("create receipt", status in (200, 201), f"status={status} body={body[:200]!r}")
        doc_id = json.loads(body)["id"]

        status, body = _request("POST", f"/api/v1/outgoing/{doc_id}/pdf",
                                company=COMPANY)
        _check("receipt PDF renders", status == 200 and body.startswith(b"%PDF-")
               and len(body) > 1000, f"status={status} size={len(body)}")

        status, body = _request(
            "GET",
            "/api/v1/reports/profit-loss/pdf?period_start=2026-01-01&period_end=2026-01-31",
            company=COMPANY,
        )
        _check("report PDF renders", status == 200 and body.startswith(b"%PDF-"),
               f"status={status}")

        print("PORTABLE SMOKE PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
