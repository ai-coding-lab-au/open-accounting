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

from sqlalchemy import text
from sqlalchemy.engine import Engine


# Tables that need a full rebuild because their CHECK constraints / FK ondelete
# rules changed after the table was first created. Each entry: (table_name, marker).
# `marker` is a substring we expect in the CREATE TABLE SQL once the rebuild has
# applied — its presence means we already rebuilt and can skip.
TABLE_REBUILDS: list[tuple[str, str]] = [
    # bank_transactions gained CHECK constraints (amount>0, gst bounds).
    ("bank_transactions", "ck_bank_txn_amount_positive"),
]


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
    `sync_missing_columns` always runs before migrations (init_company_db),
    so every model column is guaranteed present on the old table when the
    INSERT…SELECT copies it over.
    """
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
    try:
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
                    dedup_key VARCHAR(64),
                    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                    CONSTRAINT ck_bank_txn_amount_positive CHECK (amount > 0),
                    CONSTRAINT ck_bank_txn_gst_nonneg CHECK (gst_amount >= 0),
                    CONSTRAINT ck_bank_txn_gst_within CHECK (gst_amount <= amount)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO new_bank_transactions (
                    id, bank_account_id, direction, amount, occurred_at, memo,
                    counter_party_name, account_id, gst_amount, tax_code,
                    dedup_key, created_at
                )
                SELECT
                    id, bank_account_id, direction, amount, occurred_at, memo,
                    counter_party_name, account_id, gst_amount, tax_code,
                    dedup_key, created_at
                FROM bank_transactions
                """
            )
        )
        conn.execute(text("DROP TABLE bank_transactions"))
        conn.execute(text("ALTER TABLE new_bank_transactions RENAME TO bank_transactions"))
        for sql in index_sqls:
            # Skip indexes on columns the rebuild dropped (e.g. the auto index
            # on the removed linked_trust_entry_id) — recreating them would
            # fail with "no such column". Step 4 recreates the real ones.
            if "linked_trust_entry_id" in sql:
                continue
            conn.execute(text(sql))
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
    try:
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
        conn.execute(text("DROP TABLE outgoing_documents"))
        conn.execute(
            text("ALTER TABLE new_outgoing_documents RENAME TO outgoing_documents")
        )
        for sql in index_sqls:
            # Skip indexes on columns the rebuild dropped (service_agreement_id,
            # voided_by_sa_id, partner_ref_id, staff_member_id, …); recreating
            # them would fail with "no such column".
            if any(col in sql for col in REMOVED_OUTDOC_COLUMNS):
                continue
            conn.execute(text(sql))
    finally:
        conn.execute(text("PRAGMA foreign_keys = ON"))


def _ensure_bank_txn_dedup_unique_index(conn) -> None:
    """Partial UNIQUE index for statement-import deduplication."""
    if _index_exists(conn, "uq_bank_txn_dedup"):
        return
    if not _table_exists(conn, "bank_transactions"):
        return
    # Skip if the table hasn't gained dedup_key yet (schema_sync should
    # always run first; defensive guard).
    if "dedup_key" not in _existing_columns(conn, "bank_transactions"):
        return
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_txn_dedup "
            "ON bank_transactions (bank_account_id, dedup_key) "
            "WHERE dedup_key IS NOT NULL"
        )
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
    if _index_exists(conn, "uq_invoice_source_ref"):
        return
    if not _table_exists(conn, "invoices"):
        return
    # Skip if the table hasn't gained source/source_ref yet (schema_sync
    # should always run first; defensive guard).
    if not {"source", "source_ref"}.issubset(_existing_columns(conn, "invoices")):
        return
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_source_ref "
            "ON invoices (source, source_ref) "
            "WHERE source_ref IS NOT NULL"
        )
    )


def _ensure_journal_source_unique_indexes(conn) -> None:
    """Unique source provenance for generated journal entries."""
    if not _table_exists(conn, "journal_entries"):
        return
    cols = _existing_columns(conn, "journal_entries")
    if not {"source_type", "source_id", "reverses_entry_id"}.issubset(cols):
        return
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_source_doc "
            "ON journal_entries (source_type, source_id) "
            "WHERE source_id IS NOT NULL"
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_reversal_once "
            "ON journal_entries (reverses_entry_id) "
            "WHERE reverses_entry_id IS NOT NULL"
        )
    )


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


def run_company_migrations(engine: Engine) -> list[str]:
    """Apply pending schema migrations on a per-company DB.

    Returns the list of step names that ran this call. Idempotent.
    """
    applied: list[str] = []
    with engine.begin() as conn:
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
        # bank_transactions is rebuilt to (a) apply the CHECK constraints
        # (marker) and (b) drop the legacy linked_trust_entry_id column. The
        # column can't go via plain DROP COLUMN: its FK targets the
        # already-dropped trust_ledger_entries table, which SQLite's DROP COLUMN
        # integrity check rejects. The rebuild (new table → copy → drop → rename)
        # sidesteps that. So a DB that already has the marker but STILL has the
        # column must rebuild too — don't skip on the marker alone.
        for table, marker in TABLE_REBUILDS:
            if not _table_exists(conn, table):
                continue
            needs_check = marker not in _table_sql(conn, table)
            needs_drop = (
                table == "bank_transactions"
                and "linked_trust_entry_id" in _existing_columns(conn, table)
            )
            if not needs_check and not needs_drop:
                continue
            if table == "bank_transactions":
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

        # 4. Partial / functional indexes (create_all does not handle these).
        if not _index_exists(conn, "uq_bank_txn_dedup"):
            _ensure_bank_txn_dedup_unique_index(conn)
            if _index_exists(conn, "uq_bank_txn_dedup"):
                applied.append("index:uq_bank_txn_dedup")
        if not _index_exists(conn, "uq_invoice_source_ref"):
            _ensure_invoice_source_unique_index(conn)
            if _index_exists(conn, "uq_invoice_source_ref"):
                applied.append("index:uq_invoice_source_ref")
        before_journal_indexes = (
            _index_exists(conn, "uq_journal_source_doc"),
            _index_exists(conn, "uq_journal_reversal_once"),
        )
        _ensure_journal_source_unique_indexes(conn)
        after_journal_indexes = (
            _index_exists(conn, "uq_journal_source_doc"),
            _index_exists(conn, "uq_journal_reversal_once"),
        )
        if after_journal_indexes[0] and not before_journal_indexes[0]:
            applied.append("index:uq_journal_source_doc")
        if after_journal_indexes[1] and not before_journal_indexes[1]:
            applied.append("index:uq_journal_reversal_once")

        # 5. Data backfills.
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

    # A rebuild toggles `PRAGMA foreign_keys = OFF` and then runs DML
    # (INSERT … SELECT), which opens the transaction. SQLite treats the closing
    # `PRAGMA foreign_keys = ON` as a no-op while a transaction is open, so the
    # connection this migration used is left with FK enforcement OFF. That
    # connection then goes back to the pool and would silently run later requests
    # without FK enforcement. Re-assert it OUTSIDE any transaction (AUTOCOMMIT) so
    # it actually takes effect on the pooled connection. Only when something ran —
    # the common no-op startup skips this. (AUTOCOMMIT, not engine.dispose(),
    # because dispose() would wipe an in-memory DB along with its data.)
    if applied:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")

    return applied
