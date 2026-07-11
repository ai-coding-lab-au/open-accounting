"""Per-company SQLite engine management.

One SQLite file per company under data/companies/{id}/books.db.
Engines are lazily created and cached. The company_id is treated as an opaque
identifier; the caller is responsible for verifying it exists in the master DB.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from threading import Lock
from typing import Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .base import CompanyBase
from .errors import DataRecoveryRequiredError


_engines: dict[str, Engine] = {}
_sessionmakers: dict[str, sessionmaker] = {}
_lifecycle_locks: dict[str, Lock] = {}
_lock = Lock()


@contextmanager
def company_lifecycle_lock(company_id: str) -> Generator[None, None, None]:
    """Serialise a company's DB requests with create/delete/recreate.

    The lock object intentionally survives deletion. Removing it would let a
    waiter holding the old object overlap a re-create using a newly allocated
    lock for the same slug.
    """
    with _lock:
        lifecycle_lock = _lifecycle_locks.setdefault(company_id, Lock())
    lifecycle_lock.acquire()
    try:
        yield
    finally:
        lifecycle_lock.release()


def _missing_company_db_error(company_id: str) -> DataRecoveryRequiredError:
    db_path = settings.company_db_path(company_id)
    return DataRecoveryRequiredError(
        f"Company database for registered company '{company_id}' is missing: "
        f"{db_path}. Restore books.db from a verified backup. The application "
        "refused to create an empty replacement."
    )


def _reserve_new_company_db(company_id: str) -> None:
    """Allocate a new empty file only for an explicit provisioning caller."""
    db_path = settings.company_db_path(company_id)
    company_dir = db_path.parent
    companies_dir = company_dir.parent
    companies_dir.mkdir(parents=True, exist_ok=True)
    try:
        company_dir.mkdir()
    except FileExistsError as exc:
        raise DataRecoveryRequiredError(
            f"Refusing to provision company '{company_id}' over the existing "
            f"unregistered directory {company_dir}. Restore its master registry "
            "row or move it through an explicit recovery workflow first."
        ) from exc
    try:
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except Exception:
        # This directory was allocated by this provisioning call and contains
        # no accepted ledger yet. Restore the pre-call absence on allocation
        # failure; never remove a directory that existed before the call.
        company_dir.rmdir()
        raise
    else:
        os.close(fd)


def _build_engine(company_id: str) -> Engine:
    db_path = settings.company_db_path(company_id)
    if not db_path.is_file():
        raise _missing_company_db_error(company_id)

    # Every pooled connection uses SQLite URI mode=rw. Unlike the default
    # sqlite3 behaviour, mode=rw refuses to create a missing file, closing the
    # TOCTOU gap between our path check and a later pool checkout.
    def _connect_existing():
        return sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=rw",
            uri=True,
            check_same_thread=False,
        )

    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        creator=_connect_existing,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers and one writer overlap; busy_timeout makes a
        # second writer wait instead of failing "database is locked"
        # (the background writing-jobs worker writes concurrently).
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def get_company_engine(company_id: str, *, allow_create: bool = False) -> Engine:
    db_path = settings.company_db_path(company_id)
    stale_engine: Engine | None = None
    with _lock:
        eng = _engines.get(company_id)
        if allow_create:
            if eng is not None or db_path.parent.exists():
                raise DataRecoveryRequiredError(
                    f"Refusing to provision company '{company_id}' over an "
                    "existing or cached ledger. Only a brand-new company "
                    "directory may use allow_create."
                )
            _reserve_new_company_db(company_id)
        elif eng is not None and not db_path.is_file():
            _engines.pop(company_id, None)
            _sessionmakers.pop(company_id, None)
            stale_engine = eng
            eng = None

        if eng is None and db_path.is_file():
            eng = _build_engine(company_id)
            _engines[company_id] = eng
            _sessionmakers[company_id] = sessionmaker(
                bind=eng, autoflush=False, autocommit=False, future=True
            )
    if stale_engine is not None:
        stale_engine.dispose()
    if eng is None:
        raise _missing_company_db_error(company_id)
    return eng


def dispose_company_engine(company_id: str) -> None:
    """Drop a company's cached engine + sessionmaker and close its connections.

    Must be called before deleting a company's on-disk DB: otherwise the cached
    engine keeps a (WAL) handle on the now-deleted file, and a later create of
    the same id would reuse the stale engine. No-op if nothing is cached.
    """
    with _lock:
        eng = _engines.pop(company_id, None)
        _sessionmakers.pop(company_id, None)
    if eng is not None:
        eng.dispose()


def init_company_db(
    company_id: str, *, allow_create: bool = False
) -> tuple[list[str], list[str]]:
    """Bring one company database to its complete runtime invariant.

    Order is create tables, sync additive columns, run structural migrations,
    seed the default chart only when the chart is empty, then reconcile the
    canonical system accounts. Column sync must precede migrations so
    older rebuilds always see the full live schema. The entire sequence is
    idempotent across startup and provisioning retries.

    ``allow_create`` is a provisioning capability, not a convenience default:
    it is accepted only when the entire company directory is absent. Every
    startup/request path leaves it false and therefore cannot create books.db.

    Returns (added_columns, applied_migrations) so the caller can log them.
    """
    from ..models import company as _company_models  # noqa: F401
    from ..services.chart_of_accounts import seed_default_coa
    from ..services.invoice_payments import (
        PaymentAllocationError,
        backfill_missing_tax_components,
    )
    from .migrations import reconcile_system_accounts, run_company_migrations
    from .schema_sync import detect_drift, require_clean_schema, sync_missing_columns

    engine = get_company_engine(company_id, allow_create=allow_create)
    CompanyBase.metadata.create_all(engine)
    added = sync_missing_columns(engine, CompanyBase)
    applied = run_company_migrations(engine, enforce_schema_gate=True)
    require_clean_schema(
        detect_drift(engine, CompanyBase, f"company:{company_id}")
    )
    # Seed an entirely new/empty ledger before reconciliation. This preserves
    # the complete default chart and means a brand-new company does not create
    # a pointless pre-repair backup merely because all system rows are not
    # present yet. Partial legacy charts are left intact for the scoped repair.
    with Session(engine) as session:
        seeded = seed_default_coa(session)
    if seeded:
        applied.append(f"seed:default_coa:{seeded}")
    applied.extend(reconcile_system_accounts(engine))
    try:
        with Session(engine) as session:
            rebuilt_allocations = backfill_missing_tax_components(session)
    except PaymentAllocationError as exc:
        raise DataRecoveryRequiredError(
            "Legacy invoice payment allocations could not be reconciled safely: "
            f"{exc} Restore the pre-migration backup or review the recorded "
            "payment reconciliation events before retrying."
        ) from exc
    if rebuilt_allocations:
        applied.append(
            f"backfill:invoice_payment_tax_components:{rebuilt_allocations}"
        )
    return added, applied


def company_session(company_id: str) -> Session:
    get_company_engine(company_id)  # ensure built
    return _sessionmakers[company_id]()


def begin_sqlite_immediate(db: Session) -> None:
    """Start a SQLite write transaction before stale reads can occur.

    pysqlite defers BEGIN until the first write, so a SELECT-then-INSERT
    check (doc-number allocation, trust balance checks) can read stale data
    under concurrency. Calling this first takes the write lock up front.
    No-op on non-SQLite binds or when the session already has a transaction.
    """
    bind = db.get_bind()
    if bind.dialect.name != "sqlite" or db.in_transaction():
        return
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
