from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .db.errors import DataRecoveryRequiredError
from .db.master import init_master_db, validate_company_storage_registry
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
from .frontend import configure_frontend


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Master DB: create tables + reflect-add any new columns.
    master_added = init_master_db(allow_create=True)
    if master_added:
        print(
            f"[startup] master: added columns {master_added}",
            flush=True,
        )

    # Reconcile the registry and physical ledgers before any path can lazily
    # open a company engine. Missing or orphan books require recovery, never an
    # empty SQLite replacement.
    validate_company_storage_registry()

    # Per-company DBs: same reflection-based sync, then hand-rolled steps
    # for things reflection can't do (drops, rebuilds, partial indexes),
    # then a fail-closed signature check before any company can serve writes.
    from .db.base import CompanyBase
    from .db.master import MasterSession
    from .db.company import init_company_db, company_session, get_company_engine
    from .db.schema_sync import detect_drift, require_clean_schema
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
            require_clean_schema(report)
            with company_session(company.id) as csession:
                seed_default_bank_accounts(csession)

    yield
    # No teardown work today — kept for symmetry / future use.


def create_app() -> FastAPI:
    app = FastAPI(
        title="Open Accounting (Local)",
        version="0.2.0",
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

    @app.exception_handler(DataRecoveryRequiredError)
    async def data_recovery_required(_request, exc: DataRecoveryRequiredError):
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": exc.code,
                    "message": str(exc),
                }
            },
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

    # --- Packaged desktop / production static frontend ---------------------
    # In development, Vite serves the React app. In the portable Windows build,
    # PyInstaller bundles frontend/dist and the FastAPI process serves it.
    configure_frontend(app)

    return app


app = create_app()
