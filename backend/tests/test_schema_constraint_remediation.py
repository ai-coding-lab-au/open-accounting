"""P0 schema-signature gate and safe index/bank remediation coverage."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, event, text

from app.db.base import CompanyBase
from app.db.errors import DataRecoveryRequiredError
from app.db.migrations import run_company_migrations
from app.db.schema_sync import (
    MIGRATION_INDEX_SIGNATURES,
    SQLiteIndexSignature,
    detect_drift,
    require_clean_schema,
    sqlite_index_matches,
)
from app.models import company as _company_models  # noqa: F401
from app.models import outgoing as _outgoing_models  # noqa: F401


def _engine(path):
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    CompanyBase.metadata.create_all(engine)
    return engine


def test_wrong_named_indexes_and_missing_ordinary_index_are_repaired(tmp_path):
    engine = _engine(tmp_path / "wrong-indexes.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        for name in (
            "uq_bank_txn_dedup",
            "uq_invoice_source_ref",
            "uq_journal_source_doc",
            "uq_journal_reversal_once",
            "ix_invoices_status",
        ):
            conn.execute(text(f'DROP INDEX "{name}"'))
        conn.execute(
            text("CREATE INDEX uq_bank_txn_dedup ON bank_transactions (dedup_key)")
        )
        conn.execute(
            text("CREATE INDEX uq_invoice_source_ref ON invoices (source_ref)")
        )
        conn.execute(
            text("CREATE INDEX uq_journal_source_doc ON journal_entries (source_id)")
        )
        conn.execute(
            text(
                "CREATE INDEX uq_journal_reversal_once "
                "ON journal_entries (source_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX ix_invoices_status ON invoices (invoice_number)")
        )

    applied = run_company_migrations(engine)
    assert "index:uq_bank_txn_dedup" in applied
    assert "index:uq_invoice_source_ref" in applied
    assert "index:uq_journal_source_doc" in applied
    assert "index:uq_journal_reversal_once" in applied
    assert "index:ix_invoices_status" in applied

    expected = {
        "bank_transactions": (
            SQLiteIndexSignature(
                "uq_bank_txn_dedup",
                ("bank_account_id", "dedup_key"),
                unique=True,
                where="dedup_key IS NOT NULL",
            ),
        ),
        **MIGRATION_INDEX_SIGNATURES,
    }
    with engine.connect() as conn:
        for table, signatures in expected.items():
            for signature in signatures:
                assert sqlite_index_matches(conn, table, signature)

    report = detect_drift(engine, CompanyBase, "repaired-indexes")
    assert report.is_clean, report.format()


def test_duplicate_source_rows_fail_closed_and_keep_backup(tmp_path):
    db_path = tmp_path / "duplicate-source.db"
    engine = _engine(db_path)
    run_company_migrations(engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO contacts (id, kind, name, active, created_at) "
                "VALUES (1, 'customer', 'Duplicate Source', 1, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(text("DROP INDEX uq_invoice_source_ref"))
        conn.execute(
            text("CREATE INDEX uq_invoice_source_ref ON invoices (source_ref)")
        )
        for number in ("SRC-1", "SRC-2"):
            conn.execute(
                text(
                    "INSERT INTO invoices ("
                    "direction, contact_id, invoice_number, issue_date, currency, "
                    "subtotal, gst_amount, total, gst_inclusive, status, "
                    "paid_amount, source, source_ref, created_at, updated_at"
                    ") VALUES ("
                    "'AR', 1, :number, '2026-07-12', 'AUD', 10, 1, 11, 1, "
                    "'draft', 0, 'excel', 'same-source', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                ),
                {"number": number},
            )

    with pytest.raises(DataRecoveryRequiredError, match="duplicated key group"):
        run_company_migrations(engine)

    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM invoices "
                "WHERE source='excel' AND source_ref='same-source'"
            )
        ).scalar_one() == 2
        # Failed repair is transactional: the original wrong index remains.
        row = conn.exec_driver_sql(
            "PRAGMA index_list('invoices')"
        ).fetchall()
        wrong = next(item for item in row if item[1] == "uq_invoice_source_ref")
        assert wrong[2] == 0

    backups = list(
        tmp_path.glob(
            f"{db_path.name}.pre-destructive-migration-*.bak"
        )
    )
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute(
            "SELECT COUNT(*) FROM invoices "
            "WHERE source='excel' AND source_ref='same-source'"
        ).fetchone()[0] == 2


def test_partial_bank_marker_triggers_full_safe_rebuild(tmp_path):
    engine = _engine(tmp_path / "partial-bank-marker.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(text("DROP TABLE bank_transactions"))
        conn.execute(
            text(
                "CREATE TABLE bank_transactions ("
                "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                "bank_account_id INTEGER NOT NULL, direction VARCHAR(3) NOT NULL, "
                "amount NUMERIC(16, 2) NOT NULL, occurred_at DATE NOT NULL, "
                "memo VARCHAR(500), counter_party_name VARCHAR(200), "
                "account_id INTEGER, gst_amount NUMERIC(16, 2) NOT NULL DEFAULT 0, "
                "tax_code VARCHAR(20) NOT NULL DEFAULT 'standard', "
                "dedup_key VARCHAR(64), "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "CONSTRAINT ck_bank_txn_amount_positive CHECK (amount > 0))"
            )
        )

    applied = run_company_migrations(engine)
    assert "rebuild:bank_transactions" in applied
    with engine.connect() as conn:
        table_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='bank_transactions'"
            )
        ).scalar_one()
        assert "ck_bank_txn_gst_nonneg" in table_sql
        assert "ck_bank_txn_gst_within" in table_sql
        assert "ck_bank_txn_unapplied_within" in table_sql
        assert "ck_bank_txn_unapplied_account" in table_sql
        assert (
            len(
                conn.exec_driver_sql(
                    "PRAGMA foreign_key_list('bank_transactions')"
                ).fetchall()
            )
            == 3
        )

    report = detect_drift(engine, CompanyBase, "repaired-bank")
    assert report.is_clean, report.format()


def test_missing_journal_constraints_are_blocked_by_signature_gate(tmp_path):
    engine = _engine(tmp_path / "constraintless-journal.db")
    run_company_migrations(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO contacts (id, kind, name, active, created_at) "
                "VALUES (1, 'customer', 'No Backfill Customer', 1, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO invoices ("
                "direction, contact_id, invoice_number, issue_date, currency, "
                "subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
                "source, created_at, updated_at) VALUES ("
                "'AR', 1, 'NO-BACKFILL', '2026-07-12', 'AUD', 10, 1, 11, 1, "
                "'unpaid', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(text("DROP TABLE journal_lines"))
        conn.execute(
            text(
                "CREATE TABLE journal_lines ("
                "id INTEGER, entry_id INTEGER, account_id INTEGER, "
                "debit_amount NUMERIC(16, 2), "
                "credit_amount NUMERIC(16, 2), description VARCHAR(500))"
            )
        )

    # Indexes are safe to restore, but PK/NOT NULL/FK/CHECK need an explicit
    # table rebuild and therefore remain a fail-closed recovery condition.
    with pytest.raises(DataRecoveryRequiredError, match="journal_lines"):
        run_company_migrations(engine, enforce_schema_gate=True)
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT authorised_at FROM invoices "
                "WHERE invoice_number='NO-BACKFILL'"
            )
        ).scalar_one() is None
    report = detect_drift(engine, CompanyBase, "constraintless-journal")
    assert not report.is_clean
    with pytest.raises(DataRecoveryRequiredError, match="journal_lines"):
        require_clean_schema(report)
