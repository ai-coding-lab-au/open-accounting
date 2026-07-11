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
from sqlalchemy import text

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
        # Keep the legacy graph structurally valid.  The transaction fixture
        # below points at bank_account_id=1; older versions already had this
        # parent table, and the migration now (correctly) runs an unconditional
        # foreign_key_check before allowing startup to continue.
        con.executescript(
            """
            CREATE TABLE bank_accounts (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(200) NOT NULL,
                bsb VARCHAR(20),
                account_number VARCHAR(50),
                opening_balance NUMERIC(16, 2) NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                notes VARCHAR(500),
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL
            );
            INSERT INTO bank_accounts (id, name) VALUES (1, 'Legacy Bank');
            """
        )
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


def test_startup_reconciles_system_accounts_with_backup_and_preserves_history(
    data_dir,
):
    from app.config import settings
    from app.db.company import get_company_engine, init_company_db

    _added, first_applied = init_company_db(COMPANY_ID, allow_create=True)
    engine = get_company_engine(COMPANY_ID)
    db_path = settings.company_db_path(COMPANY_ID)
    backup_pattern = f"{db_path.name}.pre-destructive-migration-*.bak"

    assert any(step.startswith("seed:default_coa:") for step in first_applied)
    assert list(db_path.parent.glob(backup_pattern)) == []

    with engine.begin() as conn:
        account_ids = dict(
            conn.execute(
                text(
                    "SELECT code, id FROM accounts "
                    "WHERE code IN ('1100', '1200', '2000', '2100', '3000')"
                )
            ).all()
        )
        entry_id = conn.execute(
            text(
                "INSERT INTO journal_entries (entry_date, memo, source_type) "
                "VALUES ('2026-05-01', 'Legacy referenced controls', 'manual') "
                "RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO journal_lines "
                "(entry_id, account_id, debit_amount, credit_amount) VALUES "
                "(:entry_id, :ap_id, 100, 0), "
                "(:entry_id, :capital_id, 0, 100)"
            ),
            {
                "entry_id": entry_id,
                "ap_id": account_ids["2000"],
                "capital_id": account_ids["3000"],
            },
        )
        conn.execute(text("UPDATE accounts SET name='Trade Debtors' WHERE code='1100'"))
        conn.execute(text("DELETE FROM accounts WHERE code='1200'"))
        conn.execute(text("UPDATE accounts SET type='ASSET' WHERE code='2000'"))
        conn.execute(text("UPDATE accounts SET active=0 WHERE code='2100'"))
        conn.execute(
            text("UPDATE accounts SET type='INCOME', active=0 WHERE code='3000'")
        )

    added, applied = init_company_db(COMPANY_ID)
    assert added == []
    assert "backup:system_accounts" in applied
    assert "reconcile:system_account:1200:created" in applied
    assert "reconcile:system_account:2000:type=LIABILITY" in applied
    assert "reconcile:system_account:2100:active" in applied
    assert "reconcile:system_account:3000:type=EQUITY+active" in applied

    backups = list(db_path.parent.glob(backup_pattern))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute(
            "SELECT COUNT(*) FROM accounts WHERE code='1200'"
        ).fetchone()[0] == 0
        assert backup.execute(
            "SELECT type FROM accounts WHERE code='2000'"
        ).fetchone()[0] == "ASSET"
        assert backup.execute(
            "SELECT active FROM accounts WHERE code='2100'"
        ).fetchone()[0] == 0

    with engine.connect() as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                text(
                    "SELECT code, id, name, type, active, is_gst FROM accounts "
                    "WHERE code IN ('1100', '1200', '2000', '2100', '3000')"
                )
            ).all()
        }
        assert rows["1100"][1] == "Trade Debtors"
        assert rows["1100"][2:] == ("ASSET", 1, 0)
        assert rows["1200"][1:] == (
            "GST Paid (Input Tax Credits)",
            "ASSET",
            1,
            1,
        )
        assert rows["2000"][0] == account_ids["2000"]
        assert rows["2000"][2:4] == ("LIABILITY", 1)
        assert rows["2100"][0] == account_ids["2100"]
        assert rows["2100"][2:4] == ("LIABILITY", 1)
        assert rows["3000"][0] == account_ids["3000"]
        assert rows["3000"][2:4] == ("EQUITY", 1)
        referenced_ids = conn.execute(
            text(
                "SELECT account_id FROM journal_lines "
                "WHERE entry_id=:entry_id ORDER BY id"
            ),
            {"entry_id": entry_id},
        ).scalars().all()
        assert referenced_ids == [account_ids["2000"], account_ids["3000"]]

    second_added, second_applied = init_company_db(COMPANY_ID)
    assert second_added == []
    assert second_applied == []
    assert len(list(db_path.parent.glob(backup_pattern))) == 1


def test_duplicate_legacy_system_code_fails_closed_without_mutation(data_dir):
    from app.config import settings
    from app.db.company import init_company_db

    db_path = settings.company_db_path(COMPANY_ID)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code VARCHAR(20) NOT NULL,
                name VARCHAR(200) NOT NULL,
                type VARCHAR(20) NOT NULL,
                parent_id INTEGER,
                is_gst BOOLEAN NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT 1,
                description VARCHAR(500),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            );
            INSERT INTO accounts (code, name, type)
            VALUES ('1100', 'Legacy AR A', 'ASSET'),
                   ('1100', 'Legacy AR B', 'LIABILITY');
            """
        )

    with pytest.raises(RuntimeError, match="code 1100 is not unique"):
        init_company_db(COMPANY_ID)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT id, code, name, type, active FROM accounts ORDER BY id"
        ).fetchall() == [
            (1, "1100", "Legacy AR A", "ASSET", 1),
            (2, "1100", "Legacy AR B", "LIABILITY", 1),
        ]
    backups = list(
        db_path.parent.glob(f"{db_path.name}.pre-destructive-migration-*.bak")
    )
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute(
            "SELECT id, code, name, type, active FROM accounts ORDER BY id"
        ).fetchall() == [
            (1, "1100", "Legacy AR A", "ASSET", 1),
            (2, "1100", "Legacy AR B", "LIABILITY", 1),
        ]
