"""Reusable schema synchronisation + drift detection.

`create_all()` only creates missing tables; it never touches a table that
already exists. For an evolving schema we additionally need:

  1. Auto-add columns the model declares but the live DB lacks.
  2. A drift report so a model change that *can't* be applied automatically
     (dropped columns, type changes, new CHECK / FK rules) becomes visible
     at startup instead of silently rotting.

This module is shared by the master DB and every per-company DB so both
sides use the same logic.

Out of scope on purpose (handled elsewhere or by hand-rolled steps in
`migrations.py`):
  - Dropping columns
  - Changing column types / constraints
  - CHECK / FK / partial-index changes (need full table rebuild on SQLite)
  - Data backfill
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import functions


@dataclass
class TableDrift:
    table: str
    missing_in_db: list[str] = field(default_factory=list)   # column declared on model, not in DB
    extra_in_db: list[str] = field(default_factory=list)     # column in DB, not on model
    type_mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    # (column_name, model_type, db_type)

    @property
    def is_clean(self) -> bool:
        return not (self.missing_in_db or self.extra_in_db or self.type_mismatches)


@dataclass
class SchemaDriftReport:
    scope: str  # e.g. "master" or "company:foo_ltd"
    tables: list[TableDrift] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)  # declared on model, not in DB

    @property
    def is_clean(self) -> bool:
        return not self.missing_tables and all(t.is_clean for t in self.tables)

    def format(self) -> str:
        if self.is_clean:
            return f"[schema-check] {self.scope}: clean"
        lines = [f"[schema-check] {self.scope}: drift detected"]
        for t in self.missing_tables:
            lines.append(f"  - missing table: {t}")
        for td in self.tables:
            if td.is_clean:
                continue
            if td.missing_in_db:
                lines.append(f"  - {td.table}: missing columns {td.missing_in_db}")
            if td.extra_in_db:
                lines.append(
                    f"  - {td.table}: extra columns in DB {td.extra_in_db} "
                    f"(declare a drop in migrations.COMPANY_DB_COLUMN_DROPS)"
                )
            for col, model_t, db_t in td.type_mismatches:
                lines.append(
                    f"  - {td.table}.{col}: type mismatch model={model_t!r} db={db_t!r}"
                )
        return "\n".join(lines)


def _scalar_default_clause(col) -> str:
    """Render a column's scalar Python default as ' DEFAULT <literal>'.

    Returns "" when the column has no scalar default we can stringify here.
    Server-side defaults (server_default=func.now()) are intentionally not
    rendered: those are applied by SQLite on row insert via the DEFAULT in
    the original CREATE TABLE, and adding a NULL column without DEFAULT
    here is harmless because callers always supply a value on INSERT.
    """
    # A server_default (e.g. server_default=text("'[]'")) is the authoritative
    # DB-level default and is required when ADDing a NOT NULL column to an
    # existing table — render it verbatim. This also covers columns whose
    # Python default is a callable (e.g. default=list), which isn't scalar.
    sd = getattr(col, "server_default", None)
    if sd is not None and getattr(sd, "arg", None) is not None:
        arg = sd.arg
        if isinstance(arg, functions.now):
            # func.now() is a NON-CONSTANT default. SQLite rejects a non-constant
            # DEFAULT in ALTER TABLE ... ADD COLUMN against a NON-EMPTY table
            # ("Cannot add a column with non-constant default"), so neither
            # CURRENT_TIMESTAMP nor now() is usable here. Render a CONSTANT
            # epoch literal instead: it satisfies a NOT NULL add on existing
            # rows, and fresh inserts still get func.now() — from the CREATE
            # TABLE default on new DBs and from the ORM onupdate/default at
            # runtime. This additive ALTER only governs back-filling old rows.
            return " DEFAULT '1970-01-01 00:00:00'"
        literal = getattr(arg, "text", None)  # sqlalchemy text() clause
        if literal is None:
            literal = str(arg)
        return f" DEFAULT {literal}"
    if col.default is None or not getattr(col.default, "is_scalar", False):
        return ""
    val = col.default.arg
    if isinstance(val, bool):
        return f" DEFAULT {1 if val else 0}"
    if isinstance(val, (int, float)):
        return f" DEFAULT {val}"
    if isinstance(val, str):
        # Note: `class Foo(str, Enum)` members are str subclass instances and
        # land here; their str-identity is the .value so this produces the
        # correct SQL literal automatically.
        escaped = val.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return ""


def sync_missing_columns(engine: Engine, base: type[DeclarativeBase]) -> list[str]:
    """For each mapped table that already exists, ADD any columns the model
    declares but the DB is missing. Returns a list of "table.column" strings
    that were added.

    SQLite supports ADD COLUMN for nullable columns and columns with a
    DEFAULT, which covers everything we add additively. NOT NULL columns
    without a default would fail; we don't add those — declare them as
    nullable or give them a default.

    FK constraints declared on the ORM column are NOT applied: SQLite can
    only enforce FKs declared at CREATE TABLE time, and `col.type.compile()`
    here only emits the data type, not REFERENCES. New tables get the FK
    via create_all(); columns added to existing tables get the type only.
    If you need a real enforced FK on an existing table, declare a rebuild
    step in migrations.TABLE_REBUILDS.
    """
    added: list[str] = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in base.metadata.tables.values():
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                nullable = "" if col.nullable else " NOT NULL"
                default_clause = _scalar_default_clause(col)
                conn.execute(
                    text(
                        f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" '
                        f"{col_type}{nullable}{default_clause}"
                    )
                )
                added.append(f"{table.name}.{col.name}")
    return added


def detect_drift(engine: Engine, base: type[DeclarativeBase], scope: str) -> SchemaDriftReport:
    """Compare ORM metadata to live DB and return a structured report.

    `sync_missing_columns` is expected to have run first, so a clean report
    after both calls means: nothing the framework can fix automatically is
    pending. Anything left over needs a hand-rolled step in migrations.py.
    """
    report = SchemaDriftReport(scope=scope)
    insp = inspect(engine)
    for table in base.metadata.tables.values():
        if not insp.has_table(table.name):
            report.missing_tables.append(table.name)
            continue
        live_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        model_cols = {c.name: c for c in table.columns}
        td = TableDrift(table=table.name)
        for name, col in model_cols.items():
            if name not in live_cols:
                td.missing_in_db.append(name)
                continue
            model_type = col.type.compile(dialect=engine.dialect).upper()
            db_type = str(live_cols[name]["type"]).upper()
            if not _types_compatible(model_type, db_type):
                td.type_mismatches.append((name, model_type, db_type))
        for name in live_cols:
            if name not in model_cols:
                td.extra_in_db.append(name)
        if not td.is_clean:
            report.tables.append(td)
    return report


# SQLite is loose about types; collapse common equivalents so we don't spam
# the log with spurious mismatches (BOOLEAN ↔ INTEGER, DATETIME ↔ TIMESTAMP …).
_TYPE_EQUIVALENCE_CLASSES: list[set[str]] = [
    {"BOOLEAN", "INTEGER", "INT", "TINYINT"},
    {"DATETIME", "TIMESTAMP"},
    {"TEXT", "CLOB"},
]


def _normalise_type(t: str) -> str:
    # Drop the (precision, scale) suffix so NUMERIC(16,2) compares to NUMERIC.
    if "(" in t:
        t = t.split("(", 1)[0]
    return t.strip()


def _types_compatible(model_type: str, db_type: str) -> bool:
    a = _normalise_type(model_type)
    b = _normalise_type(db_type)
    if a == b:
        return True
    for cls in _TYPE_EQUIVALENCE_CLASSES:
        if a in cls and b in cls:
            return True
    # VARCHAR(n) in the model often stores as VARCHAR in SQLite — same family.
    if a.startswith("VARCHAR") and b.startswith("VARCHAR"):
        return True
    return False
