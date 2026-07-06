"""Seed a two-year realistic accounting shakeout company.

Run:
    python -m backend.scripts.seed_realistic --base-url http://127.0.0.1:8000 --reset

The operational seed path intentionally talks to the public HTTP API. The only
direct SQLite access here is for reset/idempotency and the final row-count
summary, because the app does not expose company delete or table-count
endpoints.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import httpx
from openpyxl import Workbook


COMPANY_ID = "m5-shakeout"
COMPANY_NAME = "M5 Shakeout Co Pty Ltd"
RANDOM_SEED = 20260524
FY_START = date(2023, 7, 1)
FY_END = date(2025, 6, 30)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def gst_from_gross(gross: Decimal, tax_code: str) -> Decimal:
    if tax_code in {"standard", "capital"}:
        return money(gross / Decimal("11"))
    return Decimal("0.00")


def iso(d: date) -> str:
    return d.isoformat()


def month_starts() -> list[date]:
    out: list[date] = []
    y, m = 2023, 7
    while (y, m) <= (2025, 6):
        out.append(date(y, m, 1))
        m += 1
        if m == 13:
            y += 1
            m = 1
    return out


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def data_dir() -> Path:
    raw = os.environ.get("DATA_DIR")
    if not raw:
        return PROJECT_ROOT / "data"
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def reset_company_storage(company_id: str) -> None:
    root = data_dir()
    master = root / "master.db"
    if master.exists():
        with sqlite3.connect(master) as conn:
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
            conn.commit()
    shutil.rmtree(root / "companies" / company_id, ignore_errors=True)


def table_counts(company_id: str) -> dict[str, int]:
    db_path = data_dir() / "companies" / company_id / "books.db"
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for (name,) in rows}


@dataclass
class Api:
    client: Any
    base_url: str = ""
    company_id: str | None = None

    @classmethod
    def from_url(cls, base_url: str) -> "Api":
        return cls(httpx.Client(base_url=base_url.rstrip("/"), timeout=60), "")

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Company-Id": self.company_id} if self.company_id else {}

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            close()

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._check(self.client.get(path, headers=self.headers, **kwargs))

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._check(self.client.post(path, headers=self.headers, **kwargs))

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self._check(self.client.patch(path, headers=self.headers, **kwargs))

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._check(self.client.put(path, headers=self.headers, **kwargs))

    def _check(self, response: Any) -> Any:
        if response.status_code >= 400:
            raise RuntimeError(f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text}")
        return response


def create_company(api: Api) -> None:
    companies = api.get("/api/v1/companies").json()
    if any(c["id"] == COMPANY_ID for c in companies):
        raise RuntimeError(f"Company {COMPANY_ID!r} already exists. Re-run with --reset to recreate it.")
    payload = {
        "id": COMPANY_ID,
        "name": COMPANY_NAME,
        "legal_name": COMPANY_NAME,
        "abn": "53 004 085 616",
        "base_currency": "AUD",
        "fy_start_month": 7,
        "gst_registered": True,
        # A company must carry a practitioner credential at creation.
        "registered_agent_name": "Mina Patel",
        "marn": "2312345",
    }
    api.post("/api/v1/companies", json=payload)
    api.company_id = COMPANY_ID
    api.patch(
        f"/api/v1/companies/{COMPANY_ID}",
        json={
            "address_line1": "Level 12, 55 Collins Street",
            "suburb": "Melbourne",
            "state": "VIC",
            "postcode": "3000",
            "phone": "+61 3 9000 0123",
            "email": "accounts@m5shakeout.example",
            "website": "https://m5shakeout.example",
            "bank_account_name": COMPANY_NAME,
            "bank_name": "National Australia Bank",
            "bank_bsb": "083-001",
            "bank_account_number": "123456789",
            "registered_agent_name": "Mina Patel",
            "marn": "2312345",
        },
    )


def load_accounts(api: Api) -> dict[str, dict[str, Any]]:
    return {a["code"]: a for a in api.get("/api/v1/accounts").json()}


def load_bank(api: Api) -> dict[str, Any]:
    return api.get("/api/v1/bank-accounts").json()[0]


def create_bank_rules(api: Api, accounts: dict[str, dict[str, Any]]) -> None:
    rules = [
        (5, "Customer receipts", "in", None, "(?i)(invoice|client payment|receipt)", "4000", "standard"),
        (10, "Office rent", "out", "(?i)rent", None, "6100", "standard"),
        (15, "Weekly payroll", "out", "(?i)payroll", None, "6000", "none"),
        (20, "Superannuation", "out", "(?i)superannuation", None, "6010", "none"),
        (25, "Contractors", "out", "(?i)contractor", None, "5100", "standard"),
        (30, "Software subscriptions", "out", "(?i)(xero|adobe|microsoft|aws)", None, "6410", "standard"),
        (35, "Fuel and vehicle", "out", "(?i)(fuel|bp|caltex)", None, "6300", "standard"),
        (40, "Bank fees", "out", "(?i)bank fee", None, "6500", "input_taxed"),
        (45, "Insurance", "out", "(?i)insurance", None, "6700", "standard"),
        (50, "BAS payment", "out", "(?i)bas payment", None, "2100", "none"),
        (55, "Capital equipment", "out", "(?i)(laptop|equipment)", None, "1700", "capital"),
        (60, "Unicode cafe supplier", "out", None, "Café Niño Supplies", "6400", "gst_free"),
        (65, "Interest income", "in", "(?i)interest", None, "4100", "input_taxed"),
        (70, "Same priority first", "out", "(?i)priority tiebreak", None, "6900", "none"),
        (70, "Same priority second", "out", "(?i)priority tiebreak", None, "6400", "standard"),
    ]
    for priority, description, direction, memo_re, counter_re, code, tax_code in rules:
        payload = {
            "priority": priority,
            "description": description,
            "match_direction": direction,
            "match_memo_regex": memo_re,
            "match_counter_party_regex": counter_re,
            "set_account_id": accounts[code]["id"],
            "set_tax_code": tax_code,
        }
        api.post("/api/v1/bank-rules", json=payload)


def post_journal(api: Api, entry_date: date, memo_text: str, lines: list[tuple[str, str, Decimal]], accounts: dict[str, dict[str, Any]]) -> None:
    payload = {
        "entry_date": iso(entry_date),
        "memo": memo_text,
        "reference": f"M5-{entry_date:%Y%m%d}",
        "lines": [
            {
                "account_id": accounts[code]["id"],
                "debit_amount": str(amount if side == "dr" else Decimal("0.00")),
                "credit_amount": str(amount if side == "cr" else Decimal("0.00")),
            }
            for code, side, amount in lines
        ],
    }
    api.post("/api/v1/journal", json=payload)


def create_clients(api: Api) -> dict[str, Any]:
    client_payloads = [
        {"display_name": "Aisha Rahman", "email": "aisha@example.com", "client_ref": "C-1001"},
        {"display_name": "Nguyen Family", "email": "nguyen@example.com", "client_ref": "C-1002"},
        {"display_name": "Carlos Silva", "email": "carlos@example.com", "client_ref": "C-1003"},
    ]
    clients = [api.post("/api/v1/clients", json=p).json() for p in client_payloads]
    staff = api.post(
        "/api/v1/staff",
        json={
            "full_name": "Avery Migration Agent",
            "registration_type": "mara",
            "registration_number": "1234567",
        },
    ).json()
    receipt = api.post(
        "/api/v1/outgoing",
        json={
            "doc_type": "receipt",
            "issue_date": "2024-02-06",
            "client_ref_id": clients[0]["id"],
            "lines": [
                {
                    "description": "Skilled Independent Visa Application",
                    "quantity": "1",
                    "unit_price": "3300.00",
                    "amount": "3300.00",
                }
            ],
        },
    ).json()
    return {"clients": clients, "staff": staff, "receipt": receipt}


CUSTOMERS = [
    "ACME Advisory",
    "Southern Cross Imports",
    "Koala Mining Pty Ltd",
    "Blue Harbour Clinics",
    "Orbit Legal",
    "Tasman Education Group",
]

SUPPLIERS = [
    "Melbourne Office Leasing",
    "Xero Australia",
    "Adobe Systems",
    "Microsoft Australia",
    "BP Fuel",
    "Caltex",
    "Café Niño Supplies",
    "Vero Insurance",
    "NAB Bank Fee",
    "Atlas Contractor Group",
]


def invoice_payload(direction: str, number: str, name: str, issue: date, total: Decimal, tax_code: str = "standard") -> dict[str, Any]:
    gst = gst_from_gross(total, tax_code)
    subtotal = money(total - gst)
    return {
        "direction": direction,
        "contact_name": name,
        "invoice_number": number,
        "issue_date": iso(issue),
        "due_date": iso(issue + timedelta(days=14)),
        "subtotal": str(subtotal),
        "gst_amount": str(gst),
        "total": str(total),
        "gst_inclusive": True,
        "notes": "Generated by M5 realistic seed.",
        "source": "manual",
        "lines": [
            {
                "description": "Professional services" if direction == "AR" else "Supplier services",
                "account_id": None,
                "quantity": "1",
                "unit_price": str(subtotal),
                "gst_rate": "0.10" if tax_code == "standard" else "0",
                "line_subtotal": str(subtotal),
                "line_gst": str(gst),
                "line_total": str(total),
            }
        ],
    }


def create_invoices_and_docs(
    api: Api,
    rng: random.Random,
    clients: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payments: list[dict[str, Any]] = []
    seq = 1
    ap_seq = 1
    outgoing_seq = 1
    for month in month_starts():
        invoice_count = rng.randint(10, 20)
        for _ in range(invoice_count):
            issue = month + timedelta(days=rng.randint(0, 24))
            total = money(rng.randint(8, 60) * 110)
            customer = rng.choice(CUSTOMERS)
            if issue == date(2024, 9, 20) or (customer == "Koala Mining Pty Ltd" and seq % 17 == 0):
                pay_date = issue + timedelta(days=45)
            else:
                pay_date = issue + timedelta(days=rng.randint(3, 24))
            if pay_date > FY_END:
                pay_date = FY_END
            inv = api.post(
                "/api/v1/invoices",
                json=invoice_payload("AR", f"AR-{seq:05d}", customer, issue, total),
            ).json()
            # Invoices stay draft: paid status now requires posting first
            # (lifecycle guard), and these seed lines have no account_id.
            # The cash movement is seeded through the bank rows below.
            payments.append(
                {
                    "occurred_at": pay_date,
                    "direction": "in",
                    "amount": total,
                    "memo": f"Client payment invoice {inv['invoice_number']}",
                    "counter_party_name": customer,
                    "account_code": "4000",
                    "tax_code": "standard",
                }
            )
            seq += 1

        for _ in range(rng.randint(5, 10)):
            issue = month + timedelta(days=rng.randint(1, 25))
            total = money(rng.randint(3, 24) * 110)
            supplier = rng.choice(SUPPLIERS)
            api.post(
                "/api/v1/invoices",
                json=invoice_payload("AP", f"AP-{ap_seq:05d}", supplier, issue, total),
            )
            ap_seq += 1

        for _ in range(rng.randint(5, 15)):
            issue = month + timedelta(days=rng.randint(0, 26))
            total = money(rng.randint(4, 30) * 100)
            # Receipt is the only outgoing document type; created directly.
            client = rng.choice(clients)
            api.post(
                "/api/v1/outgoing",
                json={
                    "doc_type": "receipt",
                    "issue_date": iso(issue),
                    "client_ref_id": client["id"],
                    "customer_name": client["display_name"],
                    "customer_email": "accounts@example.com",
                    "lines": [{"description": "Migration advisory services", "quantity": "1", "unit_price": str(total)}],
                    "notes": "M5 outgoing document volume.",
                    "paid_date": iso(issue),
                    "payment_method": "Bank transfer",
                },
            )
            outgoing_seq += 1
    return payments


def monthly_bank_rows(month: date, rng: random.Random, invoice_payments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [p for p in invoice_payments if p["occurred_at"].year == month.year and p["occurred_at"].month == month.month]

    def add(day: int, direction: str, amount: Decimal, memo: str, party: str, account: str, tax: str) -> None:
        rows.append(
            {
                "occurred_at": month + timedelta(days=min(day, 27) - 1),
                "direction": direction,
                "amount": money(amount),
                "memo": memo,
                "counter_party_name": party,
                "account_code": account,
                "tax_code": tax,
            }
        )

    add(1, "out", Decimal("3850.00"), f"Office rent {month:%b %Y}", "Melbourne Office Leasing", "6100", "standard")
    add(7, "out", Decimal("2450.00"), f"Payroll week 1 {month:%b %Y}", "Payroll Clearing", "6000", "none")
    add(14, "out", Decimal("2450.00"), f"Payroll week 2 {month:%b %Y}", "Payroll Clearing", "6000", "none")
    add(21, "out", Decimal("2450.00"), f"Payroll week 3 {month:%b %Y}", "Payroll Clearing", "6000", "none")
    add(27, "out", Decimal("2450.00"), f"Payroll week 4 {month:%b %Y}", "Payroll Clearing", "6000", "none")
    add(16, "out", Decimal("980.00"), f"Superannuation {month:%b %Y}", "Aware Super", "6010", "none")
    add(3, "out", Decimal("99.00"), f"Xero subscription {month:%b %Y}", "Xero Australia", "6410", "standard")
    add(8, "out", Decimal("77.00"), f"Microsoft 365 {month:%b %Y}", "Microsoft Australia", "6410", "standard")
    add(11, "out", Decimal("45.00"), f"Monthly bank fee {month:%b %Y}", "NAB Bank Fee", "6500", "input_taxed")
    add(25, "in", Decimal("18.00"), f"Interest {month:%b %Y}", "NAB Interest", "4100", "input_taxed")
    if month.month in {7, 10, 1, 4}:
        add(18, "out", Decimal("3300.00"), f"Insurance annual/quarterly {month:%b %Y}", "Vero Insurance", "6700", "standard")
    if month.month in {10, 1, 4, 6}:
        add(22, "out", Decimal("1850.00"), f"BAS payment {month:%b %Y}", "Australian Taxation Office", "2100", "none")
    if month in {date(2023, 8, 1), date(2024, 7, 1), date(2025, 2, 1)}:
        add(9, "out", Decimal("4180.00"), f"Laptop equipment {month:%b %Y}", "JB Hi-Fi Business", "1700", "capital")
    if month == date(2024, 5, 1):
        add(12, "out", Decimal("88.00"), "Priority tiebreak deterministic probe", "Rule Probe Pty Ltd", "6900", "none")

    random_expenses = rng.randint(10, 26)
    expense_options = [
        ("Fuel purchase", "BP Fuel", "6300", "standard", 55, 220),
        ("Caltex client travel", "Caltex", "6300", "standard", 60, 260),
        ("Contractor support", "Atlas Contractor Group", "5100", "standard", 660, 2750),
        ("Café supplies", "Café Niño Supplies", "6400", "gst_free", 25, 180),
        ("Client travel", "Qantas", "6310", "standard", 180, 900),
        ("Office stationery", "Officeworks", "6400", "standard", 35, 320),
        ("Merchant fee", "Stripe", "6510", "input_taxed", 20, 140),
        ("Legal review", "Orbit Legal", "6600", "standard", 550, 1650),
        ("Owner transfer", "Owner", "3100", "none", 300, 1200),
    ]
    for i in range(random_expenses):
        label, party, account, tax, lo, hi = rng.choice(expense_options)
        add(
            rng.randint(2, 27),
            "out",
            Decimal(rng.randint(lo, hi)),
            f"{label} {month:%Y%m}-{i:02d}",
            party,
            account,
            tax,
        )
    rows.sort(key=lambda r: (r["occurred_at"], r["memo"]))
    return rows


def csv_bytes(rows: list[dict[str, Any]], *, signed: bool = False) -> bytes:
    buf = io.StringIO()
    if signed:
        writer = csv.writer(buf)
        writer.writerow(["Date", "Narrative", "Counterparty", "Amount"])
        for r in rows:
            amt = r["amount"] if r["direction"] == "in" else -r["amount"]
            writer.writerow([iso(r["occurred_at"]), r["memo"], r["counter_party_name"], str(money(amt))])
    else:
        writer = csv.writer(buf)
        writer.writerow(["Date", "Description", "Payee", "Debit", "Credit"])
        for r in rows:
            debit = str(r["amount"]) if r["direction"] == "out" else ""
            credit = str(r["amount"]) if r["direction"] == "in" else ""
            writer.writerow([iso(r["occurred_at"]), r["memo"], r["counter_party_name"], debit, credit])
    return buf.getvalue().encode("utf-8")


def xlsx_bytes(rows: list[dict[str, Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Value Date", "Particulars", "Counter Party", "Transaction Amount"])
    for r in rows:
        amt = r["amount"] if r["direction"] == "in" else -r["amount"]
        ws.append([iso(r["occurred_at"]), r["memo"], r["counter_party_name"], float(amt)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def preview_and_commit(api: Api, bank_id: int, content: bytes, filename: str, account_by_code: dict[str, dict[str, Any]], desired: list[dict[str, Any]]) -> dict[str, int]:
    preview = api.post(
        f"/api/v1/bank-accounts/{bank_id}/import/preview",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    ).json()
    commit_rows = []
    desired_by_key = {
        (iso(r["occurred_at"]), r["direction"], str(r["amount"]), r["memo"]): r
        for r in desired
    }
    for row in preview["rows"]:
        if not row["ok"]:
            raise RuntimeError(f"Bank import row failed: {row}")
        parsed = row["parsed"]
        key = (parsed["occurred_at"], parsed["direction"], parsed["amount"], parsed["memo"])
        source = desired_by_key[key]
        tax_code = source["tax_code"]
        commit_rows.append(
            {
                "occurred_at": parsed["occurred_at"],
                "direction": parsed["direction"],
                "amount": parsed["amount"],
                "dedup_key": row["dedup_key"],
                "memo": parsed["memo"],
                "counter_party_name": parsed["counter_party_name"],
                "account_id": account_by_code[source["account_code"]]["id"],
                "tax_code": tax_code,
                "gst_amount": str(gst_from_gross(Decimal(parsed["amount"]), tax_code)),
            }
        )
    return api.post(f"/api/v1/bank-accounts/{bank_id}/import/commit", json={"rows": commit_rows}).json()


def seed_bank(api: Api, accounts: dict[str, dict[str, Any]], payments: list[dict[str, Any]], rng: random.Random) -> dict[str, Any]:
    bank = load_bank(api)
    duplicate_result: dict[str, int] | None = None
    imports: list[dict[str, Any]] = []
    for idx, month in enumerate(month_starts()):
        rows = monthly_bank_rows(month, rng, payments)
        content = csv_bytes(rows, signed=(idx % 2 == 1))
        filename = f"m5-bank-{month:%Y-%m}-{'signed' if idx % 2 else 'debit-credit'}.csv"
        result = preview_and_commit(api, bank["id"], content, filename, accounts, rows)
        imports.append({"month": f"{month:%Y-%m}", **result})
        if idx == 0:
            duplicate_result = preview_and_commit(api, bank["id"], content, filename, accounts, rows)

    xlsx_rows = [
        {
            "occurred_at": date(2024, 3, 7),
            "direction": "out",
            "amount": Decimal("132.00"),
            "memo": "Adobe unusual XLSX header",
            "counter_party_name": "Adobe Systems",
            "account_code": "6410",
            "tax_code": "standard",
        },
        {
            "occurred_at": date(2024, 3, 8),
            "direction": "out",
            "amount": Decimal("64.00"),
            "memo": "Café supplies unusual XLSX header",
            "counter_party_name": "Café Niño Supplies",
            "account_code": "6400",
            "tax_code": "gst_free",
        },
        {
            "occurred_at": date(2024, 3, 9),
            "direction": "in",
            "amount": Decimal("22.00"),
            "memo": "Interest unusual XLSX header",
            "counter_party_name": "NAB Interest",
            "account_code": "4100",
            "tax_code": "input_taxed",
        },
    ]
    imports.append({"month": "2024-03-xlsx", **preview_and_commit(api, bank["id"], xlsx_bytes(xlsx_rows), "unusual-bank-export.xlsx", accounts, xlsx_rows)})
    return {"bank_account_id": bank["id"], "imports": imports, "duplicate_result": duplicate_result}


def seed_journals(api: Api, accounts: dict[str, dict[str, Any]]) -> None:
    post_journal(
        api,
        FY_START,
        "Opening balances",
        [("1000", "dr", Decimal("25000.00")), ("1700", "dr", Decimal("12000.00")), ("3000", "cr", Decimal("37000.00"))],
        accounts,
    )
    for month in month_starts():
        post_journal(
            api,
            add_months(month, 1) - timedelta(days=1),
            f"Monthly depreciation {month:%Y-%m}",
            [("6800", "dr", Decimal("350.00")), ("1710", "cr", Decimal("350.00"))],
            accounts,
        )
        if month.month in {9, 12, 3, 6}:
            post_journal(
                api,
                add_months(month, 1) - timedelta(days=1),
                f"Quarter-end accounting accrual {month:%Y-%m}",
                [("6600", "dr", Decimal("900.00")), ("2000", "cr", Decimal("900.00"))],
                accounts,
            )
        if month.month in {8, 2}:
            post_journal(
                api,
                month + timedelta(days=20),
                f"Prepayment amortisation {month:%Y-%m}",
                [("6700", "dr", Decimal("275.00")), ("1500", "cr", Decimal("275.00"))],
                accounts,
            )


def exercise_reports_and_routes(api: Api) -> dict[str, Any]:
    checks: dict[str, Any] = {"reports": {}, "routes": {}}
    for as_of in ["2024-06-30", "2025-06-30"]:
        checks["reports"][f"trial_balance_{as_of}"] = api.get("/api/v1/reports/trial-balance", params={"as_of": as_of}).json()
        checks["reports"][f"balance_sheet_{as_of}"] = api.get("/api/v1/reports/balance-sheet", params={"as_of": as_of}).json()
    for fy in [2024, 2025]:
        for q in [1, 2, 3, 4]:
            checks["reports"][f"bas_FY{fy}_Q{q}"] = api.get("/api/v1/reports/bas", params={"fy_year": fy, "quarter": q}).json()
            checks["reports"][f"gst_FY{fy}_Q{q}"] = api.get("/api/v1/reports/gst-exposure", params={"fy_year": fy, "quarter": q}).json()
    pdf = api.get("/api/v1/reports/bas/pdf", params={"fy_year": 2024, "quarter": 3})
    checks["reports"]["bas_pdf_fy2024_q3_bytes"] = len(pdf.content)

    route_probes = {
        "dashboard": ("/api/v1/dashboard/summary", {}),
        "accounts": ("/api/v1/accounts", {}),
        "journal": ("/api/v1/journal", {}),
        "invoices": ("/api/v1/invoices", {}),
        "outgoing": ("/api/v1/outgoing", {}),
        "clients": ("/api/v1/clients", {}),
        "contacts": ("/api/v1/contacts", {}),
        "bank_accounts": ("/api/v1/bank-accounts", {}),
        "bank_rules": ("/api/v1/bank-rules", {}),
        "reconciliation": ("/api/v1/bank-accounts/transactions/uncategorised", {}),
        "reports": ("/api/v1/reports/profit-loss", {"period_start": "2024-07-01", "period_end": "2025-06-30"}),
    }
    for name, (path, params) in route_probes.items():
        try:
            r = api.get(path, params=params)
            checks["routes"][name] = {"status_code": r.status_code, "items": len(r.json()) if isinstance(r.json(), list) else None}
        except Exception as e:
            checks["routes"][name] = {"error": str(e)}
    return checks


def seed(api: Api, *, reset: bool = False, exercise: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    if reset:
        reset_company_storage(COMPANY_ID)
    create_company(api)
    rng = random.Random(RANDOM_SEED)
    accounts = load_accounts(api)
    create_bank_rules(api, accounts)
    seed_journals(api, accounts)
    clients = create_clients(api)
    payments = create_invoices_and_docs(api, rng, clients["clients"])
    bank = seed_bank(api, accounts, payments, rng)
    checks = exercise_reports_and_routes(api) if exercise else {}
    counts = table_counts(COMPANY_ID)
    elapsed = time.monotonic() - started
    totals = {
        "bank_accounts": api.get("/api/v1/bank-accounts").json(),
        "trial_balance_2025_06_30": api.get("/api/v1/reports/trial-balance", params={"as_of": "2025-06-30"}).json(),
        "balance_sheet_2025_06_30": api.get("/api/v1/reports/balance-sheet", params={"as_of": "2025-06-30"}).json(),
    }
    return {
        "company_id": COMPANY_ID,
        "company_name": COMPANY_NAME,
        "random_seed": RANDOM_SEED,
        "period_start": iso(FY_START),
        "period_end": iso(FY_END),
        "runtime_seconds": round(elapsed, 2),
        "row_counts": counts,
        "clients": clients,
        "bank_import": bank,
        "checks": checks,
        "totals": totals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before --reset deletion.")
    parser.add_argument("--skip-exercise", action="store_true", help="Only seed data; skip report/sidebar probes.")
    args = parser.parse_args(argv)

    if args.reset and not args.yes:
        answer = input(f"Delete and recreate {COMPANY_ID!r} in {data_dir()}? Type 'yes': ")
        if answer.strip().lower() != "yes":
            print("Aborted.", file=sys.stderr)
            return 2

    api = Api.from_url(args.base_url)
    try:
        summary = seed(api, reset=args.reset, exercise=not args.skip_exercise)
    finally:
        api.close()
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
