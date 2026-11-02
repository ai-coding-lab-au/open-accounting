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

from app.db.base import CompanyBase
from app.db import migrations
from app.db.errors import DataRecoveryRequiredError
import app.models.company  # noqa: F401


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
        conn.execute(
            text(
                "CREATE TABLE accounts ("
                "id INTEGER PRIMARY KEY, "
                "code VARCHAR(20) NOT NULL, "
                "name VARCHAR(200) NOT NULL, "
                "type VARCHAR(20) NOT NULL, "
                "parent_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT, "
                "is_gst BOOLEAN NOT NULL DEFAULT 0, "
                "active BOOLEAN NOT NULL DEFAULT 1, "
                "description VARCHAR(500), "
                "created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE bank_accounts ("
                "id INTEGER PRIMARY KEY, "
                "name VARCHAR(200) NOT NULL, "
                "opening_balance NUMERIC(16, 2) NOT NULL DEFAULT 0, "
                "is_active BOOLEAN NOT NULL DEFAULT 1, "
                "created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO accounts (id, code, name, type, is_gst, active) "
                "VALUES (1, '1000', 'Operating cash', 'ASSET', 0, 1)"
            )
        )
        conn.execute(
            text("INSERT INTO bank_accounts (id, name) VALUES (1, 'Legacy Bank')")
        )
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


def test_allocation_batch_schema_migration_is_idempotent_without_backfill():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)

    first_run = migrations.run_company_migrations(engine)
    second_run = migrations.run_company_migrations(engine)

    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ))
        }
        assert {
            "allocation_batches",
            "allocation_batch_lines",
            "allocation_batch_tax_components",
            "allocation_batch_finalizations",
        } <= tables
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_batches"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_batch_finalizations"
        )).scalar_one() == 0
        trigger_sql = connection.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_allocation_batches_domain'"
        )).scalar_one()
        assert "CREATE TRIGGER \"trg_allocation_batches_domain\"" in trigger_sql
        assert "allocation_reallocation" in trigger_sql
    assert "guards:allocation_batches_append_only" in first_run
    assert "guards:allocation_batches_append_only" not in second_run
    engine.dispose()


def test_allocation_batch_migration_repairs_code_only_residual_trigger():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    migrations.run_company_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_allocation_batches_domain"))
        connection.execute(text(
            "CREATE TRIGGER trg_allocation_batches_domain "
            "BEFORE INSERT ON allocation_batches "
            "WHEN NEW.residual_amount_cents>0 AND NOT EXISTS ("
            "SELECT 1 FROM accounts a WHERE a.id=NEW.residual_account_id "
            "AND a.code NOT IN ('1100','1200','2000','2100')) "
            "BEGIN SELECT RAISE(ABORT, 'Old code-only residual policy'); END"
        ))

    repaired = migrations.run_company_migrations(engine)
    with engine.connect() as connection:
        trigger_sql = connection.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_allocation_batches_domain'"
        )).scalar_one()
        assert "a.active=1" in trigger_sql
        assert "a.type='LIABILITY'" in trigger_sql
        assert "a.type='ASSET'" in trigger_sql
        assert "FROM bank_accounts ba" in trigger_sql
        assert "Old code-only residual policy" not in trigger_sql
    assert "guards:allocation_batches_append_only" in repaired
    assert "guards:allocation_batches_append_only" not in migrations.run_company_migrations(engine)
    engine.dispose()


def test_allocation_batch_migration_repairs_partial_schema_without_bank_table():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    initial_run = migrations.run_company_migrations(engine)
    independent_trigger_names = {
        f"trg_{table}_no_{operation}"
        for table in (
            "allocation_batches",
            "allocation_batch_lines",
            "allocation_batch_tax_components",
            "allocation_batch_finalizations",
        )
        for operation in ("update", "delete", "replace_insert")
    }
    with engine.connect() as connection:
        initial_independent_triggers = {
            name: sql
            for name, sql in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            ))
            if name in independent_trigger_names
        }
    assert independent_trigger_names <= initial_independent_triggers.keys()

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE bank_transactions"))

    first_run = migrations.run_company_migrations(engine)
    second_run = migrations.run_company_migrations(engine)
    with engine.connect() as connection:
        assert not _table_exists(connection, "bank_transactions")
        invalid_triggers = connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND (lower(name) LIKE 'trg_allocation_batch%' "
            "OR lower(name) LIKE 'trg_bank_transactions_allocation_batch%') "
            "AND lower(COALESCE(sql, '')) LIKE '%bank_transactions%'"
        )).scalars().all()
        assert invalid_triggers == []
        assert _table_exists(connection, "allocation_batches")
        remaining_independent_triggers = {
            name: sql
            for name, sql in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            ))
            if name in independent_trigger_names
        }
        assert remaining_independent_triggers == initial_independent_triggers
    assert "guards:allocation_batches_append_only" in initial_run
    assert "drop_triggers:allocation_batches_missing_bank_table" in first_run
    assert "guards:allocation_batches_append_only" not in first_run
    assert second_run == []
    engine.dispose()


def test_allocation_batch_migration_repairs_empty_partial_tables_without_bank():
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE allocation_batches (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(text(
            "CREATE TABLE allocation_batch_lines (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(text(
            "CREATE TABLE allocation_batch_tax_components (id INTEGER PRIMARY KEY)"
        ))
        connection.execute(text(
            "CREATE TABLE allocation_batch_finalizations (batch_id INTEGER PRIMARY KEY)"
        ))

    applied = migrations.run_company_migrations(engine)
    with engine.connect() as connection:
        assert not _table_exists(connection, "bank_transactions")
        assert {
            "idempotency_key", "transaction_version", "expected_prior_version",
            "bank_transaction_id", "baseline_finalization_id",
        } <= {
            row[1] for row in connection.execute(
                text("PRAGMA table_info(allocation_batches)")
            )
        }
        index_names = {
            row[0] for row in connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ))
        }
        assert {
            "uq_allocation_batch_idempotency",
            "uq_allocation_batch_transaction_version",
            "uq_allocation_batch_line_order",
            "uq_allocation_batch_line_identity",
            "uq_allocation_batch_component_tax_code",
        } <= index_names
        trigger_names = {
            row[0] for row in connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ))
        }
        assert "trg_allocation_batches_no_update" in trigger_names
        assert "trg_allocation_batch_lines_no_update" in trigger_names
        assert "trg_allocation_batch_tax_components_no_update" in trigger_names
        assert "trg_allocation_batch_finalizations_no_update" in trigger_names
        assert not any(
            name.startswith("trg_allocation_batch")
            and "domain" in name
            and name in trigger_names
            for name in (
                "trg_allocation_batches_domain",
                "trg_allocation_batch_lines_domain",
                "trg_allocation_batch_finalizations_domain",
            )
        )
    assert "schema:allocation_batches" in applied
    assert "guards:allocation_batches_append_only" in applied
    engine.dispose()


def test_gst_adjustment_schema_survives_rebuild_without_legacy_backfill():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    migrations.run_company_migrations(engine)

    def gst_adjustment_snapshot(conn):
        return {
            "events": conn.execute(
                text(
                    "SELECT id, event_type, source_direction, adjustment_direction, "
                    "projection_box, amount_cents, gst_amount_cents, tax_code, "
                    "policy_version, reason, source_record_type, source_record_id, "
                    "lifecycle_operation_type, lifecycle_operation_id, "
                    "effective_date, awareness_date, agreement_date, "
                    "refund_repayment_date, adjustment_note_reference, "
                    "adjustment_note_held_date, manual_review_status, "
                    "reversal_of_event_id, tax_slice_count, created_at "
                    "FROM gst_adjustment_events ORDER BY id"
                )
            ).all(),
            "slices": conn.execute(
                text(
                    "SELECT id, event_id, tax_code, amount_cents, gst_amount_cents "
                    "FROM gst_adjustment_tax_slices ORDER BY id"
                )
            ).all(),
            "finalizations": conn.execute(
                text(
                    "SELECT event_id, tax_slice_count, amount_cents, gst_amount_cents, "
                    "finalized_at FROM gst_adjustment_finalizations ORDER BY event_id"
                )
            ).all(),
            "evidence": conn.execute(
                text(
                    "SELECT id, event_id, evidence_type, evidence_reference, "
                    "content_sha256, notes, created_at "
                    "FROM gst_adjustment_evidence ORDER BY id"
                )
            ).all(),
            "reviews": conn.execute(
                text(
                    "SELECT id, event_id, status, reviewer, reviewed_at, reason, created_at "
                    "FROM gst_adjustment_manual_reviews ORDER BY id"
                )
            ).all(),
        }

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO contacts (id, kind, name, active, created_at) "
                "VALUES (1, 'customer', 'Synthetic legacy contact', 1, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO invoices ("
                "id, direction, contact_id, invoice_number, issue_date, currency, "
                "subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
                "source, created_at, updated_at"
                ") VALUES (1, 'AR', 1, 'INV-LEGACY', '2026-07-01', 'AUD', "
                "100, 10, 110, 1, 'draft', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO credit_notes ("
                "id, source_invoice_id, direction, contact_id, credit_note_number, "
                "issue_date, currency, subtotal, gst_amount, total, gst_inclusive, "
                "status, created_at, updated_at"
                ") VALUES (99, 1, 'AR', 1, 'CN-LEGACY', '2026-08-01', 'AUD', "
                "100, 10, 110, 1, 'authorised', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_events ("
                "id, event_type, source_direction, adjustment_direction, "
                "projection_box, amount_cents, gst_amount_cents, tax_code, policy_version, "
                "reason, source_record_type, source_record_id, effective_date, "
                "awareness_date, agreement_date, refund_repayment_date, "
                "adjustment_note_reference, adjustment_note_held_date, "
                "manual_review_status, tax_slice_count"
                ") VALUES (1, 'agreement', 'AR', 'decreasing', '1B', 11000, 1000, "
                "'mixed', 'F-02-v1', 'Synthetic agreement', 'credit_note', 77, "
                "'2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', "
                "'CN-77', '2026-09-05', 'pending', 2)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (1, 'standard', 10000, 1000)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (1, 'gst_free', 1000, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reviewer, reviewed_at, reason) "
                "VALUES (1, 'approved', 'Synthetic reviewer', "
                "'2026-09-06 10:00:00', 'Synthetic approval')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_finalizations "
                "(event_id, tax_slice_count, amount_cents, gst_amount_cents) "
                "VALUES (1, 2, 11000, 1000)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_evidence "
                "(event_id, evidence_type, evidence_reference, content_sha256) "
                "VALUES (1, 'document', 'synthetic://agreement', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
            )
        )
        reversal_id = conn.execute(
            text(
                "INSERT INTO gst_adjustment_events ("
                "event_type, source_direction, adjustment_direction, projection_box, "
                "amount_cents, gst_amount_cents, tax_code, policy_version, reason, "
                "source_record_type, source_record_id, effective_date, awareness_date, "
                "agreement_date, refund_repayment_date, adjustment_note_reference, "
                "adjustment_note_held_date, manual_review_status, tax_slice_count, "
                "reversal_of_event_id"
                ") VALUES ('reversal', 'AR', 'increasing', '1A', 11000, 1000, "
                "'mixed', 'F-02-v1', 'Synthetic reversal', 'credit_note', 77, "
                "'2026-09-07', '2026-09-08', '2026-09-09', '2026-09-10', "
                "'CN-77-REV', '2026-09-11', 'pending', 2, 1) RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (:event_id, 'standard', 10000, 1000)"
            ),
            {"event_id": reversal_id},
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (:event_id, 'gst_free', 1000, 0)"
            ),
            {"event_id": reversal_id},
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reviewer, reason) "
                "VALUES (:event_id, 'pending', 'Synthetic reviewer', 'Reversal review opened')"
            ),
            {"event_id": reversal_id},
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reviewer, reviewed_at, reason) "
                "VALUES (:event_id, 'approved', 'Synthetic reviewer', "
                "'2026-09-12 10:00:00', 'Synthetic reversal approval')"
            ),
            {"event_id": reversal_id},
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_evidence "
                "(event_id, evidence_type, evidence_reference, content_sha256) "
                "VALUES (:event_id, 'document', 'synthetic://reversal', "
                "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')"
            ),
            {"event_id": reversal_id},
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_finalizations "
                "(event_id, tax_slice_count, amount_cents, gst_amount_cents) "
                "VALUES (:event_id, 2, 11000, 1000)"
            ),
            {"event_id": reversal_id},
        )
        legacy_credit_note = conn.execute(
            text(
                "SELECT id, credit_note_number, subtotal, gst_amount, total, status "
                "FROM credit_notes WHERE id=99"
            )
        ).one()
        event_before_rebuild = conn.execute(
            text(
                "SELECT policy_version, effective_date, awareness_date, agreement_date, "
                "refund_repayment_date, adjustment_note_held_date "
                "FROM gst_adjustment_events WHERE id=1"
            )
        ).one()
        f02_before_rebuild = gst_adjustment_snapshot(conn)
        conn.execute(text("DROP TABLE bank_transactions"))
        conn.execute(text(_OLD_BANK_TXN_DDL))

    applied = migrations.run_company_migrations(engine)
    assert "rebuild:bank_transactions" in applied

    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT id, credit_note_number, subtotal, gst_amount, total, status "
                "FROM credit_notes WHERE id=99"
            )
        ).one() == legacy_credit_note
        assert conn.execute(
            text("SELECT COUNT(*) FROM gst_adjustment_events WHERE source_record_id=99")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM gst_adjustment_events")
        ).scalar_one() == 2
        assert conn.execute(
            text(
                "SELECT id, event_type, reversal_of_event_id, source_record_type, "
                "source_record_id, policy_version, amount_cents, gst_amount_cents "
                "FROM gst_adjustment_events ORDER BY id"
            )
        ).all() == [
            (1, "agreement", None, "credit_note", 77, "F-02-v1", 11000, 1000),
            (reversal_id, "reversal", 1, "credit_note", 77, "F-02-v1", 11000, 1000),
        ]
        assert gst_adjustment_snapshot(conn) == f02_before_rebuild
        assert conn.execute(
            text(
                "SELECT policy_version, effective_date, awareness_date, agreement_date, "
                "refund_repayment_date, adjustment_note_held_date "
                "FROM gst_adjustment_events WHERE id=1"
            )
        ).one() == event_before_rebuild
        assert conn.execute(
            text(
                "SELECT tax_code, amount_cents, gst_amount_cents FROM gst_adjustment_tax_slices "
                "WHERE event_id=1 ORDER BY tax_code"
            )
        ).all() == [("gst_free", 1000, 0), ("standard", 10000, 1000)]
        assert conn.execute(
            text(
                "SELECT evidence_reference, content_sha256 FROM gst_adjustment_evidence "
                "WHERE event_id=1"
            )
        ).one() == (
            "synthetic://agreement",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        assert conn.execute(
            text(
                "SELECT status, reviewer FROM gst_adjustment_manual_reviews "
                "WHERE event_id=1"
            )
        ).one() == ("approved", "Synthetic reviewer")
        assert conn.execute(
            text(
                "SELECT tax_slice_count, amount_cents, gst_amount_cents "
                "FROM gst_adjustment_finalizations WHERE event_id=1"
            )
        ).one() == (2, 11000, 1000)
        reversal_index_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_gst_adjustment_events_reversal_once'"
            )
        ).scalar_one()
        assert "WHERE reversal_of_event_id IS NOT NULL" in reversal_index_sql

    second_applied = migrations.run_company_migrations(engine)
    assert "rebuild:bank_transactions" not in second_applied
    assert "guards:gst_adjustment_append_only" not in second_applied
    with engine.connect() as conn:
        assert gst_adjustment_snapshot(conn) == f02_before_rebuild
        with pytest.raises(Exception, match="append-only"):
            conn.execute(
                text(
                    "UPDATE gst_adjustment_events SET policy_version='F-02-v2' "
                    "WHERE id=1"
                )
            )

    engine.dispose()


def test_gst_adjustment_lifecycle_operation_columns_are_additive_and_idempotent():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    migrations.run_company_migrations(engine)

    with engine.begin() as conn:
        event_id = conn.execute(
            text(
                "INSERT INTO gst_adjustment_events ("
                "event_type, source_direction, adjustment_direction, projection_box, "
                "amount_cents, gst_amount_cents, tax_code, policy_version, reason, "
                "source_record_type, source_record_id, effective_date, tax_slice_count"
                ") VALUES ('agreement', 'AP', 'decreasing', '1B', 10000, 1000, "
                "'standard', 'F-02-v1', 'Synthetic legacy event', 'credit_note', "
                "77, '2026-09-01', 1) RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (:event_id, 'standard', 10000, 1000)"
            ),
            {"event_id": event_id},
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_finalizations "
                "(event_id, tax_slice_count, amount_cents, gst_amount_cents) "
                "VALUES (:event_id, 1, 10000, 1000)"
            ),
            {"event_id": event_id},
        )
        legacy_event = conn.execute(
            text(
                "SELECT event_type, source_direction, adjustment_direction, projection_box, "
                "amount_cents, gst_amount_cents, tax_code, policy_version, source_record_type, "
                "source_record_id, effective_date FROM gst_adjustment_events WHERE id=:event_id"
            ),
            {"event_id": event_id},
        ).one()
        trigger_names = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND "
                "(name LIKE 'trg_gst_adjustment_%' OR "
                "name LIKE 'trg_journal_entries_gst_adjustment_%')"
            )
        ).scalars().all()
        for trigger_name in trigger_names:
            conn.execute(text(f'DROP TRIGGER "{trigger_name}"'))
        conn.execute(
            text("ALTER TABLE gst_adjustment_events DROP COLUMN lifecycle_operation_type")
        )
        conn.execute(
            text("ALTER TABLE gst_adjustment_events DROP COLUMN lifecycle_operation_id")
        )

    repaired = migrations.run_company_migrations(engine)
    assert "guards:gst_adjustment_append_only" in repaired
    assert not any(step.startswith("rebuild:") for step in repaired)
    with engine.connect() as conn:
        event_columns = {
            row[1]: row for row in conn.execute(text("PRAGMA table_info(gst_adjustment_events)"))
        }
        assert event_columns["lifecycle_operation_type"][2].upper() == "VARCHAR(50)"
        assert event_columns["lifecycle_operation_id"][2].upper() == "INTEGER"
        assert event_columns["lifecycle_operation_type"][3] == 0
        assert event_columns["lifecycle_operation_id"][3] == 0
        assert conn.execute(
            text(
                "SELECT lifecycle_operation_type, lifecycle_operation_id FROM "
                "gst_adjustment_events WHERE id=:event_id"
            ),
            {"event_id": event_id},
        ).one() == (None, None)
        assert conn.execute(
            text(
                "SELECT event_type, source_direction, adjustment_direction, projection_box, "
                "amount_cents, gst_amount_cents, tax_code, policy_version, source_record_type, "
                "source_record_id, effective_date FROM gst_adjustment_events WHERE id=:event_id"
            ),
            {"event_id": event_id},
        ).one() == legacy_event
        assert conn.execute(
            text("SELECT COUNT(*) FROM gst_adjustment_finalizations WHERE event_id=:event_id"),
            {"event_id": event_id},
        ).scalar_one() == 1

    assert "guards:gst_adjustment_append_only" not in migrations.run_company_migrations(engine)
    engine.dispose()


def test_legacy_credit_note_upgrade_does_not_infer_gst_events():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in (
            "gst_adjustment_evidence",
            "gst_adjustment_manual_reviews",
            "gst_adjustment_tax_slices",
            "gst_adjustment_finalizations",
            "gst_adjustment_events",
        ):
            conn.execute(text(f'DROP TABLE "{table}"'))
        conn.execute(
            text(
                "INSERT INTO contacts (id, kind, name, active, created_at) "
                "VALUES (1, 'customer', 'Synthetic legacy contact', 1, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO invoices ("
                "id, direction, contact_id, invoice_number, issue_date, currency, "
                "subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
                "source, created_at, updated_at"
                ") VALUES (1, 'AR', 1, 'INV-LEGACY', '2026-07-01', 'AUD', "
                "100, 10, 110, 1, 'draft', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO credit_notes ("
                "id, source_invoice_id, direction, contact_id, credit_note_number, "
                "issue_date, currency, subtotal, gst_amount, total, gst_inclusive, "
                "status, created_at, updated_at"
                ") VALUES (99, 1, 'AR', 1, 'CN-LEGACY', '2026-08-01', 'AUD', "
                "100, 10, 110, 1, 'authorised', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    CompanyBase.metadata.create_all(engine)
    applied = migrations.run_company_migrations(engine)
    assert "guards:gst_adjustment_append_only" in applied
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM gst_adjustment_events")
        ).scalar_one() == 0
        assert conn.execute(
            text(
                "SELECT credit_note_number, subtotal, gst_amount, total, status "
                "FROM credit_notes WHERE id=99"
            )
        ).one() == ("CN-LEGACY", 100, 10, 110, "authorised")

    assert "guards:gst_adjustment_append_only" not in migrations.run_company_migrations(engine)
    engine.dispose()


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
        assert not _table_exists(conn, "bank_transactions")
        assert conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND (lower(name) LIKE 'trg_allocation_batch%' "
            "OR lower(name) LIKE 'trg_bank_transactions_allocation_batch%') "
            "AND lower(COALESCE(sql, '')) LIKE '%bank_transactions%'"
        )).scalar_one() == 0
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


def test_bank_rebuild_batch_trigger_lifecycle_is_atomic_and_retryable(
    tmp_path, monkeypatch
):
    engine = _file_engine_with_fk_hook(tmp_path / "batch-trigger-rebuild.db")
    _populate_legacy_bank_graph(engine)
    migrations.run_company_migrations(engine)
    CompanyBase.metadata.create_all(engine)
    migrations.run_company_migrations(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO contacts (id, kind, name, active, created_at) "
            "VALUES (1, 'customer', 'Synthetic batch customer', 1, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO accounts (id, code, name, type, is_gst, active) "
            "VALUES (2, '2200', 'Synthetic customer deposits', 'LIABILITY', 0, 1)"
        ))
        connection.execute(text(
            "INSERT INTO invoices (id, direction, contact_id, invoice_number, issue_date, "
            "currency, subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
            "source, created_at, updated_at) VALUES (1, 'AR', 1, 'SYN-BATCH', "
            "'2026-05-01', 'AUD', 5, 0.5, 5.5, 1, 'authorised', 0, 'manual', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "DROP TRIGGER trg_allocation_history_baseline_finalizations_domain"
        ))
        event_id = connection.execute(text(
            "INSERT INTO allocation_history_events (operation, bank_transaction_id) "
            "VALUES ('legacy_history_unknown', 1) RETURNING id"
        )).scalar_one()
        connection.execute(text(
            "INSERT INTO allocation_history_baseline_finalizations "
            "(baseline_event_id, bank_transaction_id, observed_payment_date, observed_at, "
            "observed_allocation_count, observed_component_count, "
            "gross_allocation_total_cents, gst_total_cents, integrity_hash, status, "
            "incomplete_reason_codes) VALUES (:event_id, 1, '2026-05-01', "
            "'2026-05-01 00:00:00', 0, 0, 0, 0, :hash, 'complete', '[]')"
        ), {"event_id": event_id, "hash": "0" * 64})
        migrations._ensure_allocation_history_guards(connection)

        batch_payload = {
            "kind": "batch",
            "bank_transaction_id": 1,
            "operation": "allocation_correction",
            "transaction_version": 1,
            "expected_prior_version": 0,
            "effective_date": "2026-05-01",
            "recorded_at": "2026-05-02 00:00:00",
            "idempotency_key": "synthetic-rebuild-batch",
            "reason": "Synthetic rebuild preservation",
            "evidence_reference": "synthetic://rebuild",
            "evidence_sha256": None,
            "actor_placeholder": "Synthetic operator",
            "linked_prior_batch_id": None,
            "internal_correction_approval_id": None,
            "manual_review_status": "approved",
            "residual_amount_cents": 500,
            "residual_account_id": 2,
            "baseline_finalization_id": event_id,
        }
        line_payload = {
            "kind": "line",
            "line_order": 0,
            "source_kind": "invoice",
            "invoice_or_bill_id": 1,
            "gross_cents": 500,
            "gst_cents": 50,
        }
        component_payload = {
            "kind": "component",
            "line_order": 0,
            "tax_code": "standard",
            "gross_cents": 500,
            "gst_cents": 50,
        }
        batch_hash = migrations.allocation_baseline_integrity_hash(
            [batch_payload, line_payload, component_payload]
        )
        batch_id = connection.execute(text(
            "INSERT INTO allocation_batches (bank_transaction_id, operation, "
            "transaction_version, expected_prior_version, effective_date, recorded_at, "
            "idempotency_key, canonical_payload_hash, reason, evidence_reference, "
            "actor_placeholder, manual_review_status, residual_amount_cents, "
            "residual_account_id, baseline_finalization_id) VALUES "
            "(1, 'allocation_correction', 1, 0, '2026-05-01', '2026-05-02 00:00:00', "
            "'synthetic-rebuild-batch', :hash, 'Synthetic rebuild preservation', "
            "'synthetic://rebuild', 'Synthetic operator', 'approved', 500, 2, "
            ":event_id) RETURNING id"
        ), {"hash": batch_hash, "event_id": event_id}).scalar_one()
        line_id = connection.execute(text(
            "INSERT INTO allocation_batch_lines "
            "(batch_id, line_order, source_kind, invoice_or_bill_id, gross_cents, gst_cents) "
            "VALUES (:batch_id, 0, 'invoice', 1, 500, 50) RETURNING id"
        ), {"batch_id": batch_id}).scalar_one()
        connection.execute(text(
            "INSERT INTO allocation_batch_tax_components "
            "(allocation_batch_line_id, tax_code, gross_cents, gst_cents) "
            "VALUES (:line_id, 'standard', 500, 50)"
        ), {"line_id": line_id})
        connection.execute(text(
            "INSERT INTO allocation_batch_finalizations "
            "(batch_id, allocation_line_count, component_count, total_allocated_cents, "
            "total_gst_cents, residual_cents, bank_cash_cents, "
            "canonical_integrity_hash, finalized_at) "
            "VALUES (:batch_id, 1, 1, 500, 50, 500, 1000, :hash, "
            "'2026-05-02 00:01:00')"
        ), {"batch_id": batch_id, "hash": batch_hash})

    bank_dependent_names = {
        "trg_allocation_batches_domain",
        "trg_allocation_batch_lines_domain",
        "trg_allocation_batch_finalizations_domain",
        "trg_bank_transactions_allocation_batch_cash_immutable",
        "trg_bank_transactions_allocation_batch_no_delete",
        "trg_bank_transactions_allocation_batch_no_replace",
    }
    before_rows = None
    with engine.connect() as connection:
        before_rows = (
            connection.execute(text("SELECT * FROM allocation_batches")).all(),
            connection.execute(text("SELECT * FROM allocation_batch_lines")).all(),
            connection.execute(text("SELECT * FROM allocation_batch_tax_components")).all(),
            connection.execute(text("SELECT * FROM allocation_batch_finalizations")).all(),
            connection.execute(text("SELECT * FROM bank_transactions")).all(),
        )
        before_triggers = {
            name: sql
            for name, sql in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            ))
            if name in bank_dependent_names
        }
    assert set(before_triggers) == bank_dependent_names

    observed_during_rebuild = []
    original_rebuild = migrations._rebuild_bank_transactions

    def observe_dependencies(connection):
        remaining = {
            row[0]
            for row in connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ))
            if row[0] in bank_dependent_names
        }
        observed_during_rebuild.append(remaining)
        return original_rebuild(connection)

    should_fail = True

    def inject_rebuild_failure(_conn, _cursor, statement, _params, _context, _many):
        nonlocal should_fail
        if should_fail and " ".join(statement.upper().split()).startswith(
            "DROP TABLE BANK_TRANSACTIONS"
        ):
            should_fail = False
            raise RuntimeError("synthetic batch-trigger rebuild failure")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_bank_needs_rebuild", lambda _conn: True)
        patch.setattr(migrations, "_rebuild_bank_transactions", observe_dependencies)
        event.listen(engine, "before_cursor_execute", inject_rebuild_failure)
        try:
            with pytest.raises(RuntimeError, match="synthetic batch-trigger rebuild failure"):
                migrations.run_company_migrations(engine)
        finally:
            event.remove(engine, "before_cursor_execute", inject_rebuild_failure)

        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM allocation_batches")).all() == before_rows[0]
            assert connection.execute(text("SELECT * FROM allocation_batch_lines")).all() == before_rows[1]
            assert connection.execute(text("SELECT * FROM allocation_batch_tax_components")).all() == before_rows[2]
            assert connection.execute(text("SELECT * FROM allocation_batch_finalizations")).all() == before_rows[3]
            assert connection.execute(text("SELECT * FROM bank_transactions")).all() == before_rows[4]
            restored_triggers = {
                name: sql
                for name, sql in connection.execute(text(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
                ))
                if name in bank_dependent_names
            }
            assert restored_triggers == before_triggers
            assert not _table_exists(connection, "new_bank_transactions")
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []

        retry = migrations.run_company_migrations(engine)
        assert "rebuild:bank_transactions" in retry

    assert observed_during_rebuild == [set(), set()]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM allocation_batches")).all() == before_rows[0]
        assert connection.execute(text("SELECT * FROM allocation_batch_lines")).all() == before_rows[1]
        assert connection.execute(text("SELECT * FROM allocation_batch_tax_components")).all() == before_rows[2]
        assert connection.execute(text("SELECT * FROM allocation_batch_finalizations")).all() == before_rows[3]
        assert connection.execute(text("SELECT * FROM bank_transactions")).all() == before_rows[4]
        restored_triggers = {
            name: sql
            for name, sql in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            ))
            if name in bank_dependent_names
        }
        assert restored_triggers == before_triggers
        assert not _table_exists(connection, "new_bank_transactions")
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    assert "rebuild:bank_transactions" not in migrations.run_company_migrations(engine)
    engine.dispose()


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
    """Brand-new company schema (no legacy tables or columns to drop): the
    migration run must remain valid and must not claim any drops happened.
    """
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    applied = migrations.run_company_migrations(engine)
    # No drops should be reported on an empty DB.
    drops = [step for step in applied if step.startswith("drop_")]
    assert drops == [], f"expected no drops on empty DB, got {drops}"
    with engine.connect() as conn:
        invoice_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(invoices)")).fetchall()
        }
    assert {
        "contact_name_snapshot",
        "contact_abn_snapshot",
        "contact_address_snapshot",
        "contact_email_snapshot",
        "contact_phone_snapshot",
    }.issubset(invoice_columns)


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


def test_allocation_history_migration_repairs_owned_guards_and_indexes_idempotently():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    first = migrations.run_company_migrations(engine)
    second = migrations.run_company_migrations(engine)
    assert "guards:allocation_history_append_only" in first
    assert "guards:allocation_history_append_only" not in second
    assert "schema:allocation_history" not in second

    with engine.begin() as conn:
        conn.execute(text('DROP INDEX "uq_allocation_history_idempotency"'))
        conn.execute(
            text(
                "CREATE INDEX uq_allocation_history_idempotency "
                "ON allocation_history_events (bank_transaction_id)"
            )
        )
        conn.execute(text('DROP INDEX "uq_allocation_history_baseline_allocation"'))
        conn.execute(
            text(
                "CREATE INDEX uq_allocation_history_baseline_allocation "
                "ON allocation_history_baseline_bindings (bank_transaction_id)"
            )
        )
        canonical = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_allocation_history_events_domain'"
            )
        ).scalar_one()
        altered_case = canonical.replace(
            "'allocation_correction'", "'ALLOCATION_CORRECTION'"
        )
        assert altered_case != canonical
        conn.execute(text('DROP TRIGGER "trg_allocation_history_events_domain"'))
        conn.exec_driver_sql(altered_case)
        conn.execute(
            text(
                'DROP TRIGGER "trg_allocation_history_baseline_finalizations_domain"'
            )
        )
        conn.execute(
            text(
                'CREATE TRIGGER "trg_allocation_history_baseline_finalizations_domain" '
                'BEFORE INSERT ON allocation_history_baseline_finalizations '
                'BEGIN SELECT 1; END'
            )
        )

    repaired = migrations.run_company_migrations(engine)
    assert "schema:allocation_history" in repaired
    assert "guards:allocation_history_append_only" in repaired
    with engine.connect() as conn:
        assert migrations.sqlite_index_matches(
            conn,
            "allocation_history_events",
            migrations.SQLiteIndexSignature(
                "uq_allocation_history_idempotency",
                ("idempotency_key",),
                unique=True,
                where="idempotency_key IS NOT NULL",
            ),
        )
        assert migrations.sqlite_index_matches(
            conn,
            "allocation_history_baseline_bindings",
            migrations.SQLiteIndexSignature(
                "uq_allocation_history_baseline_allocation",
                ("baseline_event_id", "allocation_row_id"),
                unique=True,
            ),
        )
        canonical = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_allocation_history_events_domain'"
            )
        ).scalar_one()
        assert "'allocation_correction'" in canonical
        assert "'ALLOCATION_CORRECTION'" not in canonical
        assert "Invalid allocation history event" in canonical
        baseline_finalization_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_allocation_history_baseline_finalizations_domain'"
            )
        ).scalar_one()
        assert "allocation_baseline_sha256" in baseline_finalization_sql
        assert "SELECT 1; END" not in baseline_finalization_sql
        assert conn.execute(
            text("SELECT COUNT(*) FROM bas_period_lodgment_markers")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM allocation_history_events")
        ).scalar_one() == 0
        with pytest.raises(Exception, match="Invalid internal BAS period marker"):
            conn.execute(
                text(
                    "INSERT INTO bas_period_lodgment_markers "
                    "(period_id, period_start, period_end, lodged_status, "
                    "internally_asserted_at, actor_placeholder, evidence_reference, "
                    "evidence_scope) VALUES ('blank', '2026-10-01', '2026-10-31', 1, "
                    "'2026-11-01', 'synthetic', ' ', "
                    "'internal_assertion_not_ato_lodgment_evidence')"
                )
            )
    engine.dispose()


def test_allocation_history_migration_repairs_empty_partial_table_by_table():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    migrations.run_company_migrations(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE allocation_history_tax_components"))
        conn.execute(
            text(
                "CREATE TABLE allocation_history_tax_components "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
        )
        conn.execute(
            text("INSERT INTO allocation_history_tax_components (id) VALUES (7)")
        )
        conn.execute(
            text("DROP TABLE allocation_history_baseline_tax_bindings")
        )
        conn.execute(
            text(
                "CREATE TABLE allocation_history_baseline_tax_bindings "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
        )
        conn.execute(
            text("INSERT INTO allocation_history_baseline_tax_bindings (id) VALUES (9)")
        )

    repaired = migrations.run_company_migrations(engine)
    assert "schema:allocation_history" in repaired
    with engine.connect() as conn:
        assert {
            "id", "snapshot_id", "tax_code", "gross_cents", "gst_cents"
        }.issubset(
            {row[1] for row in conn.execute(
                text("PRAGMA table_info(allocation_history_tax_components)")
            )}
        )
        assert conn.execute(
            text(
                "SELECT id, snapshot_id, tax_code, gross_cents, gst_cents "
                "FROM allocation_history_tax_components"
            )
        ).one() == (7, None, None, None, None)
        assert conn.execute(
            text("SELECT COUNT(*) FROM allocation_history_events")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM bas_period_lodgment_markers")
        ).scalar_one() == 0
        assert conn.execute(
            text(
                "SELECT id, allocation_binding_id, source_component_row_id, tax_code, "
                "component_gross_cents, component_gross_source, component_gst_cents, "
                "component_gst_source FROM allocation_history_baseline_tax_bindings"
            )
        ).one() == (9, None, None, None, None, None, None, None)
    engine.dispose()


def test_allocation_history_partial_binding_without_primary_key_fails_closed():
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    migrations.run_company_migrations(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE allocation_history_baseline_bindings"))
        conn.execute(
            text(
                "CREATE TABLE allocation_history_baseline_bindings "
                "(baseline_event_id INTEGER)"
            )
        )
    with pytest.raises(DataRecoveryRequiredError, match="primary key is missing"):
        migrations.run_company_migrations(engine)
    engine.dispose()


def test_bank_transaction_rebuild_preserves_allocation_history(monkeypatch):
    engine = _engine()
    CompanyBase.metadata.create_all(engine)
    migrations.run_company_migrations(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bank_accounts (id, name, opening_balance, is_active) "
                "VALUES (1, 'Synthetic history bank', 0, 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO bank_transactions "
                "(id, bank_account_id, direction, amount, occurred_at, gst_amount, "
                "tax_code, unapplied_amount) "
                "VALUES (1, 1, 'in', 20, '2026-10-01', 0, 'none', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO allocation_history_events "
                "(operation, bank_transaction_id) "
                "VALUES ('legacy_history_unknown', 1)"
            )
        )

    monkeypatch.setattr(migrations, "_bank_needs_rebuild", lambda _conn: True)
    rebuilt = migrations.run_company_migrations(engine)
    assert "rebuild:bank_transactions" in rebuilt
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT id, operation, bank_transaction_id, effective_date, "
                "evidence_reference FROM allocation_history_events"
            )
        ).all() == [(1, "legacy_history_unknown", 1, None, None)]
        assert conn.execute(
            text("SELECT id, amount, occurred_at FROM bank_transactions")
        ).one() == (1, 20, "2026-10-01")
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_bank_transactions_allocation_history_no_delete'"
            )
        ).scalar_one() == 1
    engine.dispose()
