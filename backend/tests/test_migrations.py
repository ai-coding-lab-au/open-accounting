"""Tests for the company-DB migrations module.

`run_company_migrations()` is the place where SQLite-friendly hand-rolled
steps live (drop column, drop table, drop+recreate partial indexes, data
backfills). Each step must be idempotent: re-running it on an already-
migrated DB is a no-op.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, event, text

from app.db import migrations


def _engine():
    return create_engine("sqlite://", future=True)


def _file_engine_with_fk_hook(db_path):
    """A file-backed engine mirroring the production per-company engine: pooled
    connections + a connect hook that turns FK enforcement on (company.py)."""
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return engine


_OLD_OUTDOC_DDL = (
    "CREATE TABLE outgoing_documents ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, doc_type VARCHAR(20) NOT NULL, "
    "doc_number VARCHAR(40) NOT NULL, issue_date DATE NOT NULL, "
    "expiration_date DATE, service_agreement_id INTEGER, voided_by_sa_id INTEGER, "
    "partner_ref_id INTEGER, related_doc_id INTEGER, instalment_index INTEGER, "
    "staff_member_id INTEGER, sa_applicants JSON, sa_visa_items JSON, "
    "sa_payment_sched JSON, gst_inclusive BOOLEAN DEFAULT 0, "
    "customer_id INTEGER, client_ref_id INTEGER, customer_name VARCHAR(200) NOT NULL, "
    "customer_address VARCHAR(500), customer_abn VARCHAR(20), customer_email VARCHAR(200), "
    "customer_phone VARCHAR(50), currency VARCHAR(3) NOT NULL DEFAULT 'AUD', "
    "subtotal NUMERIC(16,2) NOT NULL DEFAULT 0, gst_amount NUMERIC(16,2) NOT NULL DEFAULT 0, "
    "total NUMERIC(16,2) NOT NULL DEFAULT 0, status VARCHAR(20) NOT NULL DEFAULT 'draft', "
    "paid_date DATE, payment_method VARCHAR(100), notes VARCHAR(1000), "
    "pdf_rel_path VARCHAR(500), created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
)


_OLD_OUTDOC_LINE_DDL = (
    "CREATE TABLE outgoing_document_lines ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL "
    "REFERENCES outgoing_documents(id) ON DELETE CASCADE, "
    "order_no INTEGER NOT NULL DEFAULT 0, description VARCHAR(500) NOT NULL, "
    "quantity NUMERIC(12,4) NOT NULL DEFAULT 1, "
    "unit_price NUMERIC(16,2) NOT NULL DEFAULT 0, "
    "amount NUMERIC(16,2) NOT NULL DEFAULT 0)"
)


_OLD_BANK_TXN_DDL = (
    "CREATE TABLE bank_transactions ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE RESTRICT, "
    "direction VARCHAR(3) NOT NULL, amount NUMERIC(16,2) NOT NULL, "
    "occurred_at DATE NOT NULL, memo VARCHAR(500), "
    "counter_party_name VARCHAR(200), "
    "account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL, "
    "gst_amount NUMERIC(16,2) NOT NULL DEFAULT 0, "
    "tax_code VARCHAR(20) NOT NULL DEFAULT 'standard', "
    "dedup_key VARCHAR(64), "
    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
)


def _populate_legacy_outgoing_graph(engine, *, stale_helper: bool = False):
    """Create retained/filtered parents, real child rows and seq > MAX(id)."""
    with engine.begin() as conn:
        # Targets referenced by the live rebuilt DDL.  The synthetic rows use
        # NULL FKs, but SQLite still requires the target tables to exist before
        # a new receipt can be inserted with enforcement enabled.
        conn.execute(text("CREATE TABLE contacts (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE clients (id INTEGER PRIMARY KEY)"))
        conn.execute(text(_OLD_OUTDOC_DDL))
        conn.execute(
            text(
                "CREATE INDEX ix_outdoc_customer_name "
                "ON outgoing_documents(customer_name)"
            )
        )
        conn.execute(text(_OLD_OUTDOC_LINE_DDL))
        conn.execute(
            text(
                "INSERT INTO outgoing_documents "
                "(id, doc_type, doc_number, issue_date, customer_name, partner_ref_id) "
                "VALUES "
                "(1, 'receipt', 'RCT-2026-0001-1', '2026-05-01', 'Keep', NULL), "
                "(2, 'payment_request', 'PR-2026-0001-1', '2026-05-02', 'Purge', NULL), "
                "(50, 'invoice', 'INV-2026-0001-1', '2026-05-03', 'Purge High', NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO outgoing_document_lines "
                "(id, document_id, order_no, description, quantity, unit_price, amount) "
                "VALUES "
                "(1, 1, 0, 'retained receipt line', 1, 10, 10), "
                "(2, 2, 0, 'filtered payment-request line', 1, 20, 20), "
                "(3, 50, 0, 'filtered high-id invoice line', 1, 30, 30)"
            )
        )
        # A deleted ID may leave sqlite_sequence above MAX(id).  Both values are
        # part of the no-reuse contract.
        conn.execute(
            text(
                "UPDATE sqlite_sequence SET seq = 80 "
                "WHERE name = 'outgoing_documents'"
            )
        )
        if stale_helper:
            conn.execute(
                text("CREATE TABLE new_outgoing_documents (stale_marker INTEGER)")
            )


def _outgoing_sequence(conn) -> int:
    return int(
        conn.execute(
            text(
                "SELECT seq FROM sqlite_sequence "
                "WHERE name = 'outgoing_documents'"
            )
        ).scalar_one()
    )


def _populate_legacy_bank_graph(engine, *, stale_helper: bool = False):
    """Create a valid old bank table with a deleted-ID high-water mark."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE accounts (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE bank_accounts (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO accounts (id) VALUES (1)"))
        conn.execute(text("INSERT INTO bank_accounts (id) VALUES (1)"))
        conn.execute(text(_OLD_BANK_TXN_DDL))
        conn.execute(
            text(
                "INSERT INTO bank_transactions "
                "(id, bank_account_id, direction, amount, occurred_at, account_id) "
                "VALUES (1, 1, 'in', 10, '2026-05-01', 1)"
            )
        )
        conn.execute(
            text(
                "UPDATE sqlite_sequence SET seq = 80 "
                "WHERE name = 'bank_transactions'"
            )
        )
        if stale_helper:
            conn.execute(
                text("CREATE TABLE new_bank_transactions (stale_marker INTEGER)")
            )


def _bank_sequence(conn) -> int:
    return int(
        conn.execute(
            text(
                "SELECT seq FROM sqlite_sequence "
                "WHERE name = 'bank_transactions'"
            )
        ).scalar_one()
    )


def test_rebuild_leaves_fk_enforcement_on_for_pooled_connections(tmp_path):
    """A rebuild toggles `PRAGMA foreign_keys=OFF` then runs DML, which opens the
    transaction and makes the closing `PRAGMA foreign_keys=ON` a silent no-op —
    leaving the connection (and every later checkout that reuses it) with FK
    enforcement OFF. run_company_migrations must not hand back a pool poisoned
    that way: a fresh checkout after a rebuild must report foreign_keys = 1.
    """
    engine = _file_engine_with_fk_hook(tmp_path / "co.db")
    with engine.begin() as conn:
        conn.execute(text(_OLD_OUTDOC_DDL))
        conn.execute(
            text(
                "INSERT INTO outgoing_documents "
                "(doc_type, doc_number, issue_date, customer_name) "
                "VALUES ('receipt', 'RCT-2026-0001-1', '2026-05-01', 'Client A')"
            )
        )

    applied = migrations.run_company_migrations(engine)
    assert "rebuild:outgoing_documents" in applied

    with engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert fk == 1, "FK enforcement must stay on after a rebuild migration"


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    ).fetchone()
    return row is not None


def test_migrations_drop_legacy_service_agreements_table_idempotently():
    """Build a DB that still has the deleted `service_agreements` table
    (the shape it had right before deletion), run migrations twice, and
    assert the table is gone after the first pass and the second pass
    is a no-op.
    """
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE service_agreements ("
                "id INTEGER PRIMARY KEY, doc_number VARCHAR(40))"
            )
        )
        conn.execute(
            text("CREATE UNIQUE INDEX uq_sa_number ON service_agreements (doc_number)")
        )

    first = migrations.run_company_migrations(engine)
    assert "drop_table:service_agreements" in first

    with engine.begin() as conn:
        assert not _table_exists(conn, "service_agreements")

    # Second pass must be a no-op (idempotent).
    second = migrations.run_company_migrations(engine)
    assert "drop_table:service_agreements" not in second


def test_migrations_rebuild_outgoing_documents_to_receipt_only():
    """An old-shape `outgoing_documents` (with the SA/PR/Invoice/partner columns
    + a `partners` table) is rebuilt to the lean Receipt-only schema: the removed
    columns and the partners table are gone, only receipts survive (SA/PR/Invoice
    and partner rows purged), and a second pass is a no-op.
    """
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE partners (id INTEGER PRIMARY KEY, display_name VARCHAR(200))"))
        conn.execute(text("CREATE INDEX ix_partners_display_name ON partners (display_name)"))
        # Old-shape outgoing_documents carrying the now-removed columns.
        conn.execute(
            text(
                "CREATE TABLE outgoing_documents ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, doc_type VARCHAR(20) NOT NULL, "
                "doc_number VARCHAR(40) NOT NULL, issue_date DATE NOT NULL, "
                "expiration_date DATE, service_agreement_id INTEGER, voided_by_sa_id INTEGER, "
                "partner_ref_id INTEGER, related_doc_id INTEGER, instalment_index INTEGER, "
                "staff_member_id INTEGER, sa_applicants JSON, sa_visa_items JSON, "
                "sa_payment_sched JSON, gst_inclusive BOOLEAN DEFAULT 0, "
                "customer_id INTEGER, client_ref_id INTEGER, customer_name VARCHAR(200) NOT NULL, "
                "customer_address VARCHAR(500), customer_abn VARCHAR(20), customer_email VARCHAR(200), "
                "customer_phone VARCHAR(50), currency VARCHAR(3) NOT NULL DEFAULT 'AUD', "
                "subtotal NUMERIC(16,2) NOT NULL DEFAULT 0, gst_amount NUMERIC(16,2) NOT NULL DEFAULT 0, "
                "total NUMERIC(16,2) NOT NULL DEFAULT 0, status VARCHAR(20) NOT NULL DEFAULT 'draft', "
                "paid_date DATE, payment_method VARCHAR(100), notes VARCHAR(1000), "
                "pdf_rel_path VARCHAR(500), created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)"
            )
        )
        conn.execute(text("CREATE INDEX ix_outgoing_documents_service_agreement_id ON outgoing_documents (service_agreement_id)"))
        # A client receipt (survives), a payment_request (purged), a partner receipt (purged).
        conn.execute(text(
            "INSERT INTO outgoing_documents (doc_type, doc_number, issue_date, customer_name, partner_ref_id) "
            "VALUES ('receipt', 'RCT-2026-0001-1', '2026-05-01', 'Client A', NULL)"
        ))
        conn.execute(text(
            "INSERT INTO outgoing_documents (doc_type, doc_number, issue_date, customer_name, partner_ref_id) "
            "VALUES ('payment_request', 'PR-2026-0001-1', '2026-05-01', 'Client B', NULL)"
        ))
        conn.execute(text(
            "INSERT INTO outgoing_documents (doc_type, doc_number, issue_date, customer_name, partner_ref_id) "
            "VALUES ('receipt', 'PRCT-2026-0001-1', '2026-05-01', 'Partner C', 1)"
        ))

    first = migrations.run_company_migrations(engine)
    assert "rebuild:outgoing_documents" in first
    assert "drop_table:partners" in first

    with engine.begin() as conn:
        assert not _table_exists(conn, "partners")
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(outgoing_documents)")).fetchall()}
        assert "service_agreement_id" not in cols
        assert "partner_ref_id" not in cols
        assert "sa_visa_items" not in cols
        assert "expiration_date" not in cols
        rows = conn.execute(text("SELECT doc_number FROM outgoing_documents")).fetchall()
        numbers = {r[0] for r in rows}
        assert numbers == {"RCT-2026-0001-1"}  # only the client receipt survives

    # Second pass: table now lean, no rebuild/drop reported.
    second = migrations.run_company_migrations(engine)
    assert "rebuild:outgoing_documents" not in second
    assert "drop_table:partners" not in second


def test_outgoing_rebuild_purges_filtered_children_preserves_high_water_and_backup(
    tmp_path,
):
    """The P0 fixture cannot orphan/reassociate lines or reuse any old ID."""
    db_path = tmp_path / "p0-success.db"
    engine = _file_engine_with_fk_hook(db_path)
    _populate_legacy_outgoing_graph(engine, stale_helper=True)

    first = migrations.run_company_migrations(engine)
    second = migrations.run_company_migrations(engine)
    third = migrations.run_company_migrations(engine)

    assert "backup:outgoing_documents" in first
    assert "rebuild:outgoing_documents" in first
    assert "rebuild:outgoing_documents" not in second
    assert "rebuild:outgoing_documents" not in third
    assert "backup:outgoing_documents" not in second
    assert "backup:outgoing_documents" not in third

    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT id FROM outgoing_documents ORDER BY id")
        ).scalars().all() == [1]
        assert conn.execute(
            text(
                "SELECT id, document_id, description "
                "FROM outgoing_document_lines ORDER BY id"
            )
        ).fetchall() == [(1, 1, "retained receipt line")]
        assert _outgoing_sequence(conn) == 80
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert not _table_exists(conn, "new_outgoing_documents")

        inserted_id = conn.execute(
            text(
                "INSERT INTO outgoing_documents "
                "(doc_type, doc_number, issue_date, customer_name) "
                "VALUES ('receipt', 'RCT-2026-0002-1', '2026-05-04', 'New') "
                "RETURNING id"
            )
        ).scalar_one()
        assert inserted_id == 81

    backups = list(tmp_path.glob("p0-success.db.pre-destructive-migration-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        # Recovery copy is the complete pre-destructive legacy state.
        assert backup.execute(
            "SELECT COUNT(*) FROM outgoing_documents"
        ).fetchone()[0] == 3
        assert backup.execute(
            "SELECT COUNT(*) FROM outgoing_document_lines"
        ).fetchone()[0] == 3
        assert backup.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='outgoing_documents'"
        ).fetchone()[0] == 80
        assert "expiration_date" in {
            row[1] for row in backup.execute("PRAGMA table_info(outgoing_documents)")
        }


@pytest.mark.parametrize(
    "failure_statement",
    [
        "INSERT INTO NEW_OUTGOING_DOCUMENTS",
        "DROP TABLE OUTGOING_DOCUMENTS",
        "CREATE INDEX IX_OUTDOC_CUSTOMER_NAME",
    ],
)
def test_outgoing_rebuild_failure_is_atomic_and_retryable(
    tmp_path, failure_statement,
):
    """Copy/drop failures roll back the helper and retry cleanly."""
    db_path = tmp_path / f"p0-failure-{failure_statement.split()[0].lower()}.db"
    engine = _file_engine_with_fk_hook(db_path)
    _populate_legacy_outgoing_graph(engine)

    should_fail = True

    def inject_failure(_conn, _cursor, statement, _params, _context, _many):
        nonlocal should_fail
        normalised = " ".join(statement.upper().split())
        if should_fail and normalised.startswith(failure_statement):
            should_fail = False
            raise RuntimeError("synthetic outgoing rebuild failure")

    event.listen(engine, "before_cursor_execute", inject_failure)
    try:
        with pytest.raises(RuntimeError, match="synthetic outgoing rebuild failure"):
            migrations.run_company_migrations(engine)
    finally:
        event.remove(engine, "before_cursor_execute", inject_failure)

    backups_after_failure = list(
        tmp_path.glob(f"{db_path.name}.pre-destructive-migration-*.bak")
    )
    assert len(backups_after_failure) == 1
    original_backup_bytes = backups_after_failure[0].read_bytes()

    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(
            text("SELECT id FROM outgoing_documents ORDER BY id")
        ).scalars().all() == [1, 2, 50]
        assert conn.execute(
            text("SELECT document_id FROM outgoing_document_lines ORDER BY id")
        ).scalars().all() == [1, 2, 50]
        assert _outgoing_sequence(conn) == 80
        assert not _table_exists(conn, "new_outgoing_documents")
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []

    retry = migrations.run_company_migrations(engine)
    no_op = migrations.run_company_migrations(engine)
    assert "rebuild:outgoing_documents" in retry
    assert "rebuild:outgoing_documents" not in no_op

    backups_after_retry = list(
        tmp_path.glob(f"{db_path.name}.pre-destructive-migration-*.bak")
    )
    assert len(backups_after_retry) == 2
    # Retry creates a new recovery point; the first one is never overwritten.
    assert backups_after_failure[0].read_bytes() == original_backup_bytes

    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT document_id FROM outgoing_document_lines ORDER BY id")
        ).scalars().all() == [1]
        assert _outgoing_sequence(conn) == 80
        inserted_id = conn.execute(
            text(
                "INSERT INTO outgoing_documents "
                "(doc_type, doc_number, issue_date, customer_name) "
                "VALUES ('receipt', 'RCT-RETRY-1', '2026-05-05', 'Retry') "
                "RETURNING id"
            )
        ).scalar_one()
        assert inserted_id == 81


def test_bank_rebuild_recovers_stale_helper_and_preserves_high_water(tmp_path):
    db_path = tmp_path / "bank-stale.db"
    engine = _file_engine_with_fk_hook(db_path)
    _populate_legacy_bank_graph(engine, stale_helper=True)

    first = migrations.run_company_migrations(engine)
    second = migrations.run_company_migrations(engine)
    third = migrations.run_company_migrations(engine)

    assert "rebuild:bank_transactions" in first
    assert "rebuild:bank_transactions" not in second
    assert "rebuild:bank_transactions" not in third
    with engine.begin() as conn:
        assert not _table_exists(conn, "new_bank_transactions")
        assert _bank_sequence(conn) == 80
        assert "ck_bank_txn_amount_positive" in conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='bank_transactions'"
            )
        ).scalar_one()
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(
            text(
                "SELECT unapplied_account_id, unapplied_amount "
                "FROM bank_transactions WHERE id = 1"
            )
        ).one() == (None, 0)
        inserted_id = conn.execute(
            text(
                "INSERT INTO bank_transactions "
                "(bank_account_id, direction, amount, occurred_at, account_id) "
                "VALUES (1, 'out', 5, '2026-05-02', 1) RETURNING id"
            )
        ).scalar_one()
        assert inserted_id == 81


@pytest.mark.parametrize(
    "failure_statement",
    [
        "INSERT INTO NEW_BANK_TRANSACTIONS",
        "DROP TABLE BANK_TRANSACTIONS",
    ],
)
def test_bank_rebuild_copy_drop_failures_are_atomic_and_retryable(
    tmp_path, failure_statement,
):
    db_path = tmp_path / f"bank-failure-{failure_statement.split()[0].lower()}.db"
    engine = _file_engine_with_fk_hook(db_path)
    _populate_legacy_bank_graph(engine)
    should_fail = True

    def inject_failure(_conn, _cursor, statement, _params, _context, _many):
        nonlocal should_fail
        normalised = " ".join(statement.upper().split())
        if should_fail and normalised.startswith(failure_statement):
            should_fail = False
            raise RuntimeError("synthetic bank rebuild failure")

    event.listen(engine, "before_cursor_execute", inject_failure)
    try:
        with pytest.raises(RuntimeError, match="synthetic bank rebuild failure"):
            migrations.run_company_migrations(engine)
    finally:
        event.remove(engine, "before_cursor_execute", inject_failure)

    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(
            text("SELECT id FROM bank_transactions ORDER BY id")
        ).scalars().all() == [1]
        assert _bank_sequence(conn) == 80
        assert not _table_exists(conn, "new_bank_transactions")
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []

    retry = migrations.run_company_migrations(engine)
    no_op = migrations.run_company_migrations(engine)
    assert "rebuild:bank_transactions" in retry
    assert "rebuild:bank_transactions" not in no_op
    with engine.begin() as conn:
        assert _bank_sequence(conn) == 80
        inserted_id = conn.execute(
            text(
                "INSERT INTO bank_transactions "
                "(bank_account_id, direction, amount, occurred_at, account_id) "
                "VALUES (1, 'out', 5, '2026-05-02', 1) RETURNING id"
            )
        ).scalar_one()
        assert inserted_id == 81


def test_populated_dropped_table_gets_online_backup(tmp_path):
    db_path = tmp_path / "populated-drop.db"
    engine = _file_engine_with_fk_hook(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE service_agreements ("
                "id INTEGER PRIMARY KEY, doc_number TEXT, private_notes TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO service_agreements "
                "VALUES (7, 'SA-LEGACY-7', 'private recovery marker')"
            )
        )

    first = migrations.run_company_migrations(engine)
    second = migrations.run_company_migrations(engine)
    assert "backup:drop_table:service_agreements" in first
    assert "drop_table:service_agreements" in first
    assert "backup:drop_table:service_agreements" not in second

    backups = list(
        tmp_path.glob(f"{db_path.name}.pre-destructive-migration-*.bak")
    )
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute(
            "SELECT id, doc_number, private_notes FROM service_agreements"
        ).fetchall() == [(7, "SA-LEGACY-7", "private recovery marker")]
    with engine.connect() as conn:
        assert not _table_exists(conn, "service_agreements")
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_noop_migration_rejects_preexisting_fk_violation_and_restores_fk(tmp_path):
    engine = _file_engine_with_fk_hook(tmp_path / "orphan.db")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(text("CREATE TABLE parents (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE children ("
                "id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL "
                "REFERENCES parents(id))"
            )
        )
        conn.execute(text("INSERT INTO children VALUES (1, 999)"))

    with pytest.raises(RuntimeError, match="foreign_key_check found 1 violation"):
        migrations.run_company_migrations(engine)

    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() != []


def test_migrations_on_fresh_db_is_safe_noop():
    """Brand-new empty DB (no legacy tables, no columns to drop): the
    migration run must not raise and must not claim any drops happened.
    """
    engine = _engine()
    applied = migrations.run_company_migrations(engine)
    # No drops should be reported on an empty DB.
    drops = [step for step in applied if step.startswith("drop_")]
    assert drops == [], f"expected no drops on empty DB, got {drops}"


def test_system_account_reconciliation_error_rolls_back_and_keeps_backup(tmp_path):
    db_path = tmp_path / "system-account-repair.db"
    engine = _file_engine_with_fk_hook(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE accounts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "code VARCHAR(20) NOT NULL UNIQUE, "
                "name VARCHAR(200) NOT NULL, type VARCHAR(20) NOT NULL, "
                "parent_id INTEGER, is_gst BOOLEAN NOT NULL DEFAULT 0, "
                "active BOOLEAN NOT NULL DEFAULT 1, description VARCHAR(500))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO accounts (code, name, type, is_gst, active) VALUES "
                "('1100', 'AR renamed', 'ASSET', 0, 1), "
                "('1200', 'GST Paid', 'ASSET', 1, 1), "
                "('2000', 'AP renamed', 'ASSET', 0, 0), "
                "('2100', 'GST Collected', 'LIABILITY', 1, 1), "
                "('3000', 'Capital renamed', 'EQUITY', 0, 1)"
            )
        )

    should_fail = True

    def inject_failure(_conn, _cursor, statement, _params, _context, _many):
        nonlocal should_fail
        normalised = " ".join(statement.upper().split())
        if should_fail and normalised.startswith("UPDATE ACCOUNTS SET TYPE"):
            should_fail = False
            raise RuntimeError("synthetic system-account repair failure")

    event.listen(engine, "before_cursor_execute", inject_failure)
    try:
        with pytest.raises(
            RuntimeError, match="synthetic system-account repair failure"
        ):
            migrations.reconcile_system_accounts(engine)
    finally:
        event.remove(engine, "before_cursor_execute", inject_failure)

    backup_pattern = f"{db_path.name}.pre-destructive-migration-*.bak"
    backups_after_failure = list(tmp_path.glob(backup_pattern))
    assert len(backups_after_failure) == 1
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT name, type, active FROM accounts WHERE code='2000'")
        ).one() == ("AP renamed", "ASSET", 0)
    with sqlite3.connect(backups_after_failure[0]) as backup:
        assert backup.execute(
            "SELECT name, type, active FROM accounts WHERE code='2000'"
        ).fetchone() == ("AP renamed", "ASSET", 0)

    retry = migrations.reconcile_system_accounts(engine)
    no_op = migrations.reconcile_system_accounts(engine)
    assert "backup:system_accounts" in retry
    assert "reconcile:system_account:2000:type=LIABILITY+active" in retry
    assert no_op == []
    assert len(list(tmp_path.glob(backup_pattern))) == 2
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT name, type, active FROM accounts WHERE code='2000'")
        ).one() == ("AP renamed", "LIABILITY", 1)
