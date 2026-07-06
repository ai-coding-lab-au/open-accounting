#!/usr/bin/env python3
"""Self-contained end-to-end smoke test.

Boots the backend on a throwaway port + temp DATA_DIR, then exercises the
whole system over real HTTP:

  company create (CoA + bank account seeded) -> staff (LPN) -> client
  -> receipt -> receipt PDF
  -> manual bank transaction -> dashboard -> P&L (JSON + PDF) -> trial balance
  -> supplier contact -> AP invoice -> post to GL -> journal entry visible

Run from backend/:  python3 scripts/smoke_e2e.py
Exit code 0 = system works end to end.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PORT = int(os.environ.get("SMOKE_PORT", "8199"))
BASE = f"http://127.0.0.1:{PORT}/api/v1"
BACKEND_DIR = Path(__file__).resolve().parents[1]


def ok(label: str, r: httpx.Response, code: int = 200):
    assert r.status_code == code, f"{label}: HTTP {r.status_code} {r.text[:300]}"
    print(f"  ok {label}")
    ctype = r.headers.get("content-type", "")
    return r.json() if ctype.startswith("application/json") else r


def wait_healthy(c: httpx.Client, deadline: float = 30.0) -> None:
    start = time.time()
    while time.time() - start < deadline:
        try:
            if c.get(f"http://127.0.0.1:{PORT}/health").status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.3)
    raise RuntimeError("backend did not become healthy in time")


def run_flow(c: httpx.Client) -> None:
    H = {"X-Company-Id": "smoke"}

    ok("create company", c.post(f"{BASE}/companies", json={
        "id": "smoke", "name": "Smoke Test Pty Ltd", "gst_registered": True}), 201)
    accounts = ok("CoA seeded", c.get(f"{BASE}/accounts", headers=H))
    assert any(a["code"] == "4000" for a in accounts)
    banks = ok("bank account seeded", c.get(f"{BASE}/bank-accounts", headers=H))
    assert len(banks) == 1

    staff = ok("create staff (LPN)", c.post(f"{BASE}/staff", headers=H, json={
        "full_name": "Jane Doe", "registration_type": "lpn",
        "registration_number": "12345"}), 201)
    client_row = ok("create client", c.post(f"{BASE}/clients", headers=H, json={
        "display_name": "John Citizen", "email": "john@example.com"}), 201)

    rec = ok("create receipt", c.post(f"{BASE}/outgoing", headers=H, json={
        "doc_type": "receipt",
        "client_ref_id": client_row["id"],
        "issue_date": "2026-07-01",
        "lines": [{"description": "Subclass 482 Visa Application", "quantity": "1",
                   "unit_price": "3000.00", "amount": "3000.00"}],
    }), 201)
    pdf = c.post(f"{BASE}/outgoing/{rec['id']}/pdf", headers=H)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF", pdf.status_code
    print("  ok receipt PDF renders")

    ok("manual bank txn", c.post(f"{BASE}/bank-accounts/{banks[0]['id']}/transactions",
        headers=H, json={
            "direction": "in", "amount": "3000.00", "occurred_at": "2026-07-01",
            "memo": "Receipt payment",
            "account_id": next(a["id"] for a in accounts if a["code"] == "4000")}), 201)
    ok("dashboard", c.get(f"{BASE}/dashboard/summary", headers=H))
    ok("P&L json", c.get(f"{BASE}/reports/profit-loss", headers=H,
                         params={"period_start": "2026-07-01", "period_end": "2027-06-30"}))
    r = c.get(f"{BASE}/reports/profit-loss/pdf", headers=H,
              params={"period_start": "2026-07-01", "period_end": "2027-06-30"})
    assert r.status_code == 200 and r.content[:4] == b"%PDF", r.status_code
    print("  ok P&L PDF renders")
    ok("trial balance", c.get(f"{BASE}/reports/trial-balance", headers=H))

    contact = ok("create supplier", c.post(f"{BASE}/contacts", headers=H, json={
        "kind": "supplier", "name": "Office Supplies Co"}), 201)
    inv = ok("create AP invoice", c.post(f"{BASE}/invoices", headers=H, json={
        "direction": "AP", "contact_id": contact["id"], "invoice_number": "INV-1",
        "issue_date": "2026-07-01", "gst_inclusive": True,
        "subtotal": "100.00", "gst_amount": "10.00", "total": "110.00",
        "lines": [{"description": "Stationery", "quantity": "1", "unit_price": "110.00",
                   "line_subtotal": "100.00", "line_gst": "10.00", "line_total": "110.00",
                   "account_id": next(a["id"] for a in accounts if a["type"] == "EXPENSE")}],
    }), 201)
    ok("post invoice to GL", c.post(f"{BASE}/invoices/{inv['id']}/post", headers=H))
    jes = ok("journal entries", c.get(f"{BASE}/journal", headers=H))
    assert len(jes) >= 1

    print("SMOKE PASSED")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="oa-smoke-") as tmp:
        env = {**os.environ, "DATA_DIR": tmp}
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
            cwd=BACKEND_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(timeout=60) as c:
                wait_healthy(c)
                run_flow(c)
            return 0
        except Exception as e:  # noqa: BLE001 — report and fail the smoke
            print(f"SMOKE FAILED: {e}", file=sys.stderr)
            if proc.poll() is not None and proc.stdout:
                print(proc.stdout.read().decode(errors="ignore")[-2000:], file=sys.stderr)
            return 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
