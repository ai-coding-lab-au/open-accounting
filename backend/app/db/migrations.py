"""Per-company schema migrations that reflection cannot handle.

Additive column changes are handled automatically by `schema_sync.sync_missing_columns`
(called from startup). This module covers only the things SQLite + reflection
can't do:

  - drop columns
  - rebuild a table to change CHECK constraints / FK ondelete rules
  - create partial / functional indexes
  - data backfills

Each step is idempotent: rerunning on an already-migrated DB is a no-op.

Why not Alembic: one SQLite file per company; we prefer deploy-simplicity
(zero migration files, schema is the model + this short list) over the
linear-history bookkeeping Alembic provides. Re-evaluate if any of:
  - a step needs data backfill more complex than a one-liner
  - the rebuild list grows past ~5 entries
  - we hit a bug caused by silent drift the startup check didn't catch
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Generator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .base import CompanyBase
from .errors import DataRecoveryRequiredError
from .schema_sync import (
    MIGRATION_INDEX_SIGNATURES,
    SQLiteIndexSignature,
    live_sqlite_indexes,
    model_sqlite_indexes,
    sqlite_index_matches,
    table_signature_mismatches,
)
from ..services.account_invariants import SYSTEM_ACCOUNT_SPECS

# Tables that have a supported full rebuild. Need is decided from the complete
# model/live constraint signature rather than the historical marker substring.
# Every run compares the live PK/NULL/default/FK/CHECK signature to the model.
TABLE_REBUILDS: tuple[str, ...] = ("bank_transactions",)


# (table_name, column_name) for columns that were removed from the schema.
# Applied via SQLite's ALTER TABLE ... DROP COLUMN (3.35+); SQLite shipped
# with Python 3.11 (>=3.37) supports this.
COMPANY_DB_COLUMN_DROPS: list[tuple[str, str]] = [
    ("service_agreements", "dha_charges_note"),
    ("service_agreements", "other_costs_note"),
    # Removed when SA visa items got per-item `item_type`; the SA-level
    # service_kind became redundant and is no longer in the model.
    ("outgoing_documents", "sa_service_kind"),
    # Removed when the discount feature was rolled back (audit P0).
    ("outgoing_documents", "sa_discount"),
    # Trust model removed: accounting is a single plain bank account now.
    # `kind` (trust/business/savings) is gone.
    #
    # NOTE: bank_transactions.linked_trust_entry_id is NOT dropped here. Its FK
    # targets trust_ledger_entries, which COMPANY_DB_TABLE_DROPS removes — and
    # SQLite's DROP COLUMN integrity check rejects a column whose FK points at a
    # now-missing table ("unknown column ... in foreign key definition"). It is
    # removed by the bank_transactions rebuild instead (see TABLE_REBUILDS).
    ("bank_accounts", "kind"),
]


# Tables removed from the schema entirely. Each entry includes explicit indexes
# to drop first; DROP TABLE handles table-owned indexes, but naming them keeps
# the migration robust across older SQLite files.
COMPANY_DB_TABLE_DROPS: list[tuple[str, tuple[str, ...]]] = [
    ("closed_periods", ("ix_closed_periods_period_start", "ix_closed_periods_period_end")),
    # Legacy ServiceAgreement table; the SA workflow lives entirely in
    # OutgoingDocument now. All M5 test data has been wiped (confirmed
    # empty across all company DBs), so a plain DROP is safe.
    ("service_agreements", ("uq_sa_number",)),
    # Trust ledger removed with the single-account accounting rebuild.
    ("trust_ledger_entries", ("uq_trust_earned_external_ref", "uq_trust_earned_per_sa")),
    # Partner-documents feature removed with the Receipt-only rewrite. Partner
    # docs lived in outgoing_documents (partner_ref_id → partners); that column
    # is dropped by the outgoing_documents rebuild below (step 3b), so this DROP
    # only needs FKs off (handled by run_company_migrations step 1).
    ("partners", ("ix_partners_display_name",)),
]


# Columns removed from outgoing_documents when the document model was reduced to
# Receipt-only (Service Agreement / Payment Request / outgoing-Invoice / Partner
# workflows deleted). Several are self-FKs or FK to the dropped `partners` table,
# so they can't go via plain DROP COLUMN — the outgoing_documents rebuild (step
# 3b) removes them. Presence of any one triggers the rebuild.
REMOVED_OUTDOC_COLUMNS: frozenset[str] = frozenset(
    {
        "expiration_date",
        "service_agreement_id",
        "voided_by_sa_id",
        "partner_ref_id",
        "related_doc_id",
        "instalment_index",
        "staff_member_id",
        "sa_applicants",
        "sa_visa_items",
        "sa_payment_sched",
        "gst_inclusive",
    }
)


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name = :t"),
        {"t": table},
    ).fetchone()
    return row is not None


def _table_sql(conn, table: str) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name = :t"),
        {"t": table},
    ).fetchone()
    return (row[0] if row else "") or ""


def _index_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name = :n"),
        {"n": name},
    ).fetchone()
    return row is not None


_BANK_DEDUP_INDEX = SQLiteIndexSignature(
    "uq_bank_txn_dedup",
    ("bank_account_id", "dedup_key"),
    unique=True,
    where="dedup_key IS NOT NULL",
)


def _required_unique_indexes() -> dict[str, tuple[SQLiteIndexSignature, ...]]:
    required = dict(MIGRATION_INDEX_SIGNATURES)
    required["bank_transactions"] = (_BANK_DEDUP_INDEX,)
    return required


def _table_has_rows(conn, table: str) -> bool:
    return (
        conn.execute(text(f'SELECT 1 FROM "{table}" LIMIT 1')).fetchone()
        is not None
    )


def _bank_constraint_mismatches(conn) -> list[str]:
    if not _table_exists(conn, "bank_transactions"):
        return []
    from ..models.company import BankTransaction

    return table_signature_mismatches(
        conn,
        inspect(conn),
        BankTransaction.__table__,
        include_indexes=False,
    )


def _bank_needs_rebuild(conn) -> bool:
    if not _table_exists(conn, "bank_transactions"):
        return False
    return bool(
        _bank_constraint_mismatches(conn)
        or "linked_trust_entry_id" in _existing_columns(conn, "bank_transactions")
    )


def _bank_rebuild_data_violations(conn) -> list[str]:
    """Return unsafe rows that must be resolved before a constraint rebuild."""

    table = "bank_transactions"
    if not _table_exists(conn, table):
        return []
    columns = _existing_columns(conn, table)
    required = {
        "id",
        "bank_account_id",
        "direction",
        "amount",
        "occurred_at",
        "gst_amount",
        "tax_code",
        "created_at",
    }
    missing = sorted(required - columns)
    if missing:
        return [f"missing required columns {missing!r}"]

    violations: list[str] = []
    unapplied_columns = {"unapplied_account_id", "unapplied_amount"}
    present_unapplied_columns = unapplied_columns & columns
    if present_unapplied_columns and present_unapplied_columns != unapplied_columns:
        violations.append(
            "the unapplied-cash column pair is only partially present: "
            + repr(sorted(present_unapplied_columns))
        )
    null_predicate = " OR ".join(f'"{column}" IS NULL' for column in sorted(required))
    null_count = conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {null_predicate}")
    ).scalar_one()
    if null_count:
        violations.append(f"{null_count} row(s) contain NULL in required columns")

    duplicate_ids = conn.execute(
        text(
            f"SELECT COUNT(*) FROM (SELECT id FROM {table} "
            "WHERE id IS NOT NULL GROUP BY id HAVING COUNT(*) > 1)"
        )
    ).scalar_one()
    if duplicate_ids:
        violations.append(f"{duplicate_ids} duplicated id value(s)")

    bad_amount = conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE amount <= 0")
    ).scalar_one()
    if bad_amount:
        violations.append(f"{bad_amount} row(s) have non-positive amount")
    bad_gst = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE gst_amount < 0 OR gst_amount > amount"
        )
    ).scalar_one()
    if bad_gst:
        violations.append(f"{bad_gst} row(s) have invalid GST bounds")
    if present_unapplied_columns == unapplied_columns:
        bad_unapplied = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {table} WHERE "
                "unapplied_amount < 0 OR unapplied_amount > amount OR "
                "(unapplied_amount = 0 AND unapplied_account_id IS NOT NULL) OR "
                "(unapplied_amount > 0 AND unapplied_account_id IS NULL)"
            )
        ).scalar_one()
        if bad_unapplied:
            violations.append(
                f"{bad_unapplied} row(s) have invalid unapplied-cash bounds/account"
            )

    orphan_banks = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {table} AS txn "
            "LEFT JOIN bank_accounts AS bank ON bank.id = txn.bank_account_id "
            "WHERE txn.bank_account_id IS NOT NULL AND bank.id IS NULL"
        )
    ).scalar_one()
    if orphan_banks:
        violations.append(f"{orphan_banks} row(s) reference a missing bank account")
    orphan_accounts = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {table} AS txn "
            "LEFT JOIN accounts AS account ON account.id = txn.account_id "
            "WHERE txn.account_id IS NOT NULL AND account.id IS NULL"
        )
    ).scalar_one()
    if orphan_accounts:
        violations.append(f"{orphan_accounts} row(s) reference a missing account")
    if present_unapplied_columns == unapplied_columns:
        orphan_unapplied_accounts = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {table} AS txn "
                "LEFT JOIN accounts AS account ON account.id = txn.unapplied_account_id "
                "WHERE txn.unapplied_account_id IS NOT NULL AND account.id IS NULL"
            )
        ).scalar_one()
        if orphan_unapplied_accounts:
            violations.append(
                f"{orphan_unapplied_accounts} row(s) reference a missing unapplied account"
            )
    duplicate_dedup = conn.execute(
        text(
            f"SELECT COUNT(*) FROM (SELECT bank_account_id, dedup_key FROM {table} "
            "WHERE dedup_key IS NOT NULL GROUP BY bank_account_id, dedup_key "
            "HAVING COUNT(*) > 1)"
        )
    ).scalar_one()
    if duplicate_dedup:
        violations.append(f"{duplicate_dedup} duplicated import dedup key(s)")
    return violations


def _duplicate_groups_for_index(
    conn, table: str, signature: SQLiteIndexSignature
) -> int:
    columns = ", ".join(f'"{column}"' for column in signature.columns)
    where = f" WHERE {signature.where}" if signature.where else ""
    return int(
        conn.execute(
            text(
                f'SELECT COUNT(*) FROM (SELECT {columns} FROM "{table}"'
                f"{where} GROUP BY {columns} HAVING COUNT(*) > 1)"
            )
        ).scalar_one()
    )


def _create_index_sql(table: str, signature: SQLiteIndexSignature) -> str:
    unique = "UNIQUE " if signature.unique else ""
    columns = ", ".join(f'"{column}"' for column in signature.columns)
    where = f" WHERE {signature.where}" if signature.where else ""
    return (
        f'CREATE {unique}INDEX "{signature.name}" ON "{table}" '
        f"({columns}){where}"
    )


def _repair_named_index(
    conn,
    *,
    table: str,
    signature: SQLiteIndexSignature,
) -> bool:
    """Safely create or replace one expected named index."""

    if not _table_exists(conn, table):
        return False
    columns = _existing_columns(conn, table)
    if not set(signature.columns).issubset(columns):
        return False
    if sqlite_index_matches(conn, table, signature):
        return False
    if signature.unique:
        duplicates = _duplicate_groups_for_index(conn, table, signature)
        if duplicates:
            duplicate_detail = f"found {duplicates} duplicated key group(s)"
            if table == "accounts" and signature.columns == ("code",):
                duplicate_code = conn.execute(
                    text(
                        "SELECT code FROM accounts GROUP BY code "
                        "HAVING COUNT(*) > 1 ORDER BY code LIMIT 1"
                    )
                ).scalar_one()
                duplicate_detail = f"code {duplicate_code} is not unique"
            raise DataRecoveryRequiredError(
                f"Cannot repair unique index {signature.name} on {table}: "
                f"{duplicate_detail}. A pre-repair "
                "backup was preserved; resolve the duplicates through an "
                "operator-reviewed recovery workflow."
            )
    if _index_exists(conn, signature.name):
        conn.execute(text(f'DROP INDEX "{signature.name}"'))
    conn.execute(text(_create_index_sql(table, signature)))
    if not sqlite_index_matches(conn, table, signature):
        raise RuntimeError(
            f"Index repair did not produce the required signature: "
            f"{table}.{signature.name}"
        )
    return True


def _load_company_model_tables():
    # The caller controls whether the optional documents module is registered.
    # Never mutate CompanyBase.metadata after create_all has already run.
    from ..models import company as _company_models  # noqa: F401

    return CompanyBase.metadata.tables


def _pending_index_repairs(conn) -> list[tuple[str, SQLiteIndexSignature]]:
    pending: dict[tuple[str, str], tuple[str, SQLiteIndexSignature]] = {}
    for table_name, signatures in _required_unique_indexes().items():
        if not _table_exists(conn, table_name):
            continue
        for signature in signatures:
            if not sqlite_index_matches(conn, table_name, signature):
                pending[(table_name, signature.name)] = (table_name, signature)
    for table in _load_company_model_tables().values():
        if not _table_exists(conn, table.name):
            continue
        for signature in model_sqlite_indexes(table).values():
            if not sqlite_index_matches(conn, table.name, signature):
                pending[(table.name, signature.name)] = (table.name, signature)
    return list(pending.values())


def _pending_unexpected_nonunique_indexes(conn) -> list[tuple[str, str]]:
    pending: list[tuple[str, str]] = []
    for table in _load_company_model_tables().values():
        if not _table_exists(conn, table.name):
            continue
        expected = set(model_sqlite_indexes(table))
        for name, signature in live_sqlite_indexes(conn, table.name).items():
            if name.startswith("sqlite_autoindex_") or name in expected:
                continue
            if not signature.unique:
                pending.append((table.name, name))
    return pending


def _constraint_repair_backup_reasons(
    conn, *, include_unrepaired_schema: bool = False
) -> list[str]:
    reasons: list[str] = []
    if _bank_needs_rebuild(conn):
        reasons.append("bank_transactions_constraints")
    for table, signature in _pending_index_repairs(conn):
        # Missing/wrong indexes on empty fresh tables carry no data risk. A
        # populated table receives a recovery point before any index mutation
        # or duplicate preflight failure.
        if _table_has_rows(conn, table):
            reasons.append(f"index:{table}.{signature.name}")
    for table, name in _pending_unexpected_nonunique_indexes(conn):
        if _table_has_rows(conn, table):
            reasons.append(f"drop_index:{table}.{name}")
    if include_unrepaired_schema:
        for table, problems in _unrepaired_table_signature_problems(conn):
            if problems and _table_has_rows(conn, table):
                reasons.append(f"schema:{table}")
    return reasons


def _unrepaired_table_signature_problems(conn) -> list[tuple[str, list[str]]]:
    """Constraint drift that targeted migrations do not safely auto-rebuild."""

    inspector = inspect(conn)
    problems: list[tuple[str, list[str]]] = []
    for table in _load_company_model_tables().values():
        if not _table_exists(conn, table.name):
            continue
        mismatches = table_signature_mismatches(conn, inspector, table)
        if mismatches:
            problems.append((table.name, mismatches))
    return problems


def _require_no_unrepaired_table_signature_drift(conn) -> None:
    problems = _unrepaired_table_signature_problems(conn)
    if not problems:
        return
    details = "; ".join(
        f"{table}: {', '.join(mismatches[:3])}"
        for table, mismatches in problems[:5]
    )
    raise DataRecoveryRequiredError(
        "Company schema still has unsafe constraint drift after targeted "
        f"repairs ({details}). A pre-repair backup was preserved for any "
        "populated affected table. Startup refused to apply data backfills or "
        "serve writes; use an explicit table-rebuild recovery workflow."
    )


def _system_account_reconciliation_plan(conn) -> list[dict]:
    """Describe canonical account rows that startup must create or repair.

    Existing names, GST display flags, hierarchy and descriptions are not part
    of the runtime identity: posting and reporting resolve these accounts by
    code. Only code presence, type and active state are reconciled.
    """
    if not _table_exists(conn, "accounts"):
        raise RuntimeError("Cannot reconcile system accounts: accounts table is missing")

    required_columns = {"id", "code", "name", "type", "is_gst", "active"}
    missing_columns = required_columns - _existing_columns(conn, "accounts")
    if missing_columns:
        raise RuntimeError(
            "Cannot reconcile system accounts: accounts table is missing columns "
            + ", ".join(sorted(missing_columns))
        )

    rows_by_code: dict[str, list[dict]] = {
        code: [] for code in SYSTEM_ACCOUNT_SPECS
    }
    rows = conn.execute(
        text("SELECT id, code, name, type, is_gst, active FROM accounts")
    ).mappings()
    for row in rows:
        code = row["code"]
        if code in rows_by_code:
            rows_by_code[code].append(dict(row))

    plan: list[dict] = []
    for code, spec in SYSTEM_ACCOUNT_SPECS.items():
        matches = rows_by_code[code]
        if len(matches) > 1:
            raise RuntimeError(
                f"System account code {code} is not unique ({len(matches)} rows); "
                "reconciliation aborted without changing the database"
            )
        if not matches:
            plan.append({"code": code, "action": "create", "row": None})
            continue

        row = matches[0]
        wrong_type = row["type"] != spec.type.value
        inactive = not bool(row["active"])
        if wrong_type or inactive:
            plan.append(
                {
                    "code": code,
                    "action": "repair",
                    "row": row,
                    "wrong_type": wrong_type,
                    "inactive": inactive,
                }
            )
    return plan


def _destructive_backup_reasons(
    conn,
    *,
    include_system_accounts: bool = False,
    include_unrepaired_schema: bool = False,
) -> list[str]:
    """Describe pending destructive work that warrants an online backup.

    The outgoing rebuild can filter rows even when the retired feature tables
    are absent.  Conversely, an older database may already have the lean
    outgoing shape while still carrying populated tables that step 1 drops.
    Both cases, plus an explicitly requested system-account identity repair,
    need the same pre-migration recovery point.
    """
    reasons: list[str] = []
    if _table_exists(conn, "outgoing_documents") and (
        REMOVED_OUTDOC_COLUMNS & _existing_columns(conn, "outgoing_documents")
    ):
        reasons.append("outgoing_documents")

    for table, _indexes in COMPANY_DB_TABLE_DROPS:
        if not _table_exists(conn, table):
            continue
        has_rows = conn.execute(
            text(f'SELECT 1 FROM "{table}" LIMIT 1')
        ).fetchone()
        if has_rows is not None:
            reasons.append(f"drop_table:{table}")
    if include_system_accounts and _system_account_reconciliation_plan(conn):
        reasons.append("system_accounts")
    if _payment_reconciliation_counts(conn):
        reasons.append("payment_reconciliation")
    reasons.extend(
        _constraint_repair_backup_reasons(
            conn,
            include_unrepaired_schema=include_unrepaired_schema,
        )
    )
    # Keep the backup log stable if one table needs more than one index repair.
    return list(dict.fromkeys(reasons))


def _create_destructive_migration_backup(
    engine: Engine,
    *,
    include_system_accounts: bool = False,
    include_unrepaired_schema: bool = False,
) -> tuple[Path | None, list[str]]:
    """Create a consistent backup before destructive work or identity repair.

    The SQLite online-backup API includes committed WAL contents and is safe while
    other readers exist.  In-memory databases have no durable file to preserve,
    so test-only/in-memory engines intentionally return ``None``.

    A new timestamped file is allocated with O_EXCL for every attempted legacy
    migration attempt.  A failed attempt therefore never overwrites the recovery
    point made by an earlier attempt.
    """
    if engine.dialect.name != "sqlite":
        return None, []
    database = engine.url.database
    if not database or database == ":memory:":
        return None, []

    db_path = Path(database).resolve()
    if not db_path.is_file():
        return None, []

    with engine.connect() as conn:
        reasons = _destructive_backup_reasons(
            conn,
            include_system_accounts=include_system_accounts,
            include_unrepaired_schema=include_unrepaired_schema,
        )
        if not reasons:
            return None, []

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path: Path | None = None
        for suffix in range(1000):
            suffix_text = "" if suffix == 0 else f"-{suffix}"
            candidate = db_path.with_name(
                f"{db_path.name}.pre-destructive-migration-"
                f"{stamp}{suffix_text}.bak"
            )
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            else:
                os.close(fd)
                backup_path = candidate
                break
        if backup_path is None:
            raise RuntimeError("Could not allocate a unique migration backup")

        try:
            raw_source = conn.connection.driver_connection
            with sqlite3.connect(backup_path) as destination:
                raw_source.backup(destination)
                result = destination.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise RuntimeError(
                        "Migration backup failed SQLite integrity_check"
                    )
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise

    return backup_path, reasons


def _assert_sqlite_integrity(conn, *, context: str) -> None:
    """Fail the migration before commit if SQLite or FK integrity is broken."""
    integrity_rows = conn.execute(text("PRAGMA integrity_check")).fetchall()
    if not integrity_rows or any(row[0] != "ok" for row in integrity_rows):
        raise DataRecoveryRequiredError(
            f"SQLite integrity_check failed after {context}. Restore from a "
            "verified backup or use an operator-reviewed recovery workflow."
        )

    try:
        fk_violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    except Exception as exc:
        raise DataRecoveryRequiredError(
            f"SQLite foreign_key_check could not validate the schema after "
            f"{context}: {exc}. A referenced parent key or FK declaration is "
            "structurally invalid; startup refused to continue."
        ) from exc
    if fk_violations:
        raise DataRecoveryRequiredError(
            f"SQLite foreign_key_check found {len(fk_violations)} violation(s) "
            f"after {context}. Restore or repair the orphaned rows before retrying."
        )


@contextmanager
def _migration_transaction(engine: Engine) -> Generator:
    """Run migration work transactionally and never poison a pooled connection.

    SQLite ignores ``PRAGMA foreign_keys = ON`` while a transaction is active.
    Reassert it after both commit and rollback, in AUTOCOMMIT mode, before the
    connection can return to the pool.
    """
    conn = engine.connect()
    try:
        try:
            with conn.begin():
                yield conn
        finally:
            # Restore the exact DBAPI connection that ran the migration before
            # it can return to the pool.  Opening a second Engine connection is
            # insufficient when the pool has more than one live checkout.
            raw = conn.connection.driver_connection
            # A failed SQLite DDL sequence can leave the DBAPI connection in a
            # transaction even after SQLAlchemy unwinds its context.  PRAGMA
            # foreign_keys=ON is ignored in that state, so clear it explicitly
            # before returning the connection to the pool.
            if getattr(raw, "in_transaction", False):
                raw.rollback()
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError(
                    "Could not restore SQLite foreign-key enforcement after migration"
                )
    finally:
        conn.close()


def _apply_system_account_reconciliation(conn) -> list[str]:
    """Apply one freshly-computed reconciliation plan inside one transaction."""
    applied: list[str] = []
    for item in _system_account_reconciliation_plan(conn):
        code = item["code"]
        spec = SYSTEM_ACCOUNT_SPECS[code]
        if item["action"] == "create":
            conn.execute(
                text(
                    "INSERT INTO accounts "
                    "(code, name, type, parent_id, is_gst, active, description) "
                    "VALUES (:code, :name, :type, NULL, :is_gst, 1, NULL)"
                ),
                {
                    "code": spec.code,
                    "name": spec.default_name,
                    "type": spec.type.value,
                    "is_gst": spec.is_gst,
                },
            )
            applied.append(f"reconcile:system_account:{code}:created")
            continue

        row = item["row"]
        result = conn.execute(
            text(
                "UPDATE accounts SET type = :type, active = 1 "
                "WHERE id = :id AND code = :code"
            ),
            {"type": spec.type.value, "id": row["id"], "code": code},
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"System account {code} changed during reconciliation; "
                "transaction rolled back"
            )
        repairs: list[str] = []
        if item["wrong_type"]:
            repairs.append(f"type={spec.type.value}")
        if item["inactive"]:
            repairs.append("active")
        applied.append(
            f"reconcile:system_account:{code}:" + "+".join(repairs)
        )

    residual = _system_account_reconciliation_plan(conn)
    if residual:
        codes = ", ".join(item["code"] for item in residual)
        raise RuntimeError(
            f"System account reconciliation did not converge for: {codes}"
        )
    _assert_sqlite_integrity(conn, context="system account reconciliation")
    return applied


def reconcile_system_accounts(engine: Engine) -> list[str]:
    """Backup and atomically repair canonical account identity at startup.

    The initial read is intentionally non-mutating. Duplicate legacy codes or
    malformed schemas fail before a backup/transaction; all database errors
    propagate. A durable SQLite database is copied with the online-backup API
    before any required row is inserted or updated.
    """
    with engine.connect() as conn:
        if not _system_account_reconciliation_plan(conn):
            return []

    backup_path, _reasons = _create_destructive_migration_backup(
        engine, include_system_accounts=True
    )
    applied = ["backup:system_accounts"] if backup_path is not None else []
    with _migration_transaction(engine) as conn:
        applied.extend(_apply_system_account_reconciliation(conn))
    return applied


def _autoincrement_high_water(conn, table: str) -> int:
    """Return the highest live or historically allocated ID for ``table``."""
    max_id = conn.execute(
        text(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
    ).scalar_one()
    old_sequence = 0
    if _table_exists(conn, "sqlite_sequence"):
        old_sequence = (
            conn.execute(
                text("SELECT seq FROM sqlite_sequence WHERE name = :table"),
                {"table": table},
            ).scalar_one_or_none()
            or 0
        )
    return max(int(max_id), int(old_sequence))


def _restore_autoincrement_high_water(
    conn,
    *,
    table: str,
    helper_table: str,
    high_water_id: int,
) -> None:
    """Keep deleted legacy IDs from being reused after a table rebuild."""
    conn.execute(
        text(
            "DELETE FROM sqlite_sequence "
            "WHERE name IN (:table, :helper_table)"
        ),
        {"table": table, "helper_table": helper_table},
    )
    conn.execute(
        text("INSERT INTO sqlite_sequence(name, seq) VALUES (:table, :seq)"),
        {"table": table, "seq": high_water_id},
    )


def _indexes_referencing_column(conn, table: str, column: str) -> list[str]:
    """Names of indexes on `table` that reference `column`.

    SQLite's ALTER TABLE ... DROP COLUMN fails if any index still references
    the column being dropped (e.g. the auto-created ix_bank_accounts_kind on
    an indexed column). Caller drops these first. Uses PRAGMA index_info so
    it matches the actual indexed columns, not a substring of the DDL.
    """
    rows = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name = :t AND name NOT LIKE 'sqlite_%'"
        ),
        {"t": table},
    ).fetchall()
    matching: list[str] = []
    for (index_name,) in rows:
        cols = conn.execute(
            text(f'PRAGMA index_info("{index_name}")')
        ).fetchall()
        if any(c[2] == column for c in cols):
            matching.append(index_name)
    return matching


def _rebuild_bank_transactions(conn) -> None:
    """Rebuild bank_transactions table to apply CHECK constraints + FK ondelete.

    SQLite can't ALTER CHECK / FK rules in place. The supported pattern is:
      1. CREATE TABLE new_X with the desired schema
      2. INSERT new_X SELECT * FROM X
      3. DROP TABLE X
      4. ALTER TABLE new_X RENAME TO X
      5. recreate indexes

    foreign_keys must be OFF during this dance so dropping the old table
    doesn't cascade-trash referencing rows.

    The DDL below must mirror the LIVE model (models.company.BankTransaction)
    — a stale snapshot here silently drops newer columns and their data.
    `sync_missing_columns` runs first in `init_company_db`. The direct legacy
    runner also accepts databases from before the unapplied-cash columns; their
    unambiguous historical values (NULL account and zero amount) are supplied
    by the INSERT…SELECT below.
    """
    high_water_id = _autoincrement_high_water(conn, "bank_transactions")
    old_columns = _existing_columns(conn, "bank_transactions")
    unapplied_account_source = (
        "unapplied_account_id" if "unapplied_account_id" in old_columns else "NULL"
    )
    unapplied_amount_source = (
        "unapplied_amount" if "unapplied_amount" in old_columns else "0"
    )

    # Snapshot original index DDL so we can recreate post-rename.
    index_rows = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='bank_transactions' "
            "AND sql IS NOT NULL"
        )
    ).fetchall()
    index_sqls = [r[0] for r in index_rows]

    # Toggle FK off, rebuild, re-enable.
    conn.execute(text("PRAGMA foreign_keys = OFF"))
    if conn.execute(text("PRAGMA foreign_keys")).scalar_one() != 0:
        raise RuntimeError(
            "bank_transactions rebuild requires foreign_keys=OFF before its transaction"
        )

    savepoint = "p1_bank_transactions_rebuild"
    conn.exec_driver_sql(f"SAVEPOINT {savepoint}")
    try:
        # A failed older attempt can leave the fixed-name helper behind.  Drop
        # it inside the savepoint so both a clean retry and rollback are safe.
        conn.execute(text("DROP TABLE IF EXISTS new_bank_transactions"))
        conn.execute(
            text(
                """
                CREATE TABLE new_bank_transactions (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE RESTRICT,
                    direction VARCHAR(3) NOT NULL,
                    amount NUMERIC(16, 2) NOT NULL,
                    occurred_at DATE NOT NULL,
                    memo VARCHAR(500),
                    counter_party_name VARCHAR(200),
                    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                    gst_amount NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    tax_code VARCHAR(20) NOT NULL DEFAULT 'standard',
                    unapplied_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
                    unapplied_amount NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    dedup_key VARCHAR(64),
                    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                    CONSTRAINT ck_bank_txn_amount_positive CHECK (amount > 0),
                    CONSTRAINT ck_bank_txn_gst_nonneg CHECK (gst_amount >= 0),
                    CONSTRAINT ck_bank_txn_gst_within CHECK (gst_amount <= amount),
                    CONSTRAINT ck_bank_txn_unapplied_within CHECK (
                        unapplied_amount >= 0 AND unapplied_amount <= amount
                    ),
                    CONSTRAINT ck_bank_txn_unapplied_account CHECK (
                        (unapplied_amount = 0 AND unapplied_account_id IS NULL) OR
                        (unapplied_amount > 0 AND unapplied_account_id IS NOT NULL)
                    )
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                INSERT INTO new_bank_transactions (
                    id, bank_account_id, direction, amount, occurred_at, memo,
                    counter_party_name, account_id, gst_amount, tax_code,
                    unapplied_account_id, unapplied_amount, dedup_key, created_at
                )
                SELECT
                    id, bank_account_id, direction, amount, occurred_at, memo,
                    counter_party_name, account_id, gst_amount, tax_code,
                    {unapplied_account_source}, {unapplied_amount_source},
                    dedup_key, created_at
                FROM bank_transactions
                """
            )
        )
        conn.execute(text("DROP TABLE bank_transactions"))
        conn.execute(text("ALTER TABLE new_bank_transactions RENAME TO bank_transactions"))
        _restore_autoincrement_high_water(
            conn,
            table="bank_transactions",
            helper_table="new_bank_transactions",
            high_water_id=high_water_id,
        )
        for sql in index_sqls:
            # Skip indexes on columns the rebuild dropped (e.g. the auto index
            # on the removed linked_trust_entry_id) — recreating them would
            # fail with "no such column". Step 4 recreates the real ones.
            if "linked_trust_entry_id" in sql:
                continue
            conn.execute(text(sql))
        _assert_sqlite_integrity(conn, context="bank_transactions rebuild")
    except Exception:
        conn.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
    finally:
        conn.execute(text("PRAGMA foreign_keys = ON"))


def _rebuild_outgoing_documents(conn) -> None:
    """Rebuild outgoing_documents to the lean Receipt-only schema.

    The Receipt-only rewrite removed the SA/PR/Invoice/Partner columns
    (REMOVED_OUTDOC_COLUMNS). Several can't go via ALTER TABLE ... DROP COLUMN:
    they are self-FKs (service_agreement_id / voided_by_sa_id / related_doc_id →
    outgoing_documents.id) or an FK to the already-dropped `partners` table, and
    SQLite's DROP COLUMN integrity check rejects those. The supported pattern is
    the same new-table → copy → drop → rename dance used for bank_transactions.

    Only receipts survive (WHERE doc_type='receipt'); legacy SA/PR/Invoice rows
    and partner rows (partner_ref_id NOT NULL) are purged. All such data is
    test-only, per the removal decision.

    The DDL below must mirror the LIVE model (models.outgoing.OutgoingDocument).
    """
    old_cols = _existing_columns(conn, "outgoing_documents")

    high_water_id = _autoincrement_high_water(conn, "outgoing_documents")

    index_rows = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='outgoing_documents' "
            "AND sql IS NOT NULL"
        )
    ).fetchall()
    index_sqls = [r[0] for r in index_rows]

    # Partner rows are purged too when the column is still present.
    partner_clause = (
        " AND partner_ref_id IS NULL" if "partner_ref_id" in old_cols else ""
    )

    conn.execute(text("PRAGMA foreign_keys = OFF"))
    if conn.execute(text("PRAGMA foreign_keys")).scalar_one() != 0:
        # Continuing with FK enforcement enabled would make DROP TABLE cascade
        # away even the retained receipt lines.  Stop before any destructive DDL.
        raise RuntimeError(
            "outgoing_documents rebuild requires foreign_keys=OFF before its transaction"
        )

    savepoint = "p0_outgoing_documents_rebuild"
    conn.exec_driver_sql(f"SAVEPOINT {savepoint}")
    try:
        # A pre-fix crash could leave this fixed-name helper behind.  Always
        # remove it before CREATE so the next startup can recover.
        conn.execute(text("DROP TABLE IF EXISTS new_outgoing_documents"))
        conn.execute(
            text(
                """
                CREATE TABLE new_outgoing_documents (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    doc_type VARCHAR(20) NOT NULL,
                    doc_number VARCHAR(40) NOT NULL,
                    issue_date DATE NOT NULL,
                    customer_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                    client_ref_id INTEGER REFERENCES clients(id) ON DELETE RESTRICT,
                    customer_name VARCHAR(200) NOT NULL,
                    customer_address VARCHAR(500),
                    customer_abn VARCHAR(20),
                    customer_email VARCHAR(200),
                    customer_phone VARCHAR(50),
                    currency VARCHAR(3) NOT NULL DEFAULT 'AUD',
                    subtotal NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    gst_amount NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    total NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    status VARCHAR(20) NOT NULL DEFAULT 'draft',
                    paid_date DATE,
                    payment_method VARCHAR(100),
                    notes VARCHAR(1000),
                    pdf_rel_path VARCHAR(500),
                    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                    updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                    CONSTRAINT uq_outdoc_type_number UNIQUE (doc_type, doc_number)
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO new_outgoing_documents ("
                "id, doc_type, doc_number, issue_date, customer_id, client_ref_id, "
                "customer_name, customer_address, customer_abn, customer_email, "
                "customer_phone, currency, subtotal, gst_amount, total, status, "
                "paid_date, payment_method, notes, pdf_rel_path, created_at, updated_at"
                ") SELECT "
                "id, doc_type, doc_number, issue_date, customer_id, client_ref_id, "
                "customer_name, customer_address, customer_abn, customer_email, "
                "customer_phone, currency, subtotal, gst_amount, total, status, "
                "paid_date, payment_method, notes, pdf_rel_path, created_at, updated_at "
                "FROM outgoing_documents "
                f"WHERE doc_type = 'receipt'{partner_clause}"
            )
        )

        # FK enforcement is deliberately disabled for the parent-table dance,
        # so ON DELETE CASCADE cannot protect us here.  Remove every child whose
        # parent was not copied (and any pre-existing orphan) before IDs can ever
        # be reused by a future receipt.
        if _table_exists(conn, "outgoing_document_lines"):
            line_cols = _existing_columns(conn, "outgoing_document_lines")
            if "document_id" in line_cols:
                conn.execute(
                    text(
                        "DELETE FROM outgoing_document_lines AS line "
                        "WHERE NOT EXISTS ("
                        "SELECT 1 FROM new_outgoing_documents AS doc "
                        "WHERE doc.id = line.document_id"
                        ")"
                    )
                )
        conn.execute(text("DROP TABLE outgoing_documents"))
        conn.execute(
            text("ALTER TABLE new_outgoing_documents RENAME TO outgoing_documents")
        )

        # CREATE/INSERT seeds AUTOINCREMENT from retained rows only.  Restore the
        # old high-water mark (including a sqlite_sequence value above MAX(id))
        # so a purged legacy ID can never be assigned to a new receipt.
        _restore_autoincrement_high_water(
            conn,
            table="outgoing_documents",
            helper_table="new_outgoing_documents",
            high_water_id=high_water_id,
        )
        for sql in index_sqls:
            # Skip indexes on columns the rebuild dropped (service_agreement_id,
            # voided_by_sa_id, partner_ref_id, staff_member_id, …); recreating
            # them would fail with "no such column".
            if any(col in sql for col in REMOVED_OUTDOC_COLUMNS):
                continue
            conn.execute(text(sql))
        _assert_sqlite_integrity(conn, context="outgoing_documents rebuild")
    except Exception:
        conn.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
    finally:
        conn.execute(text("PRAGMA foreign_keys = ON"))


def _ensure_bank_txn_dedup_unique_index(conn) -> None:
    """Partial UNIQUE index for statement-import deduplication."""
    _repair_named_index(
        conn,
        table="bank_transactions",
        signature=_BANK_DEDUP_INDEX,
    )


def _ensure_invoice_source_unique_index(conn) -> None:
    """Partial UNIQUE index that catches re-imports of the same source row.

    Same Excel row / same PDF file should produce the same source_ref
    when re-uploaded; this index makes the second import fail at the
    DB level instead of silently creating a duplicate Invoice.

    Scoped by `source` so identical source_ref strings across different
    import paths don't collide (e.g. Excel "row 1" doesn't conflict
    with PDF "row 1" — they're in different sources).
    """
    [signature] = MIGRATION_INDEX_SIGNATURES["invoices"]
    _repair_named_index(
        conn,
        table="invoices",
        signature=signature,
    )


def _ensure_journal_source_unique_indexes(conn) -> None:
    """Unique source provenance for generated journal entries."""
    for signature in MIGRATION_INDEX_SIGNATURES["journal_entries"]:
        _repair_named_index(
            conn,
            table="journal_entries",
            signature=signature,
        )


def _payment_reconciliation_counts(conn) -> dict[str, int]:
    """Pending fail-safe repairs for pre-allocation-ledger payment metadata."""

    required = {
        "invoices",
        "journal_entries",
        "bank_transactions",
        "accounts",
        "invoice_payment_allocations",
        "payment_reconciliation_events",
    }
    if any(not _table_exists(conn, table) for table in required):
        return {}

    invalid_allocation_txns = int(
        conn.execute(
            text(
                "SELECT COUNT(DISTINCT a.bank_transaction_id) "
                "FROM invoice_payment_allocations AS a "
                "JOIN invoices AS i ON i.id = a.invoice_id "
                "LEFT JOIN journal_entries AS j ON j.source_id = i.id AND "
                "((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
                " (i.direction = 'AP' AND j.source_type = 'invoice_ap')) "
                "WHERE j.id IS NULL"
            )
        ).scalar_one()
    )
    unallocated_controls = int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM bank_transactions AS t "
                "JOIN accounts AS a ON a.id = t.account_id "
                "WHERE ((a.code = '1100' AND t.direction = 'in') OR "
                "       (a.code = '2000' AND t.direction = 'out')) "
                "AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
                "                WHERE p.bank_transaction_id = t.id)"
            )
        ).scalar_one()
    )
    stale_invoices = int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM invoices AS i WHERE "
                "((COALESCE(i.paid_amount, 0) > 0 OR i.status IN ('partial', 'paid')) "
                " AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
                "                 WHERE p.invoice_id = i.id)) "
                "OR (i.status NOT IN ('draft', 'void') AND NOT EXISTS ("
                "    SELECT 1 FROM journal_entries AS j WHERE j.source_id = i.id "
                "    AND ((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
                "         (i.direction = 'AP' AND j.source_type = 'invoice_ap'))))"
            )
        ).scalar_one()
    )
    counts = {
        "invalid_allocation_txns": invalid_allocation_txns,
        "unallocated_controls": unallocated_controls,
        "stale_invoices": stale_invoices,
    }
    return {name: count for name, count in counts.items() if count}


def _record_payment_reconciliation_event(
    conn,
    *,
    event_key: str,
    event_type: str,
    invoice_id: int | None = None,
    bank_transaction_id: int | None = None,
    details: dict,
) -> None:
    conn.execute(
        text(
            "INSERT OR IGNORE INTO payment_reconciliation_events "
            "(event_key, event_type, invoice_id, bank_transaction_id, details_json) "
            "VALUES (:event_key, :event_type, :invoice_id, :bank_transaction_id, "
            ":details_json)"
        ),
        {
            "event_key": event_key,
            "event_type": event_type,
            "invoice_id": invoice_id,
            "bank_transaction_id": bank_transaction_id,
            "details_json": json.dumps(
                details, ensure_ascii=False, sort_keys=True, default=str
            ),
        },
    )


def _reconcile_legacy_payment_metadata(conn) -> dict[str, int]:
    """Preserve evidence, then return unverifiable legacy state to workflow."""

    counts = _payment_reconciliation_counts(conn)
    if not counts:
        return {}

    invalid_txn_rows = conn.execute(
        text(
            "SELECT DISTINCT t.id, t.account_id, t.gst_amount, t.tax_code "
            "FROM bank_transactions AS t "
            "JOIN invoice_payment_allocations AS a ON a.bank_transaction_id = t.id "
            "JOIN invoices AS i ON i.id = a.invoice_id "
            "LEFT JOIN journal_entries AS j ON j.source_id = i.id AND "
            "((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
            " (i.direction = 'AP' AND j.source_type = 'invoice_ap')) "
            "WHERE j.id IS NULL"
        )
    ).mappings().all()
    for row in invalid_txn_rows:
        allocation_rows = conn.execute(
            text(
                "SELECT invoice_id, amount, gst_amount "
                "FROM invoice_payment_allocations WHERE bank_transaction_id = :id "
                "ORDER BY id"
            ),
            {"id": row["id"]},
        ).mappings().all()
        _record_payment_reconciliation_event(
            conn,
            event_key=f"legacy-invalid-allocation-txn-{row['id']}",
            event_type="invalid_allocation_without_invoice_journal",
            bank_transaction_id=row["id"],
            details={
                "account_id": row["account_id"],
                "gst_amount": row["gst_amount"],
                "tax_code": row["tax_code"],
                "allocations": [dict(item) for item in allocation_rows],
            },
        )
        conn.execute(
            text(
                "DELETE FROM invoice_payment_allocations "
                "WHERE bank_transaction_id = :id"
            ),
            {"id": row["id"]},
        )
        conn.execute(
            text(
                "UPDATE bank_transactions SET account_id = NULL, gst_amount = 0, "
                "tax_code = 'none', unapplied_account_id = NULL, "
                "unapplied_amount = 0 WHERE id = :id"
            ),
            {"id": row["id"]},
        )

    control_rows = conn.execute(
        text(
            "SELECT t.id, t.account_id, t.gst_amount, t.tax_code, a.code "
            "FROM bank_transactions AS t JOIN accounts AS a ON a.id = t.account_id "
            "WHERE ((a.code = '1100' AND t.direction = 'in') OR "
            "       (a.code = '2000' AND t.direction = 'out')) "
            "AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
            "                WHERE p.bank_transaction_id = t.id)"
        )
    ).mappings().all()
    for row in control_rows:
        _record_payment_reconciliation_event(
            conn,
            event_key=f"legacy-unallocated-control-txn-{row['id']}",
            event_type="unallocated_legacy_control_transaction",
            bank_transaction_id=row["id"],
            details=dict(row),
        )
        conn.execute(
            text(
                "UPDATE bank_transactions SET account_id = NULL, gst_amount = 0, "
                "tax_code = 'none', unapplied_account_id = NULL, "
                "unapplied_amount = 0 WHERE id = :id"
            ),
            {"id": row["id"]},
        )

    invoice_rows = conn.execute(
        text(
            "SELECT i.id, i.status, i.paid_amount, i.paid_date, i.authorised_at, "
            "EXISTS(SELECT 1 FROM journal_entries AS j WHERE j.source_id = i.id "
            " AND ((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
            "      (i.direction = 'AP' AND j.source_type = 'invoice_ap'))) AS has_journal "
            "FROM invoices AS i WHERE "
            "((COALESCE(i.paid_amount, 0) > 0 OR i.status IN ('partial', 'paid')) "
            " AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
            "                 WHERE p.invoice_id = i.id)) "
            "OR (i.status NOT IN ('draft', 'void') AND NOT EXISTS ("
            "    SELECT 1 FROM journal_entries AS j WHERE j.source_id = i.id "
            "    AND ((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
            "         (i.direction = 'AP' AND j.source_type = 'invoice_ap'))))"
        )
    ).mappings().all()
    for row in invoice_rows:
        _record_payment_reconciliation_event(
            conn,
            event_key=f"legacy-stale-invoice-{row['id']}",
            event_type="unverifiable_legacy_invoice_payment_state",
            invoice_id=row["id"],
            details=dict(row),
        )
        target_status = "authorised" if row["has_journal"] else "draft"
        conn.execute(
            text(
                "UPDATE invoices SET status = :status, paid_amount = 0, "
                "paid_date = NULL, authorised_at = CASE WHEN :has_journal = 1 "
                "THEN authorised_at ELSE NULL END WHERE id = :id"
            ),
            {
                "id": row["id"],
                "status": target_status,
                "has_journal": int(bool(row["has_journal"])),
            },
        )
    return {
        "invalid_allocation_txns": len(invalid_txn_rows),
        "unallocated_controls": len(control_rows),
        "stale_invoices": len(invoice_rows),
    }


def _backfill_invoice_line_tax_codes(conn) -> int:
    if not _table_exists(conn, "invoice_lines"):
        return 0
    columns = _existing_columns(conn, "invoice_lines")
    if "tax_code" not in columns:
        return 0
    result = conn.execute(
        text(
            "UPDATE invoice_lines SET tax_code = CASE "
            "WHEN COALESCE(line_gst, 0) > 0 THEN 'standard' ELSE 'gst_free' END "
            "WHERE tax_code IS NULL OR tax_code = '' OR "
            "(tax_code = 'gst_free' AND COALESCE(line_gst, 0) > 0)"
        )
    )
    return int(result.rowcount or 0)


def _backfill_journal_source_type(conn) -> int:
    """Existing journal rows predate provenance and are manual entries."""
    if not _table_exists(conn, "journal_entries"):
        return 0
    if "source_type" not in _existing_columns(conn, "journal_entries"):
        return 0
    result = conn.execute(
        text(
            "UPDATE journal_entries "
            "SET source_type = 'manual' "
            "WHERE source_type IS NULL"
        )
    )
    return result.rowcount or 0


def _backfill_invoice_authorised_at(conn) -> int:
    """Historical unpaid/partial/paid invoices were already operator-approved."""
    if not _table_exists(conn, "invoices"):
        return 0
    cols = _existing_columns(conn, "invoices")
    if "authorised_at" not in cols or "status" not in cols:
        return 0
    fallback = "created_at" if "created_at" in cols else "issue_date"
    result = conn.execute(
        text(
            f"UPDATE invoices "
            f"SET authorised_at = COALESCE(authorised_at, {fallback}, issue_date) "
            f"WHERE authorised_at IS NULL "
            f"AND status IN ('unpaid', 'partial', 'paid', 'authorised')"
        )
    )
    return result.rowcount or 0


def _backfill_doc_number_version_suffix(conn) -> int:
    """Rename `XX-YYYY-NNNN` documents to `XX-YYYY-NNNN-1`.

    The numbering scheme now stamps a version suffix on every doc; the
    original issue is `-1` and each void+reissue bumps the suffix.
    Existing data pre-dates that — bring it up to date in one shot.

    Returns the number of rows renamed. Idempotent: a second run finds
    nothing matching the regex (every doc already has a `-N` suffix).
    """
    if not _table_exists(conn, "outgoing_documents"):
        return 0

    # SQLite's REGEXP is optional; use LIKE + a SQL-side check that the
    # number does NOT already contain a fourth `-` segment.
    # Pattern of unversioned numbers: <prefix>-<year>-<serial> where there
    # are exactly two `-`. We detect by counting `-` in doc_number.
    rows = conn.execute(
        text(
            "SELECT id, doc_number FROM outgoing_documents "
            "WHERE doc_number IS NOT NULL"
        )
    ).fetchall()
    renamed = 0
    for row in rows:
        doc_id, number = row[0], row[1]
        if not isinstance(number, str):
            continue
        # New shape: at least three '-' (PREFIX-YYYY-NNNN-V).
        # Old shape: exactly two '-' (PREFIX-YYYY-NNNN).
        if number.count("-") != 2:
            continue
        new_number = f"{number}-1"
        # Guard against a hypothetical collision with a row that's already -1.
        existing = conn.execute(
            text("SELECT 1 FROM outgoing_documents WHERE doc_number = :n"),
            {"n": new_number},
        ).fetchone()
        if existing is not None:
            # Collision is theoretically impossible given the old scheme
            # never wrote `-1`, but if it ever happens, skip rather than crash.
            continue
        conn.execute(
            text("UPDATE outgoing_documents SET doc_number = :new WHERE id = :id"),
            {"new": new_number, "id": doc_id},
        )
        renamed += 1
    return renamed


def run_company_migrations(
    engine: Engine, *, enforce_schema_gate: bool = False
) -> list[str]:
    """Apply pending schema migrations on a per-company DB.

    Returns the list of step names that ran this call. Idempotent.
    """
    applied: list[str] = []
    backup_path, backup_reasons = _create_destructive_migration_backup(
        engine,
        include_unrepaired_schema=enforce_schema_gate,
    )
    if backup_path is not None:
        applied.extend(f"backup:{reason}" for reason in backup_reasons)
    with _migration_transaction(engine) as conn:
        # 1. Table drops (for tables removed from the schema). FKs are
        # toggled off so a referenced table can be dropped even when some
        # *other* table still has the dangling FK column (cleaned up in
        # step 2). All data has already been wiped for these legacy tables.
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        try:
            for table, indexes in COMPANY_DB_TABLE_DROPS:
                existed = _table_exists(conn, table)
                dropped_indexes = False
                for index in indexes:
                    if _index_exists(conn, index):
                        conn.execute(text(f'DROP INDEX IF EXISTS "{index}"'))
                        dropped_indexes = True
                if existed:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
                    applied.append(f"drop_table:{table}")
                elif dropped_indexes:
                    applied.append(f"drop_indexes:{table}")
        finally:
            conn.execute(text("PRAGMA foreign_keys = ON"))

        # 2b. Column drops (for columns removed from the schema).
        # (Additive column changes are handled by schema_sync.sync_missing_columns.)
        #
        # FK pragma off: the column being dropped may itself be a FK
        # source whose target table was dropped in step 1 (e.g.
        # bank_transactions.linked_trust_entry_id → the dropped
        # trust_ledger_entries table). SQLite's ALTER TABLE … DROP COLUMN
        # runs an integrity check that rejects "FK references missing
        # table" otherwise.
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        try:
            for table, column in COMPANY_DB_COLUMN_DROPS:
                if not _table_exists(conn, table):
                    continue
                if column not in _existing_columns(conn, table):
                    continue
                # SQLite's DROP COLUMN runs an integrity check that fails if any
                # index still references the column (e.g. ix_bank_accounts_kind
                # on the indexed `kind` column). Drop those indexes first.
                for index_name in _indexes_referencing_column(conn, table, column):
                    conn.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
                    applied.append(f"drop_index:{index_name}")
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN "{column}"'))
                applied.append(f"drop_column:{table}.{column}")
        finally:
            conn.execute(text("PRAGMA foreign_keys = ON"))

        # 3. Table rebuilds for constraint/FK changes.
        #
        # bank_transactions is rebuilt to (a) apply the full model constraint
        # signature and (b) drop the legacy linked_trust_entry_id column. The
        # column can't go via plain DROP COLUMN: its FK targets the
        # already-dropped trust_ledger_entries table, which SQLite's DROP COLUMN
        # integrity check rejects. The rebuild (new table → copy → drop → rename)
        # sidesteps that. Full signature comparison also catches partially
        # applied legacy rebuilds, including missing GST checks or FKs.
        for table in TABLE_REBUILDS:
            if not _table_exists(conn, table):
                continue
            if table != "bank_transactions" or not _bank_needs_rebuild(conn):
                continue
            violations = _bank_rebuild_data_violations(conn)
            if violations:
                raise DataRecoveryRequiredError(
                    "Cannot safely rebuild bank_transactions: "
                    + "; ".join(violations)
                    + ". A pre-repair backup was preserved; correct the data "
                    "through an operator-reviewed recovery workflow."
                )
            _rebuild_bank_transactions(conn)
            applied.append(f"rebuild:{table}")

        # 3b. outgoing_documents rebuild — the SA/PR/Invoice/Partner columns were
        # removed when the document model was reduced to Receipt-only. Several are
        # self-FKs or an FK to the dropped `partners` table, so a plain DROP COLUMN
        # can't remove them. Rebuild to the lean schema keeping only receipts.
        if _table_exists(conn, "outgoing_documents"):
            if REMOVED_OUTDOC_COLUMNS & _existing_columns(conn, "outgoing_documents"):
                _rebuild_outgoing_documents(conn)
                applied.append("rebuild:outgoing_documents")

        # 4. Repair every missing/wrong model index plus the migration-owned
        # partial unique indexes by full signature, never by name alone.
        for table, signature in _pending_index_repairs(conn):
            if _repair_named_index(conn, table=table, signature=signature):
                applied.append(f"index:{signature.name}")
        for _table, index_name in _pending_unexpected_nonunique_indexes(conn):
            conn.execute(text(f'DROP INDEX "{index_name}"'))
            applied.append(f"drop_index:{index_name}")

        # Do not mutate financial rows while any non-targeted table constraint
        # remains absent or counterfeit. Populated drifted tables received an
        # online backup before this transaction began.
        if enforce_schema_gate:
            _require_no_unrepaired_table_signature_drift(conn)

        # 5. Data backfills run only after the structural enforcement gate.
        payment_repairs = _reconcile_legacy_payment_metadata(conn)
        for repair, count in payment_repairs.items():
            if count:
                applied.append(f"reconcile:payments:{repair}:{count}")
        invoice_tax_rows = _backfill_invoice_line_tax_codes(conn)
        if invoice_tax_rows:
            applied.append(f"backfill:invoice_line_tax_code:{invoice_tax_rows}")
        journal_rows = _backfill_journal_source_type(conn)
        if journal_rows:
            applied.append(f"backfill:journal_source_type:{journal_rows}")
        invoice_rows = _backfill_invoice_authorised_at(conn)
        if invoice_rows:
            applied.append(f"backfill:invoice_authorised_at:{invoice_rows}")

        # 6. Data backfill: ensure every outgoing_documents row has a
        # version-suffixed doc_number (`XX-YYYY-NNNN-1` for the original).
        renamed = _backfill_doc_number_version_suffix(conn)
        if renamed:
            applied.append(f"backfill:doc_number_v1:{renamed}")

        # This is deliberately unconditional.  A no-op migration still must not
        # let a legacy FK violation or corrupt SQLite file enter the application.
        _assert_sqlite_integrity(conn, context="company migrations")

    return applied
