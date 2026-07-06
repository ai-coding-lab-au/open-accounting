"""Per-company SQLite engine management.

One SQLite file per company under data/companies/{id}/books.db.
Engines are lazily created and cached. The company_id is treated as an opaque
identifier; the caller is responsible for verifying it exists in the master DB.
"""

from __future__ import annotations

from threading import Lock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .base import CompanyBase


_engines: dict[str, Engine] = {}
_sessionmakers: dict[str, sessionmaker] = {}
_lock = Lock()


def _build_engine(company_id: str) -> Engine:
    db_path = settings.company_db_path(company_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
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


def get_company_engine(company_id: str) -> Engine:
    with _lock:
        eng = _engines.get(company_id)
        if eng is None:
            eng = _build_engine(company_id)
            _engines[company_id] = eng
            _sessionmakers[company_id] = sessionmaker(
                bind=eng, autoflush=False, autocommit=False, future=True
            )
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


def init_company_db(company_id: str) -> tuple[list[str], list[str]]:
    """Create all per-company tables, sync additive columns, then run
    hand-rolled migrations.

    Idempotent. Column sync runs BEFORE migrations so steps that touch
    columns newer than the DB file (the bank_transactions rebuild, the
    dedup partial index) always see the full live schema.

    Returns (added_columns, applied_migrations) so the caller can log them.
    """
    from ..models import company as _company_models  # noqa: F401
    from .migrations import run_company_migrations
    from .schema_sync import sync_missing_columns

    engine = get_company_engine(company_id)
    CompanyBase.metadata.create_all(engine)
    added = sync_missing_columns(engine, CompanyBase)
    applied = run_company_migrations(engine)
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
