"""Tests for the company-DB migrations module.

`run_company_migrations()` is the place where SQLite-friendly hand-rolled
steps live (drop column, drop table, drop+recreate partial indexes, data
backfills). Each step must be idempotent: re-running it on an already-
migrated DB is a no-op.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, text

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


def test_migrations_on_fresh_db_is_safe_noop():
    """Brand-new empty DB (no legacy tables, no columns to drop): the
    migration run must not raise and must not claim any drops happened.
    """
    engine = _engine()
    applied = migrations.run_company_migrations(engine)
    # No drops should be reported on an empty DB.
    drops = [step for step in applied if step.startswith("drop_")]
    assert drops == [], f"expected no drops on empty DB, got {drops}"
