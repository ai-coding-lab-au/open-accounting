# Open Accounting

Local-first accounting for small Australian service firms (AUD / GST).
Everything runs on your own machine: FastAPI + SQLite backend, React frontend,
no cloud, no telemetry, your books never leave your computer.

Extracted from an in-house practice-management system; the ledger and document
modules are included, the practice-specific modules are not.

## Features

**Ledger (accounting)**
- Multi-company: each company is its own SQLite file (`books.db`), plus a master registry
- Chart of accounts (AU SME default seeded), manual journal entries with balanced-lines validation
- Bank account with statement import (CSV/XLSX/PDF, UTF-8 English or simple Chinese column headers), dedupe, categorisation rules
- Reconciliation view for uncategorised transactions, including explicit bank-to-invoice payment allocation
- Reports as JSON + PDF: P&L, trial balance, balance sheet, GST activity summary/tax-code analysis, bank statement
- Supplier (AP) / customer (AR) invoices with GL posting on authorise and allocation-derived paid status
- Monotonic accounting-period lock: dated writes in a closed period fail server-side

**Invoice editor**
- Compact manual AR/AP editor with independent invoice lines and stable IDs. Each line has a description, quantity, unit price, account, tax code, subtotal, GST, total, and removal control. Accounts are selected per line, not duplicated in the header.
- Read-only invoice totals sum the rounded line amounts. Save Draft creates a draft without a journal entry; customer invoices retain AR direction and supplier invoices retain AP direction.
- Amount modes are Tax exclusive, Tax inclusive, and No tax. Line tax treatments include standard GST, GST-free, input-taxed, Outside GST/no tax, and AP-asset Capital purchase. Capital purchase is limited to eligible AP asset lines.
- Frontend calculations use BigInt-based money arithmetic for exact cents and round each line before summing the header. The backend independently validates with Decimal and ROUND_HALF_UP. API money values remain decimal-dollar strings.
- No tax forces Outside GST (`none`); non-GST companies are forced to No tax. Visible tax labels no longer include “(provisional)”. This label change is not a compliance certification.

**Documents**
- Receipts issued directly to a client (line items, GST-inclusive/exclusive, void/restore)
- Print-quality PDFs (Chromium HTML render when Playwright is available, ReportLab fallback);
  Chinese client/company names render correctly (bundled Noto Sans SC, OFL-1.1), with an
  optional per-company bilingual English+Chinese label mode for receipts
- Per-year document numbering with race-safe counters

## Quick start

Backend (Python 3.11+):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env                                 # set DATA_DIR (see notes in the file)
uvicorn app.main:app --port 8787
```

Frontend (Node 20+):

```bash
cd frontend
npm ci
npm run dev          # http://127.0.0.1:5173 — proxies /api to 127.0.0.1:8787
```

Open http://127.0.0.1:5173, create a company, and you're in.
Convenience launchers: `./start.sh` (Linux/macOS) or `./start.ps1` (Windows).

## Verifying the system

```bash
cd backend
ALLOW_UNSAFE_DATA_DIR=1 python -m pytest      # unit + API tests
python scripts/smoke_e2e.py                   # boots a throwaway server, runs a
                                              # full company/accounting smoke flow
cd ../frontend
npm run build                                 # typecheck + production build
```

PowerShell:

```powershell
cd backend
$env:ALLOW_UNSAFE_DATA_DIR = "1"
python -m pytest
python scripts\smoke_e2e.py
cd ..\frontend
npm run build
```

### Branch verification

The following checks passed for the work documented here:
- Frontend production build.
- Invoice tax Playwright suite: 12 passed.
- Journal provenance Playwright suite: 1 passed.
- Journal immutability: 98 backend tests passed across separately invoked files.
- `test_invoice_posting.py`: 14 passed; `test_invoice_posting_integration.py`: 2 passed.
- `test_gst_m23.py`: 11 passed; `test_invoice_payment_allocations.py`: 14 passed.
- `test_reports_m22.py`: 7 passed; `test_reports_p1_seams.py`: 7 passed.

These are separate results, not a combined unique test total. The backend checks emitted a non-blocking FastAPI/Starlette `TestClient` deprecation warning.

Known test-isolation issue: running `test_invoice_api.py` before `test_invoice_posting.py` in one Pytest process can cause order-dependent SQLAlchemy mapper errors involving `OutgoingDocumentLine.order_no`. The invoice API fixture removes application modules from `sys.modules` while posting tests may retain an older SQLAlchemy model registry. Both files pass in separate clean Python processes. This issue is not fixed; unrelated production models should not be altered to mask it.

## Layout

```
backend/
  app/api/v1/      HTTP routers (companies, accounts, journal, bank, reports,
                   invoices, outgoing documents (receipts), clients, staff)
  app/services/    business logic (posting, numbering, GST, renderers, imports)
  app/models/      SQLAlchemy models — master registry vs per-company tables
  app/db/          engine cache, additive schema sync, hand-rolled migrations
  scripts/         smoke_e2e.py, seed_realistic.py (demo data)
  tests/           pytest suite
frontend/
  src/pages/       one page per screen
  src/components/  document editors/drawers, layout, UI primitives
  src/types/       API types (single file)
```

Company database selection uses two HTTP headers: `X-Company-Id` identifies
the company slug and `X-Company-Generation` identifies that specific database
generation. The backend validates both before opening the company's SQLite
file, so a stale tab cannot act on a same-slug company recreated after deletion.
Manual bank-transaction creates additionally require an `Idempotency-Key`
header; retry the same logical request with the same key.

## Data safety

`DATA_DIR` must point OUTSIDE the repo (the app rejects in-tree paths unless
`ALLOW_UNSAFE_DATA_DIR=1`, which is for tests only). Every `*.db` pattern is
gitignored as a second line of defence. Back up your `DATA_DIR`.

## Known heritage & roadmap

- Bank feed: statement file import only; no live bank feeds.
- Settle posted invoices through Reconciliation by selecting Accounts
  Receivable (1100) / Accounts Payable (2000) and allocating the cash to the
  exact invoice(s). Overpayment remainders stay on the same bank row and must
  go to Customer Deposits (2050) or Supplier Prepayments (1500). Applying such
  a remainder to a later invoice is an explicit adjustment workflow, not an
  automatic memo match.
- Invoice journals post GST to the GST Collected or GST Paid control accounts. Trial Balance, Profit and Loss, Balance Sheet, and journal/report integration are covered by existing tests.
- Existing GST/BAS-support reporting is cash-basis decision support, based on bank transactions and invoice-payment tax components. An unpaid invoice journal is not itself a cash-basis GST-report transaction. Ordinary manual journal lines do not have the tax metadata required for automatic GST-report classification.
- GST-aware adjustments and credit notes are future controlled work. All Australian GST, BAS, Capital GST, tax classification, and compliance behavior requires review by an Australian accountant or registered BAS agent before live use. No BAS has been lodged.
- This application has not been deployed and is not production-ready. Use fictional data only until security, permissions, backups, restore testing, and accounting controls are verified. Keep ports 5173 and 8787 private. Hosting, backups, maintenance, security, payment processing, accountant review, and compliance may still have costs.
- Future controlled phases include credit notes linked to source invoices; GST-aware adjustment workflows with explicit tax classification and provenance; atomic backend invoice numbering; accountant access and permissions; backup and restore verification; and security and production-readiness review.
- Chromium PDF rendering is optional. Install it with
  `pip install -e ".[pdf]"` and `playwright install chromium`; otherwise the
  backend falls back to ReportLab rendering. The Windows portable build
  ships the ReportLab renderer only.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).
