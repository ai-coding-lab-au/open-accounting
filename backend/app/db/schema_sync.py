"""Reusable schema synchronisation + drift detection.

`create_all()` only creates missing tables; it never touches a table that
already exists. For an evolving schema we additionally need:

  1. Auto-add columns the model declares but the live DB lacks.
  2. A full SQLite schema-signature report so a model change that *can't* be
     applied automatically (PK / NOT NULL / UNIQUE / FK / CHECK / index rules)
     becomes visible at startup instead of silently rotting.

This module is shared by the master DB and every per-company DB so both
sides use the same logic.

Repair remains out of scope on purpose (handled elsewhere or by hand-rolled
steps in `migrations.py`):
  - Dropping columns
  - Changing column types / table constraints
  - CHECK / FK changes (need full table rebuild on SQLite)
  - Data backfill
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import re
from typing import Any

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import functions

from .errors import DataRecoveryRequiredError


@dataclass(frozen=True)
class SQLiteIndexSignature:
    """The enforcement-relevant shape of one named SQLite index."""

    name: str
    columns: tuple[str, ...]
    unique: bool = False
    where: str | None = None


# These indexes are deliberately migration-owned rather than present in the
# SQLAlchemy model.  They still form part of every company DB's required live
# signature and therefore must be checked alongside model-declared indexes.
MIGRATION_INDEX_SIGNATURES: dict[str, tuple[SQLiteIndexSignature, ...]] = {
    "invoices": (
        SQLiteIndexSignature(
            "uq_invoice_source_ref",
            ("source", "source_ref"),
            unique=True,
            where="source_ref IS NOT NULL",
        ),
    ),
    "journal_entries": (
        SQLiteIndexSignature(
            "uq_journal_source_doc",
            ("source_type", "source_id"),
            unique=True,
            where="source_id IS NOT NULL",
        ),
        SQLiteIndexSignature(
            "uq_journal_reversal_once",
            ("reverses_entry_id",),
            unique=True,
            where="reverses_entry_id IS NOT NULL",
        ),
    ),
}


@dataclass
class TableDrift:
    table: str
    missing_in_db: list[str] = field(default_factory=list)   # column declared on model, not in DB
    extra_in_db: list[str] = field(default_factory=list)     # column in DB, not on model
    type_mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    # (column_name, model_type, db_type)
    signature_mismatches: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (
            self.missing_in_db
            or self.extra_in_db
            or self.type_mismatches
            or self.signature_mismatches
        )


@dataclass
class SchemaDriftReport:
    scope: str  # e.g. "master" or "company:foo_ltd"
    tables: list[TableDrift] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)  # declared on model, not in DB
    extra_tables: list[str] = field(default_factory=list)  # live application table, not in model

    @property
    def is_clean(self) -> bool:
        return (
            not self.missing_tables
            and not self.extra_tables
            and all(t.is_clean for t in self.tables)
        )

    def format(self) -> str:
        if self.is_clean:
            return f"[schema-check] {self.scope}: clean"
        lines = [f"[schema-check] {self.scope}: drift detected"]
        for t in self.missing_tables:
            lines.append(f"  - missing table: {t}")
        for t in self.extra_tables:
            lines.append(f"  - unexpected table in DB: {t}")
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
            for mismatch in td.signature_mismatches:
                lines.append(f"  - {td.table}: {mismatch}")
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


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _strip_balanced_outer_parentheses(value: str) -> str:
    value = value.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        balanced = True
        for position, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and position != len(value) - 1:
                    balanced = False
                    break
            if depth < 0:
                balanced = False
                break
        if not balanced or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def normalise_sql_expression(value: Any) -> str | None:
    """Normalise a reflected SQLite expression for stable comparison."""

    if value is None:
        return None
    normalised = _strip_balanced_outer_parentheses(str(value))
    normalised = normalised.replace('"', "").replace("`", "")
    normalised = re.sub(r"\s+", " ", normalised).strip().casefold()
    normalised = re.sub(
        r"\s*(<=|>=|<>|!=|=|<|>)\s*",
        r" \1 ",
        normalised,
    )
    normalised = re.sub(r"\(\s+", "(", normalised)
    normalised = re.sub(r"\s+\)", ")", normalised)
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return normalised or None


def _normalise_default(value: Any) -> str | None:
    normalised = normalise_sql_expression(value)
    if normalised in {"now()", "current_timestamp()"}:
        return "current_timestamp"
    return normalised


def _accepted_model_defaults(column) -> set[str | None]:
    """Return every live default shape produced by supported schema paths.

    Fresh tables contain server defaults only.  ``sync_missing_columns`` also
    renders scalar Python defaults when adding a column to a legacy table, so
    both the fresh ``NULL`` default and the additive scalar default are valid
    signatures for those columns.
    """

    server_default = getattr(column, "server_default", None)
    if server_default is not None and getattr(server_default, "arg", None) is not None:
        arg = server_default.arg
        if isinstance(arg, functions.now):
            return {
                "current_timestamp",
                "'1970-01-01 00:00:00'",
            }
        rendered = _normalise_default(getattr(arg, "text", arg))
        accepted = {rendered}
        # SQLAlchemy quotes a plain Python string used as server_default, while
        # text("0") is emitted verbatim. Accept both equivalent SQLite forms.
        if isinstance(arg, str) and rendered is not None:
            accepted.add(f"'{rendered}'")
        return accepted

    # Python-side scalar defaults are absent from fresh CREATE TABLE output but
    # are rendered by the supported additive ALTER path.
    clause = _scalar_default_clause(column)
    if clause:
        rendered = _normalise_default(clause.removeprefix(" DEFAULT "))
        accepted = {None, rendered}
        if rendered is not None and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", rendered):
            accepted.add(f"'{rendered}'")
        return accepted
    default = getattr(column, "default", None)
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, Decimal):
            rendered = _normalise_default(str(value))
            return {None, rendered, f"'{rendered}'"}
    return {None}


def _where_from_index_sql(sql: str | None) -> str | None:
    if not sql:
        return None
    match = re.search(r"\bWHERE\b(.+)$", sql, flags=re.IGNORECASE | re.DOTALL)
    return normalise_sql_expression(match.group(1)) if match else None


def live_sqlite_indexes(conn, table_name: str) -> dict[str, SQLiteIndexSignature]:
    """Return named SQLite indexes, including their actual partial predicate."""

    quoted_table = _quote_identifier(table_name)
    signatures: dict[str, SQLiteIndexSignature] = {}
    for row in conn.exec_driver_sql(f"PRAGMA index_list({quoted_table})").fetchall():
        name = row[1]
        quoted_name = _quote_identifier(name)
        columns = tuple(
            index_row[2]
            for index_row in conn.exec_driver_sql(
                f"PRAGMA index_info({quoted_name})"
            ).fetchall()
        )
        sql_row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?",
            (name,),
        ).fetchone()
        signatures[name] = SQLiteIndexSignature(
            name=name,
            columns=columns,
            unique=bool(row[2]),
            where=_where_from_index_sql(sql_row[0] if sql_row else None),
        )
    return signatures


def model_sqlite_indexes(table) -> dict[str, SQLiteIndexSignature]:
    signatures: dict[str, SQLiteIndexSignature] = {}
    for index in table.indexes:
        if not index.name:
            continue
        columns = tuple(
            expression.name
            for expression in index.expressions
            if getattr(expression, "name", None) is not None
        )
        where = index.dialect_options["sqlite"].get("where")
        signatures[index.name] = SQLiteIndexSignature(
            name=index.name,
            columns=columns,
            unique=bool(index.unique),
            where=normalise_sql_expression(where),
        )
    for signature in MIGRATION_INDEX_SIGNATURES.get(table.name, ()):
        signatures[signature.name] = SQLiteIndexSignature(
            name=signature.name,
            columns=signature.columns,
            unique=signature.unique,
            where=normalise_sql_expression(signature.where),
        )
    return signatures


def sqlite_index_matches(conn, table_name: str, expected: SQLiteIndexSignature) -> bool:
    live = live_sqlite_indexes(conn, table_name).get(expected.name)
    if live is None:
        return False
    return live == SQLiteIndexSignature(
        expected.name,
        expected.columns,
        expected.unique,
        normalise_sql_expression(expected.where),
    )


def _expected_foreign_keys(table) -> set[tuple]:
    expected: set[tuple] = set()
    for constraint in table.foreign_key_constraints:
        elements = list(constraint.elements)
        expected.add(
            (
                tuple(element.parent.name for element in elements),
                elements[0].column.table.name,
                tuple(element.column.name for element in elements),
                (constraint.ondelete or "NO ACTION").upper(),
                (constraint.onupdate or "NO ACTION").upper(),
            )
        )
    return expected


def _live_foreign_keys(conn, table_name: str) -> set[tuple]:
    rows = conn.exec_driver_sql(
        f"PRAGMA foreign_key_list({_quote_identifier(table_name)})"
    ).fetchall()
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    live: set[tuple] = set()
    for group in grouped.values():
        ordered = sorted(group, key=lambda row: row[1])
        live.add(
            (
                tuple(row[3] for row in ordered),
                ordered[0][2],
                tuple(row[4] for row in ordered),
                (ordered[0][6] or "NO ACTION").upper(),
                (ordered[0][5] or "NO ACTION").upper(),
            )
        )
    return live


def _expected_checks(table) -> set[tuple[str | None, str | None]]:
    return {
        (constraint.name, normalise_sql_expression(constraint.sqltext))
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _live_checks(inspector, table_name: str) -> set[tuple[str | None, str | None]]:
    return {
        (row.get("name"), normalise_sql_expression(row.get("sqltext")))
        for row in inspector.get_check_constraints(table_name)
    }


def _expected_unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _master_generation_guards_present(conn) -> bool:
    rows = conn.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN "
        "('companies_generation_required_insert', "
        "'companies_generation_immutable')"
    ).fetchall()
    trigger_sql = {
        row[0]: normalise_sql_expression(row[1]) or "" for row in rows
    }
    required = trigger_sql.get("companies_generation_required_insert", "")
    immutable = trigger_sql.get("companies_generation_immutable", "")
    return (
        "before insert on companies" in required
        and "new.generation_id is null" in required
        and "new.generation_id = ''" in required
        and "raise(abort, 'company generation_id is required')" in required
        and "before update of generation_id on companies" in immutable
        and "new.generation_id is not old.generation_id" in immutable
        and "raise(abort, 'company generation_id is immutable')" in immutable
    )


def _master_company_id_guards_present(conn) -> bool:
    rows = conn.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN "
        "('companies_id_required_insert', 'companies_id_required_update')"
    ).fetchall()
    trigger_sql = {
        row[0]: normalise_sql_expression(row[1]) or "" for row in rows
    }
    required = trigger_sql.get("companies_id_required_insert", "")
    update = trigger_sql.get("companies_id_required_update", "")
    return (
        "before insert on companies" in required
        and "new.id is null" in required
        and "raise(abort, 'company id is required')" in required
        and "before update of id on companies" in update
        and "new.id is null" in update
        and "raise(abort, 'company id is required')" in update
    )


def table_signature_mismatches(
    conn, inspector, table, *, include_indexes: bool = True
) -> list[str]:
    """Compare one model table to its complete enforcement signature."""

    mismatches: list[str] = []
    table_name = table.name
    pragma_columns = conn.exec_driver_sql(
        f"PRAGMA table_info({_quote_identifier(table_name)})"
    ).fetchall()
    live_by_name = {row[1]: row for row in pragma_columns}

    expected_pk = tuple(column.name for column in table.primary_key.columns)
    live_pk = tuple(
        row[1] for row in sorted(pragma_columns, key=lambda item: item[5]) if row[5]
    )
    if expected_pk != live_pk:
        mismatches.append(f"primary key mismatch model={expected_pk!r} db={live_pk!r}")

    generation_guards = (
        table_name == "companies" and _master_generation_guards_present(conn)
    )
    company_id_guards = (
        table_name == "companies" and _master_company_id_guards_present(conn)
    )
    for column in table.columns:
        live = live_by_name.get(column.name)
        if live is None:
            continue
        expected_notnull = not column.nullable
        live_notnull = bool(live[3])
        integer_pk_equivalent = (
            not live_notnull
            and live_pk == (column.name,)
            and _normalise_type(str(live[2]).upper()) == "INTEGER"
        )
        nullable_guard_equivalent = (
            generation_guards
            and column.name == "generation_id"
            and expected_notnull
            and not live_notnull
        )
        nullable_guard_equivalent = nullable_guard_equivalent or (
            company_id_guards
            and column.name == "id"
            and expected_notnull
            and not live_notnull
        )
        if (
            expected_notnull != live_notnull
            and not nullable_guard_equivalent
            and not integer_pk_equivalent
        ):
            mismatches.append(
                f"{column.name} NOT NULL mismatch "
                f"model={expected_notnull} db={live_notnull}"
            )
        live_default = _normalise_default(live[4])
        accepted_defaults = _accepted_model_defaults(column)
        if live_default not in accepted_defaults:
            mismatches.append(
                f"{column.name} default mismatch "
                f"model={sorted(map(str, accepted_defaults))!r} db={live_default!r}"
            )

    expected_fks = _expected_foreign_keys(table)
    live_fks = _live_foreign_keys(conn, table_name)
    if expected_fks != live_fks:
        mismatches.append(
            f"foreign keys mismatch model={sorted(expected_fks)!r} "
            f"db={sorted(live_fks)!r}"
        )

    expected_checks = _expected_checks(table)
    live_checks = _live_checks(inspector, table_name)
    if expected_checks != live_checks:
        mismatches.append(
            f"CHECK constraints mismatch model={sorted(expected_checks)!r} "
            f"db={sorted(live_checks)!r}"
        )

    if include_indexes:
        expected_indexes = model_sqlite_indexes(table)
        live_indexes = live_sqlite_indexes(conn, table_name)
        for name, expected in sorted(expected_indexes.items()):
            live = live_indexes.get(name)
            if live is None:
                mismatches.append(f"missing index {name}: expected {expected!r}")
            elif live != expected:
                mismatches.append(
                    f"index {name} mismatch model={expected!r} db={live!r}"
                )
        expected_named = set(expected_indexes)
        unexpected_named = sorted(
            name
            for name in live_indexes
            if not name.startswith("sqlite_autoindex_") and name not in expected_named
        )
        if unexpected_named:
            mismatches.append(f"unexpected indexes {unexpected_named!r}")

        expected_unique = _expected_unique_columns(table)
        expected_unique.update(
            signature.columns
            for signature in expected_indexes.values()
            if signature.unique and signature.where is None
        )
        live_unique = {
            signature.columns
            for signature in live_indexes.values()
            if signature.unique and signature.where is None
        }
        # PRAGMA index_list exposes WITHOUT-ROWID/text/composite primary-key
        # autoindexes as unique indexes. PK enforcement is compared separately.
        if live_pk:
            live_unique.discard(live_pk)
        if expected_unique != live_unique:
            mismatches.append(
                f"UNIQUE columns mismatch model={sorted(expected_unique)!r} "
                f"db={sorted(live_unique)!r}"
            )
    return mismatches


def detect_drift(engine: Engine, base: type[DeclarativeBase], scope: str) -> SchemaDriftReport:
    """Compare ORM metadata and full SQLite signatures to the live DB.

    `sync_missing_columns` is expected to have run first, so a clean report
    after both calls means every required column and enforcement constraint is
    present. Anything left over needs a hand-rolled migration or operator-led
    recovery; startup must not continue past a non-clean report.
    """
    report = SchemaDriftReport(scope=scope)
    insp = inspect(engine)
    expected_tables = set(base.metadata.tables)
    live_tables = {
        name for name in insp.get_table_names() if not name.startswith("sqlite_")
    }
    report.extra_tables.extend(sorted(live_tables - expected_tables))
    with engine.connect() as conn:
        for table in base.metadata.tables.values():
            if table.name not in live_tables:
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
            td.signature_mismatches.extend(
                table_signature_mismatches(conn, insp, table)
            )
            if not td.is_clean:
                report.tables.append(td)
    return report


def require_clean_schema(report: SchemaDriftReport) -> None:
    """Fail closed before a structurally-drifted database can serve writes."""

    if report.is_clean:
        return
    raise DataRecoveryRequiredError(
        report.format()
        + "\nRestore this database from a verified backup or run an explicit "
        "constraint-repair workflow. Startup refused to continue."
    )


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
