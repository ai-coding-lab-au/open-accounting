"""Tests for the reflection-based schema sync + drift detection.

These tests build a throwaway DeclarativeBase + in-memory SQLite engine so
they don't touch the real per-company schema. Each test starts from a "v1"
schema (subset of columns / tables), then defines a "v2" base with extras
and verifies the sync/detect helpers do the right thing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    func,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase

from app.db.schema_sync import detect_drift, require_clean_schema, sync_missing_columns


def _fresh_engine():
    # File-backed in-memory DB so multiple connections see the same data.
    return create_engine("sqlite://", future=True)


def test_sync_adds_missing_column_to_existing_table():
    engine = _fresh_engine()

    # v1 schema: widget table without `colour` column.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL)"))

    # v2 schema: model declares an extra `colour` column.
    class V2Base(DeclarativeBase):
        pass

    class Widget(V2Base):
        __tablename__ = "widget"
        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=False)
        colour = Column(String(20), nullable=True)

    added = sync_missing_columns(engine, V2Base)
    assert added == ["widget.colour"]

    cols = {c["name"] for c in inspect(engine).get_columns("widget")}
    assert cols == {"id", "name", "colour"}


def test_sync_is_idempotent():
    engine = _fresh_engine()

    class V2Base(DeclarativeBase):
        pass

    class Widget(V2Base):
        __tablename__ = "widget"
        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=False)
        colour = Column(String(20), nullable=True)

    V2Base.metadata.create_all(engine)
    # Table already matches the model — nothing to add, on this call or the next.
    assert sync_missing_columns(engine, V2Base) == []
    assert sync_missing_columns(engine, V2Base) == []


def test_sync_skips_table_not_in_db():
    """A model-declared table that doesn't exist in the DB is left alone.

    create_all() is responsible for creating tables; sync_missing_columns
    only touches tables that already exist (mirrors the production startup
    sequence: create_all → sync → drift-check)."""
    engine = _fresh_engine()

    class V2Base(DeclarativeBase):
        pass

    class Widget(V2Base):
        __tablename__ = "widget"
        id = Column(Integer, primary_key=True)

    added = sync_missing_columns(engine, V2Base)
    assert added == []
    # And drift detection should flag the missing table.
    report = detect_drift(engine, V2Base, scope="test")
    assert "widget" in report.missing_tables


def test_drift_detects_extra_column_in_db():
    engine = _fresh_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE widget (id INTEGER PRIMARY KEY, name VARCHAR(50), legacy_field TEXT)"
        ))

    class V2Base(DeclarativeBase):
        pass

    class Widget(V2Base):
        __tablename__ = "widget"
        id = Column(Integer, primary_key=True)
        name = Column(String(50))

    report = detect_drift(engine, V2Base, scope="test")
    assert not report.is_clean
    [td] = report.tables
    assert td.table == "widget"
    assert td.extra_in_db == ["legacy_field"]
    assert td.missing_in_db == []


def test_drift_clean_after_sync():
    engine = _fresh_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY, name VARCHAR(50))"))

    class V2Base(DeclarativeBase):
        pass

    class Widget(V2Base):
        __tablename__ = "widget"
        id = Column(Integer, primary_key=True)
        name = Column(String(50))
        colour = Column(String(20), nullable=True)

    sync_missing_columns(engine, V2Base)
    report = detect_drift(engine, V2Base, scope="test")
    assert report.is_clean, report.format()


def test_drift_report_format_lists_problems():
    engine = _fresh_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY, stale TEXT)"))

    class V2Base(DeclarativeBase):
        pass

    class Widget(V2Base):
        __tablename__ = "widget"
        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=True)

    class OtherTable(V2Base):
        __tablename__ = "other"
        id = Column(Integer, primary_key=True)

    # Don't run sync — we want to see drift on both axes.
    report = detect_drift(engine, V2Base, scope="test")
    text_report = report.format()
    assert "drift detected" in text_report
    assert "missing table: other" in text_report
    assert "missing columns ['name']" in text_report
    assert "extra columns in DB ['stale']" in text_report


def test_sync_adds_func_now_notnull_column_to_populated_table():
    """R3-F3 (boot-blocker class): ADD COLUMN ... DEFAULT CURRENT_TIMESTAMP /
    now() is a NON-CONSTANT default that SQLite rejects on a NON-EMPTY table.
    A NOT NULL func.now() column added to a populated table must sync without
    OperationalError: existing rows get a constant default, new inserts get
    now()."""
    engine = _fresh_engine()

    # v1 schema: widget table with a row already in it (populated).
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widget (id INTEGER PRIMARY KEY, name VARCHAR(50))"))
        conn.execute(text("INSERT INTO widget (id, name) VALUES (1, 'old')"))

    # v2 schema: model gains a NOT NULL timestamp column defaulted to func.now().
    class V2Base(DeclarativeBase):
        pass

    class Widget(V2Base):
        __tablename__ = "widget"
        id = Column(Integer, primary_key=True)
        name = Column(String(50))
        created_at = Column(
            DateTime, nullable=False, server_default=func.now()
        )

    # The additive ALTER must succeed (no "Cannot add a column with
    # non-constant default" OperationalError).
    added = sync_missing_columns(engine, V2Base)
    assert added == ["widget.created_at"]

    # Existing rows are back-filled with the constant default (not NULL), so
    # the NOT NULL column is satisfiable on the populated table.
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT created_at FROM widget WHERE id = 1")
        ).scalar_one()
        assert existing == "1970-01-01 00:00:00"

    # On a FRESH DB the column is created via create_all() from the same model,
    # where server_default=func.now() renders as CURRENT_TIMESTAMP, so new rows
    # get now() (not the epoch sentinel). The constant default only governs the
    # additive ALTER on old, populated DBs above.
    fresh_engine = _fresh_engine()
    V2Base.metadata.create_all(fresh_engine)
    with fresh_engine.begin() as conn:
        conn.execute(text("INSERT INTO widget (id, name) VALUES (1, 'new')"))
        fresh = conn.execute(
            text("SELECT created_at FROM widget WHERE id = 1")
        ).scalar_one()
        assert fresh != "1970-01-01 00:00:00"


def test_drift_detects_full_sqlite_enforcement_signature():
    """Same columns/types cannot conceal missing or counterfeit constraints."""

    class LedgerBase(DeclarativeBase):
        pass

    class Parent(LedgerBase):
        __tablename__ = "parent"
        id = Column(Integer, primary_key=True)

    class Ledger(LedgerBase):
        __tablename__ = "ledger"
        __table_args__ = (
            UniqueConstraint("code", name="uq_ledger_code"),
            CheckConstraint("amount >= 0", name="ck_ledger_amount_nonneg"),
            Index("ix_ledger_code", "code"),
        )
        id = Column(Integer, primary_key=True)
        parent_id = Column(
            Integer,
            ForeignKey("parent.id", ondelete="CASCADE"),
            nullable=False,
        )
        code = Column(String(20), nullable=False)
        amount = Column(Integer, nullable=False, server_default=text("0"))

    engine = _fresh_engine()
    LedgerBase.metadata.create_all(engine)
    assert detect_drift(engine, LedgerBase, scope="fresh").is_clean

    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(text("DROP TABLE ledger"))
        conn.execute(
            text(
                "CREATE TABLE ledger ("
                "id INTEGER, parent_id INTEGER, code VARCHAR(20), amount INTEGER)"
            )
        )
        # Correct name, wrong columns and uniqueness: a name-only check would
        # incorrectly accept this index.
        conn.execute(text("CREATE INDEX ix_ledger_code ON ledger (parent_id)"))

    report = detect_drift(engine, LedgerBase, scope="counterfeit")
    assert not report.is_clean
    rendered = report.format()
    assert "primary key mismatch" in rendered
    assert "parent_id NOT NULL mismatch" in rendered
    assert "amount default mismatch" in rendered
    assert "foreign keys mismatch" in rendered
    assert "CHECK constraints mismatch" in rendered
    assert "index ix_ledger_code mismatch" in rendered
    assert "UNIQUE columns mismatch" in rendered


def test_master_same_name_wrong_unique_index_is_fail_closed():
    from app.db.base import MasterBase
    from app.models import master as _master_models  # noqa: F401

    engine = _fresh_engine()
    MasterBase.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_companies_generation_id"))
        conn.execute(
            text(
                "CREATE INDEX ix_companies_generation_id "
                "ON companies (generation_id)"
            )
        )

    report = detect_drift(engine, MasterBase, scope="master")
    assert not report.is_clean
    assert "index ix_companies_generation_id mismatch" in report.format()
    # Other integration tests intentionally reload the app package.  Resolve
    # the exception class owned by this already-imported function so the test
    # does not confuse two equivalent class objects from different reloads.
    recovery_error = require_clean_schema.__globals__["DataRecoveryRequiredError"]
    with pytest.raises(recovery_error, match="Startup refused"):
        require_clean_schema(report)
