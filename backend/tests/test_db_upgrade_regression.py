"""Upgrade regression: old-schema company DB files must boot cleanly.

P0-G: `init_company_db` used to run the hand-rolled migrations BEFORE the
additive column sync, so a company DB predating bank_transactions.tax_code /
dedup_key crashed at startup ("no such column: dedup_key"), and the
bank_transactions rebuild used a frozen DDL snapshot that silently dropped
those columns (and their data) on mid-age DBs.

These tests build real old-schema SQLite files on disk and run the full
startup path (`init_company_db`: create_all → sync_missing_columns →
run_company_migrations) against them.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECT_ROOT = ROOT.parent

COMPANY_ID = "oldco"


@pytest.fixture()
def data_dir(monkeypatch, request):
    test_data = PROJECT_ROOT / "tmp" / "tests" / request.node.name
    if test_data.exists():
        shutil.rmtree(test_data)
    test_data.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(test_data))
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    yield test_data


# bank_transactions as it looked before tax_code/dedup_key existed and
# before the CHECK constraints were added (pre-rebuild marker).
OLD_SCHEMA_PRE_DEDUP = """
CREATE TABLE bank_transactions (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE RESTRICT,
    direction VARCHAR(3) NOT NULL,
    amount NUMERIC(16, 2) NOT NULL,
    occurred_at DATE NOT NULL,
    memo VARCHAR(500),
    counter_party_name VARCHAR(200),
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    gst_amount NUMERIC(16, 2) NOT NULL DEFAULT 0,
    linked_trust_entry_id INTEGER REFERENCES trust_ledger_entries(id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL
);
"""

# Mid-age shape: tax_code/dedup_key + the dedup index already exist (added
# by schema_sync / migrations of that era), but the CHECK-constraint rebuild
# marker is still missing — so the rebuild runs and must preserve the data.
OLD_SCHEMA_MID_AGE = """
CREATE TABLE bank_transactions (
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
    linked_trust_entry_id INTEGER REFERENCES trust_ledger_entries(id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL
);
CREATE UNIQUE INDEX uq_bank_txn_dedup
    ON bank_transactions (bank_account_id, dedup_key)
    WHERE dedup_key IS NOT NULL;
"""


def _build_old_db(schema_script: str, insert_sql: str | None = None) -> Path:
    from app.config import settings

    path = settings.company_db_path(COMPANY_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(schema_script)
        if insert_sql:
            con.execute(insert_sql)
        con.commit()
    finally:
        con.close()
    return path


def _boot():
    """Run the full per-company startup path and return (engine, drift report)."""
    from app.db.base import CompanyBase
    from app.db.company import get_company_engine, init_company_db
    from app.db.schema_sync import detect_drift

    init_company_db(COMPANY_ID)
    engine = get_company_engine(COMPANY_ID)
    report = detect_drift(engine, CompanyBase, f"company:{COMPANY_ID}")
    return engine, report


def _table_sql(conn, table: str) -> str:
    row = conn.exec_driver_sql(
        f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'"
    ).fetchone()
    return (row[0] if row else "") or ""


def _index_names(conn) -> set[str]:
    rows = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_pre_dedup_db_boots_and_upgrades(data_dir):
    """Scenario 1: DB predates tax_code/dedup_key AND the CHECK constraints.

    Pre-fix this crashed at startup: migrations ran before column sync, so
    the dedup partial index (and the frozen-snapshot rebuild) hit a missing
    dedup_key column.
    """
    _build_old_db(
        OLD_SCHEMA_PRE_DEDUP,
        "INSERT INTO bank_transactions "
        "(bank_account_id, direction, amount, occurred_at, memo, gst_amount) "
        "VALUES (1, 'in', 100.00, '2026-01-15', 'legacy row', 0)",
    )

    engine, report = _boot()  # must not raise

    with engine.connect() as conn:
        assert {"tax_code", "dedup_key"}.issubset(_columns(conn, "bank_transactions"))
        row = conn.exec_driver_sql(
            "SELECT memo, amount, tax_code, dedup_key FROM bank_transactions"
        ).fetchone()
        assert row[0] == "legacy row"
        assert float(row[1]) == 100.0
        assert row[2] == "standard"  # NOT NULL default backfilled
        assert row[3] is None
        # The rebuild applied the CHECK constraints…
        assert "ck_bank_txn_amount_positive" in _table_sql(conn, "bank_transactions")
        # …and the dedup partial index now exists.
        assert "uq_bank_txn_dedup" in _index_names(conn)

    assert report.is_clean, report.format()


def test_mid_age_db_rebuild_preserves_tax_code_and_dedup_key(data_dir):
    """Scenario 2: tax_code/dedup_key + dedup index exist, CHECK marker doesn't.

    Pre-fix the rebuild's frozen DDL snapshot dropped tax_code/dedup_key
    (losing data) and then crashed recreating the dedup index.
    """
    _build_old_db(
        OLD_SCHEMA_MID_AGE,
        "INSERT INTO bank_transactions "
        "(bank_account_id, direction, amount, occurred_at, memo, gst_amount, "
        " tax_code, dedup_key) "
        "VALUES (1, 'out', 55.50, '2026-02-01', 'mid-age row', 0, "
        "        'gst_free', 'abc123deadbeef')",
    )

    engine, report = _boot()  # must not raise

    with engine.connect() as conn:
        # Rebuild ran (marker now present) and kept the data intact.
        assert "ck_bank_txn_amount_positive" in _table_sql(conn, "bank_transactions")
        row = conn.exec_driver_sql(
            "SELECT memo, amount, tax_code, dedup_key FROM bank_transactions"
        ).fetchone()
        assert row[0] == "mid-age row"
        assert float(row[1]) == 55.5
        assert row[2] == "gst_free"
        assert row[3] == "abc123deadbeef"
        assert "uq_bank_txn_dedup" in _index_names(conn)

    assert report.is_clean, report.format()

    # Second boot is a clean no-op (idempotent).
    from app.db.company import init_company_db

    added, applied = init_company_db(COMPANY_ID)
    assert added == []
    assert applied == []
