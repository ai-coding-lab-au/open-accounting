from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from ..config import settings
from .base import MasterBase
from .schema_sync import sync_missing_columns


def _sqlite_url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


# Columns removed from the Company model that must be dropped from an existing
# master.db. Unlike per-company DBs, master.db has no full migration runner —
# only additive sync_missing_columns — so a removed NOT NULL column (here
# bank_is_trust_account, dropped with the trust model) breaks INSERT of any new
# company: the model no longer supplies a value and the DB rejects the NULL.
# These 5 columns were retired with the trust removal + staff-signer refactor
# and carry no index or FK, so a plain DROP COLUMN is safe.
MASTER_DB_COLUMN_DROPS: tuple[str, ...] = (
    "bank_is_trust_account",
    "registered_agent_name",
    "marn",
    "registered_legal_practitioner_name",
    "lpn",
)


def _drop_stale_master_columns() -> list[str]:
    """Drop retired Company columns from an existing master.db. Idempotent."""
    dropped: list[str] = []
    with master_engine.begin() as conn:
        existing = {
            row[1] for row in conn.execute(text("PRAGMA table_info(companies)"))
        }
        # DROP COLUMN can't run while an index references the column; none of
        # these had one, but drop any (defensively) before the column.
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        try:
            for column in MASTER_DB_COLUMN_DROPS:
                if column not in existing:
                    continue
                for (idx_name,) in conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND tbl_name='companies' AND name NOT LIKE 'sqlite_%'"
                    )
                ).fetchall():
                    cols = [
                        r[2]
                        for r in conn.execute(text(f'PRAGMA index_info("{idx_name}")'))
                    ]
                    if column in cols:
                        conn.execute(text(f'DROP INDEX IF EXISTS "{idx_name}"'))
                conn.execute(
                    text(f'ALTER TABLE companies DROP COLUMN "{column}"')
                )
                dropped.append(f"companies.{column}")
        finally:
            conn.execute(text("PRAGMA foreign_keys = ON"))
    return dropped


settings.data_dir.mkdir(parents=True, exist_ok=True)

master_engine = create_engine(
    _sqlite_url(settings.master_db_path),
    future=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(master_engine, "connect")
def _enable_sqlite_fk(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers and one writer overlap; busy_timeout makes a
    # second writer wait instead of failing "database is locked".
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False, future=True)


def init_master_db() -> list[str]:
    """Create master tables and reconcile additive column changes. Idempotent.

    Returns the list of "table.column" entries that were added this call, so
    the caller can log them.
    """
    from ..models import master as _master_models  # noqa: F401 (import side-effect: register models)

    MasterBase.metadata.create_all(master_engine)
    dropped = _drop_stale_master_columns()
    added = sync_missing_columns(master_engine, MasterBase)
    return [f"drop:{c}" for c in dropped] + added
