import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from uuid import uuid4

from ..config import settings
from .base import MasterBase
from .errors import DataRecoveryRequiredError
from .schema_sync import (
    MASTER_AUTHORIZATION_INDEX_SIGNATURES,
    detect_drift,
    normalise_sql_expression,
    require_clean_schema,
    sqlite_index_matches,
    sync_missing_columns,
)


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


def _canonical_utc_timestamp_sql(value: str) -> str:
    digit_pattern = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]"
    year = f"CAST(substr({value}, 1, 4) AS INTEGER)"
    month = f"CAST(substr({value}, 6, 2) AS INTEGER)"
    day = f"CAST(substr({value}, 9, 2) AS INTEGER)"
    hour = f"CAST(substr({value}, 12, 2) AS INTEGER)"
    minute = f"CAST(substr({value}, 15, 2) AS INTEGER)"
    second = f"CAST(substr({value}, 18, 2) AS INTEGER)"
    leap_year = f"({year} % 4 = 0 AND ({year} % 100 != 0 OR {year} % 400 = 0))"
    days_in_month = (
        f"CASE {month} WHEN 2 THEN 28 + ({leap_year}) "
        f"WHEN 4 THEN 30 WHEN 6 THEN 30 WHEN 9 THEN 30 WHEN 11 THEN 30 ELSE 31 END"
    )
    return (
        f"({value} IS NOT NULL AND length({value}) = 26 "
        f"AND {value} GLOB '{digit_pattern}' "
        f"AND {year} BETWEEN 1 AND 9999 AND {month} BETWEEN 1 AND 12 "
        f"AND {day} BETWEEN 1 AND {days_in_month} "
        f"AND {hour} BETWEEN 0 AND 23 AND {minute} BETWEEN 0 AND 59 "
        f"AND {second} BETWEEN 0 AND 59)"
    )


def _master_authorization_trigger_definitions() -> dict[str, str]:
    subject_created = _canonical_utc_timestamp_sql("NEW.created_at")
    subject_deactivated = _canonical_utc_timestamp_sql("NEW.deactivated_at")
    membership_created = _canonical_utc_timestamp_sql("NEW.created_at")
    membership_start = _canonical_utc_timestamp_sql("NEW.valid_from")
    membership_end = _canonical_utc_timestamp_sql("NEW.valid_until")
    membership_deactivated = _canonical_utc_timestamp_sql("NEW.deactivated_at")
    grant_created = _canonical_utc_timestamp_sql("NEW.created_at")
    grant_start = _canonical_utc_timestamp_sql("NEW.valid_from")
    grant_end = _canonical_utc_timestamp_sql("NEW.valid_until")
    grant_revoked = _canonical_utc_timestamp_sql("NEW.revoked_at")

    return {
        "authenticated_subjects_subject_id_required_insert": """
            CREATE TRIGGER authenticated_subjects_subject_id_required_insert
            BEFORE INSERT ON authenticated_subjects
            WHEN NEW.subject_id IS NULL OR trim(NEW.subject_id) = ''
            BEGIN
                SELECT RAISE(ABORT, 'subject_id is required');
            END
        """,
        "authenticated_subjects_auth_method_required": """
            CREATE TRIGGER authenticated_subjects_auth_method_required
            BEFORE INSERT ON authenticated_subjects
              WHEN NEW.authentication_method IS NULL
                  OR NEW.authentication_method NOT IN ('internal_adapter', 'trusted_adapter')
            BEGIN
                SELECT RAISE(ABORT, 'authentication_method is invalid');
            END
        """,
        "authenticated_subjects_lifecycle_insert": f"""
            CREATE TRIGGER authenticated_subjects_lifecycle_insert
            BEFORE INSERT ON authenticated_subjects
              WHEN NEW.active IS NOT 1 OR NEW.deactivated_at IS NOT NULL
                  OR NOT {subject_created}
            BEGIN
                SELECT RAISE(ABORT, 'authenticated subjects must be created active with canonical timestamps');
            END
        """,
        "authenticated_subjects_lifecycle_update": f"""
            CREATE TRIGGER authenticated_subjects_lifecycle_update
            BEFORE UPDATE ON authenticated_subjects
            WHEN NEW.subject_id IS NOT OLD.subject_id
                 OR NEW.authentication_method IS NOT OLD.authentication_method
                 OR NEW.provider_subject IS NOT OLD.provider_subject
                 OR NEW.credential_version IS NOT OLD.credential_version
                 OR NEW.created_at IS NOT OLD.created_at
                 OR NOT {_canonical_utc_timestamp_sql('OLD.created_at')}
                 OR OLD.active IS NULL OR OLD.active NOT IN (0, 1)
                 OR NEW.active IS NULL OR NEW.active NOT IN (0, 1)
                 OR (OLD.active = 0 AND (NEW.active != 0 OR NEW.deactivated_at IS NOT OLD.deactivated_at))
                 OR (OLD.active = 1 AND NEW.active = 1 AND NEW.deactivated_at IS NOT OLD.deactivated_at)
                 OR (OLD.active = 1 AND NEW.active = 0 AND
                     (OLD.deactivated_at IS NOT NULL OR NEW.deactivated_at IS NULL
                      OR NOT {subject_deactivated}
                      OR NEW.deactivated_at < OLD.created_at))
            BEGIN
                SELECT RAISE(ABORT, 'authenticated subject identity is immutable and deactivation is one-way');
            END
        """,
        "authenticated_subjects_no_delete": """
            CREATE TRIGGER authenticated_subjects_no_delete
            BEFORE DELETE ON authenticated_subjects
            BEGIN
                SELECT RAISE(ABORT, 'authenticated subjects cannot be deleted');
            END
        """,
        "company_memberships_validity_guard": f"""
            CREATE TRIGGER company_memberships_validity_guard
            BEFORE INSERT ON company_memberships
            WHEN NEW.active IS NOT 1 OR NEW.deactivated_at IS NOT NULL
                 OR NOT {membership_created} OR NOT {membership_start}
                 OR (NEW.valid_until IS NOT NULL AND
                     (NOT {membership_end}
                      OR NEW.valid_from >= NEW.valid_until))
            BEGIN
                SELECT RAISE(ABORT, 'company membership state or validity window is invalid');
            END
        """,
        "company_memberships_lifecycle_update": f"""
            CREATE TRIGGER company_memberships_lifecycle_update
            BEFORE UPDATE ON company_memberships
            WHEN NEW.id IS NOT OLD.id
                 OR NEW.subject_id IS NOT OLD.subject_id
                 OR NEW.company_id IS NOT OLD.company_id
                 OR NEW.company_generation IS NOT OLD.company_generation
                 OR NEW.valid_from IS NOT OLD.valid_from
                 OR NEW.valid_until IS NOT OLD.valid_until
                 OR NEW.created_at IS NOT OLD.created_at
                 OR NOT {_canonical_utc_timestamp_sql('OLD.created_at')}
                 OR OLD.active IS NULL OR OLD.active NOT IN (0, 1)
                 OR NEW.active IS NULL OR NEW.active NOT IN (0, 1)
                 OR (OLD.active = 0 AND (NEW.active != 0 OR NEW.deactivated_at IS NOT OLD.deactivated_at))
                 OR (OLD.active = 1 AND NEW.active = 1 AND NEW.deactivated_at IS NOT OLD.deactivated_at)
                 OR (OLD.active = 1 AND NEW.active = 0 AND
                     (OLD.deactivated_at IS NOT NULL OR NEW.deactivated_at IS NULL
                      OR NOT {_canonical_utc_timestamp_sql('NEW.deactivated_at')}
                      OR NEW.deactivated_at < OLD.created_at))
            BEGIN
                SELECT RAISE(ABORT, 'company membership identity is immutable and deactivation is one-way');
            END
        """,
        "company_memberships_no_delete": """
            CREATE TRIGGER company_memberships_no_delete
            BEFORE DELETE ON company_memberships
            BEGIN
                SELECT RAISE(ABORT, 'company memberships cannot be deleted');
            END
        """,
        "company_capability_grants_validity_guard": f"""
            CREATE TRIGGER company_capability_grants_validity_guard
            BEFORE INSERT ON company_capability_grants
            WHEN NEW.active IS NOT 1 OR NEW.revoked_at IS NOT NULL
                 OR NOT {grant_created} OR NOT {grant_start}
                 OR (NEW.valid_until IS NOT NULL AND
                     (NOT {grant_end}
                      OR NEW.valid_from >= NEW.valid_until))
            BEGIN
                SELECT RAISE(ABORT, 'capability grant state or validity window is invalid');
            END
        """,
        "company_capability_grants_lifecycle_update": f"""
            CREATE TRIGGER company_capability_grants_lifecycle_update
            BEFORE UPDATE ON company_capability_grants
            WHEN NEW.id IS NOT OLD.id
                 OR NEW.membership_id IS NOT OLD.membership_id
                 OR NEW.capability IS NOT OLD.capability
                 OR NEW.valid_from IS NOT OLD.valid_from
                 OR NEW.valid_until IS NOT OLD.valid_until
                 OR NEW.created_at IS NOT OLD.created_at
                 OR NOT {_canonical_utc_timestamp_sql('OLD.created_at')}
                 OR OLD.active IS NULL OR OLD.active NOT IN (0, 1)
                 OR NEW.active IS NULL OR NEW.active NOT IN (0, 1)
                 OR (OLD.active = 0 AND (NEW.active != 0 OR NEW.revoked_at IS NOT OLD.revoked_at))
                 OR (OLD.active = 1 AND NEW.active = 1 AND NEW.revoked_at IS NOT OLD.revoked_at)
                 OR (OLD.active = 1 AND NEW.active = 0 AND
                     (OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
                      OR NOT {_canonical_utc_timestamp_sql('NEW.revoked_at')}
                      OR NEW.revoked_at < OLD.created_at))
            BEGIN
                SELECT RAISE(ABORT, 'capability grant identity is immutable and revocation is one-way');
            END
        """,
        "company_capability_grants_no_delete": """
            CREATE TRIGGER company_capability_grants_no_delete
            BEFORE DELETE ON company_capability_grants
            BEGIN
                SELECT RAISE(ABORT, 'capability grants cannot be deleted');
            END
        """,
    }


def _repair_master_authorization_triggers(conn) -> list[str]:
    changed: list[str] = []
    for name, definition in _master_authorization_trigger_definitions().items():
        existing = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": name},
        ).scalar_one_or_none()
        if existing and normalise_sql_expression(existing) == normalise_sql_expression(definition):
            continue
        if existing:
            conn.execute(text(f'DROP TRIGGER "{name}"'))
        conn.execute(text(definition))
        repaired = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": name},
        ).scalar_one_or_none()
        if not repaired or normalise_sql_expression(repaired) != normalise_sql_expression(definition):
            raise RuntimeError(f"Master authorization trigger repair failed: {name}")
        changed.append(f"master-auth-trigger:{name}")
    return changed


_AUTHORIZATION_TIMESTAMP_PATTERN = re.compile(
    r"\A\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}\Z"
)


def _parse_master_authorization_timestamp(value, *, table: str, row_id, column: str):
    if not isinstance(value, str) or not _AUTHORIZATION_TIMESTAMP_PATTERN.fullmatch(value):
        raise DataRecoveryRequiredError(
            f"Malformed master authorization timestamp: {table}[{row_id}].{column}"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataRecoveryRequiredError(
            f"Malformed master authorization timestamp: {table}[{row_id}].{column}"
        ) from exc
    expected = parsed.isoformat(sep=" ", timespec="microseconds")
    if expected != value:
        raise DataRecoveryRequiredError(
            f"Noncanonical master authorization timestamp: {table}[{row_id}].{column}"
        )
    return parsed.replace(tzinfo=UTC)


def _validate_master_authorization_records(conn) -> None:
    definitions = (
        ("authenticated_subjects", "subject_id", ("created_at", "deactivated_at"), "deactivated_at"),
        ("company_memberships", "id", ("created_at", "valid_from", "valid_until", "deactivated_at"), "deactivated_at"),
        ("company_capability_grants", "id", ("created_at", "valid_from", "valid_until", "revoked_at"), "revoked_at"),
    )
    for table, identity_column, timestamp_columns, lifecycle_column in definitions:
        selected = ", ".join(f'"{column}"' for column in (identity_column, "active", *timestamp_columns))
        for row in conn.execute(text(f'SELECT {selected} FROM "{table}"')):
            values = row._mapping
            row_id = values[identity_column]
            parsed = {
                column: (
                    _parse_master_authorization_timestamp(
                        values[column], table=table, row_id=row_id, column=column
                    )
                    if values[column] is not None
                    else None
                )
                for column in timestamp_columns
            }
            if values["active"] not in (0, 1):
                raise DataRecoveryRequiredError(
                    f"Invalid active state in master authorization record: {table}[{row_id}]"
                )
            lifecycle = parsed[lifecycle_column]
            if (values["active"] == 1) != (lifecycle is None):
                raise DataRecoveryRequiredError(
                    f"Inconsistent lifecycle state in master authorization record: {table}[{row_id}]"
                )
            if lifecycle is not None and lifecycle < parsed["created_at"]:
                raise DataRecoveryRequiredError(
                    f"Lifecycle timestamp precedes created_at: {table}[{row_id}].{lifecycle_column}"
                )
            if "valid_until" in parsed and parsed["valid_until"] is not None:
                if parsed["valid_until"] <= parsed["valid_from"]:
                    raise DataRecoveryRequiredError(
                        f"Invalid authorization validity window: {table}[{row_id}]"
                    )


def _repair_master_authorization_schema(engine=None) -> list[str]:
    """Create or repair required master authorization indexes and guards.

    This keeps the new subject, membership, and capability tables additive and
    idempotent across clean installs and legacy master DBs.
    """
    changed: list[str] = []
    engine = engine or master_engine
    with engine.begin() as conn:
        for table, signatures in MASTER_AUTHORIZATION_INDEX_SIGNATURES.items():
            for signature in signatures:
                if sqlite_index_matches(conn, table, signature):
                    continue
                existing = conn.execute(
                    text("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=:name"),
                    {"name": signature.name},
                ).scalar_one_or_none()
                if existing == table:
                    conn.execute(text(f'DROP INDEX "{signature.name}"'))
                elif existing is not None:
                    raise RuntimeError(
                        f"Master authorization index name {signature.name} "
                        f"is already used by table {existing}"
                    )
                unique = "UNIQUE " if signature.unique else ""
                columns = ", ".join(f'"{column}"' for column in signature.columns)
                where = f" WHERE {signature.where}" if signature.where else ""
                conn.execute(
                    text(
                        f'CREATE {unique}INDEX "{signature.name}" '
                        f'ON "{table}" ({columns}){where}'
                    )
                )
                if not sqlite_index_matches(conn, table, signature):
                    raise RuntimeError(
                        f"Master authorization index repair failed: "
                        f"{table}.{signature.name}"
                    )
                changed.append(f"master-auth-index:{signature.name}")

        changed.extend(_repair_master_authorization_triggers(conn))
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
    auth_changes = _repair_master_authorization_schema()
    with master_engine.connect() as conn:
        _validate_master_authorization_records(conn)
    require_clean_schema(
        detect_drift(
            master_engine,
            MasterBase,
            "master",
            migration_index_signatures=MASTER_AUTHORIZATION_INDEX_SIGNATURES,
        )
    )
    _master_initialised = True
    return (
        [f"drop:{c}" for c in dropped]
        + generation_changes
        + added
        + auth_changes
    )


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
