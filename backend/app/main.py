from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db.master import init_master_db
from .api.v1 import (
    accounts,
    bank_accounts,
    bank_rules,
    clients,
    companies,
    contacts,
    dashboard,
    invoices,
    journal,
    outgoing,
    reports,
    staff,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Master DB: create tables + reflect-add any new columns.
    master_added = init_master_db()
    if master_added:
        print(
            f"[startup] master: added columns {master_added}",
            flush=True,
        )

    # Per-company DBs: same reflection-based sync, then hand-rolled steps
    # for things reflection can't do (drops, rebuilds, partial indexes),
    # then a drift check so any residual mismatch is loud at startup.
    from .db.base import CompanyBase
    from .db.master import MasterSession
    from .db.company import init_company_db, company_session, get_company_engine
    from .db.schema_sync import detect_drift
    from .models.master import Company
    from .services.bank_accounts import seed_default_bank_accounts

    with MasterSession() as db:
        for company in db.query(Company).all():
            # create_all → sync_missing_columns → run_company_migrations,
            # in that order: migrations may touch columns newer than the
            # DB file, so the additive sync must land first.
            added, applied = init_company_db(company.id)
            if added:
                print(
                    f"[startup] company '{company.id}': added columns {added}",
                    flush=True,
                )
            if applied:
                print(
                    f"[startup] company '{company.id}': applied migrations "
                    f"{applied}",
                    flush=True,
                )
            engine = get_company_engine(company.id)
            report = detect_drift(engine, CompanyBase, f"company:{company.id}")
            if not report.is_clean:
                print(report.format(), flush=True)
            with company_session(company.id) as csession:
                seed_default_bank_accounts(csession)

    yield
    # No teardown work today — kept for symmetry / future use.


def create_app() -> FastAPI:
    app = FastAPI(
        title="Open Accounting (Local)",
        version="0.1.0",
        description=(
            "Local-first accounting backend for AU SMEs (AUD / GST): "
            "ledger, bank import, reports, and client-facing documents "
            "(receipts and invoices)."
        ),
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    def health():
        return {
            "status": "ok",
            "data_dir": str(settings.data_dir),
        }

    # --- Accounting (ledger) ---------------------------------------------
    app.include_router(companies.router, prefix="/api/v1")
    app.include_router(accounts.router, prefix="/api/v1")
    app.include_router(contacts.router, prefix="/api/v1")
    app.include_router(invoices.router, prefix="/api/v1")
    app.include_router(clients.router, prefix="/api/v1")
    app.include_router(bank_accounts.router, prefix="/api/v1")
    app.include_router(bank_rules.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(dashboard.router, prefix="/api/v1")
    app.include_router(journal.router, prefix="/api/v1")

    # --- Documents ---------------------------------------------------------
    # Importing api.v1.outgoing is what registers the document tables
    # (OutgoingDocument, OutgoingDocumentLine, DocumentCounter) with
    # CompanyBase.metadata via the api.v1.outgoing → models.outgoing chain.
    app.include_router(outgoing.router, prefix="/api/v1")

    # --- Staff (document signers) ------------------------------------------
    app.include_router(staff.router, prefix="/api/v1")

    return app


app = create_app()
