from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import CompanyBase
# Import the models module so all per-company tables (invoices, journal_entries,
# etc.) are registered on CompanyBase.metadata before create_all(). Without this
# the test fails in isolation ("no such table: invoices") because nothing else
# has imported the models yet.
import app.models.company  # noqa: F401,E402
from app.db.migrations import run_company_migrations
from app.db.schema_sync import sync_missing_columns


def _columns(conn, table):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def test_invoice_posting_migration_backfills_and_is_idempotent():
    engine = create_engine("sqlite://", future=True)
    CompanyBase.metadata.create_all(engine)
    with engine.begin() as conn:
        for index in [
            "ix_journal_entries_source_type",
            "ix_journal_entries_source_id",
            "ix_journal_entries_reverses_entry_id",
        ]:
            conn.execute(text(f"DROP INDEX IF EXISTS {index}"))
        conn.execute(text("ALTER TABLE invoices DROP COLUMN authorised_at"))
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(
            text(
                "CREATE TABLE legacy_journal_entries ("
                "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
                "entry_date DATE NOT NULL, "
                "memo VARCHAR(500) NOT NULL, "
                "reference VARCHAR(80), "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL"
                ")"
            )
        )
        conn.execute(text("DROP TABLE journal_entries"))
        conn.execute(text("ALTER TABLE legacy_journal_entries RENAME TO journal_entries"))
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.execute(
            text(
                "INSERT INTO contacts (id, kind, name) "
                "VALUES (1, 'customer', 'Legacy Customer')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO invoices ("
                "id, direction, contact_id, invoice_number, issue_date, currency, "
                "subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
                "source, created_at, updated_at"
                ") VALUES ("
                "1, 'AR', 1, 'LEG-1', '2026-05-01', 'AUD', "
                "100.00, 10.00, 110.00, 1, 'unpaid', 0.00, "
                "'manual', '2026-05-01 09:30:00', '2026-05-01 09:30:00'"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO journal_entries (id, entry_date, memo, created_at, updated_at) "
                "VALUES (1, '2026-05-01', 'Legacy manual', "
                "'2026-05-01 09:00:00', '2026-05-01 09:00:00')"
            )
        )
        assert "authorised_at" not in _columns(conn, "invoices")
        assert "source_type" not in _columns(conn, "journal_entries")

    added = sync_missing_columns(engine, CompanyBase)
    first = run_company_migrations(engine)
    with engine.begin() as conn:
        invoice = conn.execute(
            text("SELECT status, authorised_at FROM invoices WHERE id=1")
        ).fetchone()
        journal = conn.execute(
            text("SELECT source_type, source_id, reverses_entry_id FROM journal_entries WHERE id=1")
        ).fetchone()
    assert invoice[0] == "unpaid"
    assert invoice[1] == "2026-05-01 09:30:00"
    assert journal == ("manual", None, None)
    second = run_company_migrations(engine)
    third_sync = sync_missing_columns(engine, CompanyBase)

    print("MIGRATION_ADDED", added)
    print("MIGRATION_FIRST", first)
    print("MIGRATION_SECOND", second)
    print("MIGRATION_SYNC_RERUN", third_sync)

    assert "invoices.authorised_at" in added
    assert "journal_entries.source_type" in added
    assert "backfill:invoice_authorised_at:1" in first
    assert second == []
    assert third_sync == []
