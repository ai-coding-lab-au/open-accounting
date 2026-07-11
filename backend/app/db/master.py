import os
import sqlite3
from pathlib import Path
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from uuid import uuid4

from ..config import settings
from .base import MasterBase
from .errors import DataRecoveryRequiredError
from .schema_sync import detect_drift, require_clean_schema, sync_missing_columns


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


def _ensure_company_generations() -> list[str]:
    """Add/backfill the immutable company generation column and guards.

    ``sync_missing_columns`` cannot add this particular NOT NULL column to a
    populated SQLite table without inventing one shared scalar default.  Add
    it nullable for legacy databases, backfill each row with its own UUID, and
    install triggers that enforce the same non-empty/immutable contract as a
    freshly-created database from then on.
    """
    changed: list[str] = []
    with master_engine.begin() as conn:
        existing = {
            row[1] for row in conn.execute(text("PRAGMA table_info(companies)"))
        }
        if "generation_id" not in existing:
            conn.execute(
                text(
                    'ALTER TABLE companies ADD COLUMN "generation_id" VARCHAR(36)'
                )
            )
            changed.append("companies.generation_id")

        missing_ids = conn.execute(
            text(
                "SELECT id FROM companies "
                "WHERE generation_id IS NULL OR generation_id = ''"
            )
        ).scalars().all()
        for company_id in missing_ids:
            conn.execute(
                text(
                    "UPDATE companies SET generation_id = :generation_id "
                    "WHERE id = :company_id"
                ),
                {
                    "company_id": company_id,
                    "generation_id": str(uuid4()),
                },
            )
        if missing_ids:
            changed.append(f"backfill:companies.generation_id:{len(missing_ids)}")

        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_companies_generation_id "
                "ON companies (generation_id)"
            )
        )

        # Existing SQLite tables keep the nullable ADD COLUMN shape, so DB
        # triggers carry the invariant for both old and new master databases.
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS companies_generation_required_insert
                BEFORE INSERT ON companies
                WHEN NEW.generation_id IS NULL OR NEW.generation_id = ''
                BEGIN
                    SELECT RAISE(ABORT, 'company generation_id is required');
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS companies_generation_immutable
                BEFORE UPDATE OF generation_id ON companies
                WHEN NEW.generation_id IS NOT OLD.generation_id
                BEGIN
                    SELECT RAISE(ABORT, 'company generation_id is immutable');
                END
                """
            )
        )
        # A very old master schema declared ``id VARCHAR PRIMARY KEY`` without
        # an explicit NOT NULL. SQLite permits NULL in that legacy rowid-table
        # shape even though fresh SQLAlchemy DDL emits NOT NULL. These guards
        # safely provide the missing invariant without rebuilding the registry.
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS companies_id_required_insert
                BEFORE INSERT ON companies
                WHEN NEW.id IS NULL
                BEGIN
                    SELECT RAISE(ABORT, 'company id is required');
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS companies_id_required_update
                BEFORE UPDATE OF id ON companies
                WHEN NEW.id IS NULL
                BEGIN
                    SELECT RAISE(ABORT, 'company id is required');
                END
                """
            )
        )
    return changed


settings.data_dir.mkdir(parents=True, exist_ok=True)

_master_initialised = False


def physical_company_databases() -> dict[str, Path]:
    """Return immediate companies/<id>/books.db files without modifying them."""
    companies_dir = settings.data_dir / "companies"
    if not companies_dir.is_dir():
        return {}
    return {
        child.name: child / "books.db"
        for child in companies_dir.iterdir()
        if child.is_dir() and (child / "books.db").is_file()
    }


def _missing_master_error() -> DataRecoveryRequiredError:
    return DataRecoveryRequiredError(
        f"Master company registry is missing: {settings.master_db_path}. "
        "Restore master.db from a verified backup. The application refused "
        "to start with an empty replacement registry."
    )


def require_master_db_file() -> None:
    if not settings.master_db_path.is_file():
        raise _missing_master_error()


def _connect_existing_master():
    return sqlite3.connect(
        f"file:{settings.master_db_path.as_posix()}?mode=rw",
        uri=True,
        check_same_thread=False,
    )

master_engine = create_engine(
    _sqlite_url(settings.master_db_path),
    future=True,
    creator=_connect_existing_master,
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


def init_master_db(*, allow_create: bool = False) -> list[str]:
    """Create master tables and reconcile additive column changes. Idempotent.

    Returns the list of "table.column" entries that were added this call, so
    the caller can log them. ``allow_create`` is reserved for application
    first-run startup and is still rejected when physical company books exist.
    """
    global _master_initialised

    from ..models import master as _master_models  # noqa: F401 (import side-effect: register models)

    if not settings.master_db_path.is_file():
        physical = physical_company_databases()
        if physical:
            company_ids = ", ".join(sorted(physical))
            raise DataRecoveryRequiredError(
                f"Master company registry is missing while physical company "
                f"databases exist for: {company_ids}. Restore master.db; the "
                "orphan books were not modified."
            )
        if _master_initialised or not allow_create:
            raise _missing_master_error()
        settings.master_db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                settings.master_db_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise DataRecoveryRequiredError(
                f"Master registry path appeared during provisioning: "
                f"{settings.master_db_path}. Startup stopped for operator review."
            ) from exc
        else:
            os.close(fd)

    MasterBase.metadata.create_all(master_engine)
    dropped = _drop_stale_master_columns()
    generation_changes = _ensure_company_generations()
    added = sync_missing_columns(master_engine, MasterBase)
    require_clean_schema(detect_drift(master_engine, MasterBase, "master"))
    _master_initialised = True
    return [f"drop:{c}" for c in dropped] + generation_changes + added


def validate_company_storage_registry() -> None:
    """Fail startup on either missing registered books or orphan physical books."""
    require_master_db_file()
    from ..models.master import Company

    with MasterSession() as session:
        registered = {row[0] for row in session.query(Company.id).all()}
    physical = set(physical_company_databases())
    missing = sorted(registered - physical)
    orphaned = sorted(physical - registered)
    if not missing and not orphaned:
        return

    details: list[str] = []
    if missing:
        details.append("registered companies missing books.db: " + ", ".join(missing))
    if orphaned:
        details.append("unregistered orphan books.db files: " + ", ".join(orphaned))
    raise DataRecoveryRequiredError(
        "Company registry/storage mismatch ("
        + "; ".join(details)
        + "). Restore the matching registry or book files from backup; no orphan "
        "database was modified."
    )
