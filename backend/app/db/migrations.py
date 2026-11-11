"""Per-company schema migrations that reflection cannot handle.

Additive column changes are handled automatically by `schema_sync.sync_missing_columns`
(called from startup). This module covers only the things SQLite + reflection
can't do:

  - drop columns
  - rebuild a table to change CHECK constraints / FK ondelete rules
  - create partial / functional indexes
  - data backfills

Each step is idempotent: rerunning on an already-migrated DB is a no-op.

Why not Alembic: one SQLite file per company; we prefer deploy-simplicity
(zero migration files, schema is the model + this short list) over the
linear-history bookkeeping Alembic provides. Re-evaluate if any of:
  - a step needs data backfill more complex than a one-liner
  - the rebuild list grows past ~5 entries
  - we hit a bug caused by silent drift the startup check didn't catch
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Generator

from sqlalchemy import event, inspect, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.engine import Engine

from .base import CompanyBase
from .errors import DataRecoveryRequiredError
from .schema_sync import (
    MIGRATION_INDEX_SIGNATURES,
    SQLiteIndexSignature,
    live_sqlite_indexes,
    model_sqlite_indexes,
    sqlite_index_matches,
    table_signature_mismatches,
)
from ..services.account_invariants import SYSTEM_ACCOUNT_SPECS

# Tables that have a supported full rebuild. Need is decided from the complete
# model/live constraint signature rather than the historical marker substring.
# Every run compares the live PK/NULL/default/FK/CHECK signature to the model.
TABLE_REBUILDS: tuple[str, ...] = ("bank_transactions",)


# (table_name, column_name) for columns that were removed from the schema.
# Applied via SQLite's ALTER TABLE ... DROP COLUMN (3.35+); SQLite shipped
# with Python 3.11 (>=3.37) supports this.
COMPANY_DB_COLUMN_DROPS: list[tuple[str, str]] = [
    ("service_agreements", "dha_charges_note"),
    ("service_agreements", "other_costs_note"),
    # Removed when SA visa items got per-item `item_type`; the SA-level
    # service_kind became redundant and is no longer in the model.
    ("outgoing_documents", "sa_service_kind"),
    # Removed when the discount feature was rolled back (audit P0).
    ("outgoing_documents", "sa_discount"),
    # Trust model removed: accounting is a single plain bank account now.
    # `kind` (trust/business/savings) is gone.
    #
    # NOTE: bank_transactions.linked_trust_entry_id is NOT dropped here. Its FK
    # targets trust_ledger_entries, which COMPANY_DB_TABLE_DROPS removes — and
    # SQLite's DROP COLUMN integrity check rejects a column whose FK points at a
    # now-missing table ("unknown column ... in foreign key definition"). It is
    # removed by the bank_transactions rebuild instead (see TABLE_REBUILDS).
    ("bank_accounts", "kind"),
]


# Tables removed from the schema entirely. Each entry includes explicit indexes
# to drop first; DROP TABLE handles table-owned indexes, but naming them keeps
# the migration robust across older SQLite files.
COMPANY_DB_TABLE_DROPS: list[tuple[str, tuple[str, ...]]] = [
    ("closed_periods", ("ix_closed_periods_period_start", "ix_closed_periods_period_end")),
    # Legacy ServiceAgreement table; the SA workflow lives entirely in
    # OutgoingDocument now. All M5 test data has been wiped (confirmed
    # empty across all company DBs), so a plain DROP is safe.
    ("service_agreements", ("uq_sa_number",)),
    # Trust ledger removed with the single-account accounting rebuild.
    ("trust_ledger_entries", ("uq_trust_earned_external_ref", "uq_trust_earned_per_sa")),
    # Partner-documents feature removed with the Receipt-only rewrite. Partner
    # docs lived in outgoing_documents (partner_ref_id → partners); that column
    # is dropped by the outgoing_documents rebuild below (step 3b), so this DROP
    # only needs FKs off (handled by run_company_migrations step 1).
    ("partners", ("ix_partners_display_name",)),
]


# Columns removed from outgoing_documents when the document model was reduced to
# Receipt-only (Service Agreement / Payment Request / outgoing-Invoice / Partner
# workflows deleted). Several are self-FKs or FK to the dropped `partners` table,
# so they can't go via plain DROP COLUMN — the outgoing_documents rebuild (step
# 3b) removes them. Presence of any one triggers the rebuild.
REMOVED_OUTDOC_COLUMNS: frozenset[str] = frozenset(
    {
        "expiration_date",
        "service_agreement_id",
        "voided_by_sa_id",
        "partner_ref_id",
        "related_doc_id",
        "instalment_index",
        "staff_member_id",
        "sa_applicants",
        "sa_visa_items",
        "sa_payment_sched",
        "gst_inclusive",
    }
)


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name = :t"),
        {"t": table},
    ).fetchone()
    return row is not None


def _table_sql(conn, table: str) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name = :t"),
        {"t": table},
    ).fetchone()
    return (row[0] if row else "") or ""


def _index_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name = :n"),
        {"n": name},
    ).fetchone()
    return row is not None


def _sqlite_invalid_iso_date(value_sql: str) -> str:
    return (
        f"(typeof({value_sql}) != 'text' OR length({value_sql}) != 10 "
        f"OR {value_sql} NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        f"OR substr({value_sql}, 1, 4) NOT BETWEEN '0001' AND '9999' "
        f"OR strftime('%Y-%m-%d', {value_sql}, '+0 days') IS NOT {value_sql})"
    )


def _ensure_gst_adjustment_append_only_guards(conn) -> bool:
    """Install or repair canonical append-only and GST-domain triggers."""
    from ..models.company import GST_ADJUSTMENT_MAX_CENTS

    tables = (
        "gst_adjustment_events",
        "gst_adjustment_tax_slices",
        "gst_adjustment_finalizations",
        "gst_adjustment_evidence",
        "gst_adjustment_manual_reviews",
    )
    changed = False
    if _table_exists(conn, "gst_adjustment_events") and (
        "tax_slice_count" not in _existing_columns(conn, "gst_adjustment_events")
    ):
        conn.execute(
            text(
                "ALTER TABLE gst_adjustment_events ADD COLUMN tax_slice_count "
                "INTEGER NOT NULL DEFAULT 1"
            )
        )
        changed = True
    if _table_exists(conn, "gst_adjustment_events"):
        event_columns = _existing_columns(conn, "gst_adjustment_events")
        for column, column_type in (
            ("lifecycle_operation_type", "VARCHAR(50)"),
            ("lifecycle_operation_id", "INTEGER"),
        ):
            if column not in event_columns:
                conn.execute(
                    text(
                        f"ALTER TABLE gst_adjustment_events ADD COLUMN {column} "
                        f"{column_type}"
                    )
                )
                changed = True

    for table in tables:
        if not _table_exists(conn, table):
            continue

        for operation in ("UPDATE", "DELETE"):
            name = f"trg_{table}_no_{operation.lower()}"
            sql = (
                f'CREATE TRIGGER "{name}" BEFORE {operation} ON "{table}" '
                "BEGIN SELECT RAISE(ABORT, 'GST adjustment records are append-only'); END"
            )
            changed = _ensure_trigger(conn, name, sql) or changed

        if table == "gst_adjustment_finalizations":
            insert_conditions = [
                "EXISTS (SELECT 1 FROM gst_adjustment_finalizations "
                "WHERE event_id = NEW.event_id)"
            ]
        else:
            insert_conditions = [
                f'EXISTS (SELECT 1 FROM "{table}" WHERE id = NEW.id)'
            ]
        if table == "gst_adjustment_events":
            insert_conditions.append(
                "(NEW.reversal_of_event_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM gst_adjustment_events "
                "WHERE reversal_of_event_id = NEW.reversal_of_event_id))"
            )
        elif table == "gst_adjustment_tax_slices":
            insert_conditions.append(
                "EXISTS (SELECT 1 FROM gst_adjustment_tax_slices "
                "WHERE event_id = NEW.event_id AND tax_code = NEW.tax_code)"
            )
        insert_trigger = f"trg_{table}_no_replace_insert"
        insert_sql = (
            f'''CREATE TRIGGER "{insert_trigger}"
            BEFORE INSERT ON "{table}"
            WHEN {" OR ".join(insert_conditions)}
            BEGIN
                SELECT RAISE(ABORT, 'GST adjustment records are append-only');
            END'''
        )
        changed = _ensure_trigger(conn, insert_trigger, insert_sql) or changed

    if _table_exists(conn, "gst_adjustment_events"):
        domain_trigger = "trg_gst_adjustment_events_domain"
        effective_date_invalid = _sqlite_invalid_iso_date("NEW.effective_date")
        awareness_date_invalid = _sqlite_invalid_iso_date("NEW.awareness_date")
        agreement_date_invalid = _sqlite_invalid_iso_date("NEW.agreement_date")
        refund_date_invalid = _sqlite_invalid_iso_date("NEW.refund_repayment_date")
        note_held_date_invalid = _sqlite_invalid_iso_date("NEW.adjustment_note_held_date")
        domain_sql = f'''CREATE TRIGGER "{domain_trigger}"
            BEFORE INSERT ON "gst_adjustment_events"
            WHEN length(trim(COALESCE(NEW.event_type, ''), char(9) || char(10) || char(13) || ' ')) = 0
                            OR NEW.event_type NOT IN ('agreement', 'refund_repayment', 'adjustment_note', 'reversal')
              OR NEW.source_direction NOT IN ('AR', 'AP')
              OR NEW.adjustment_direction NOT IN ('increasing', 'decreasing')
              OR NEW.projection_box NOT IN ('1A', '1B')
              OR NEW.tax_code NOT IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none', 'mixed')
              OR length(trim(COALESCE(NEW.tax_code, ''), char(9) || char(10) || char(13) || ' ')) = 0
              OR NEW.policy_version NOT IN ('F-02-v1')
              OR length(trim(COALESCE(NEW.reason, ''), char(9) || char(10) || char(13) || ' ')) = 0
              OR typeof(NEW.amount_cents) != 'integer'
              OR NEW.amount_cents < 1 OR NEW.amount_cents > {GST_ADJUSTMENT_MAX_CENTS}
              OR typeof(NEW.gst_amount_cents) != 'integer'
              OR NEW.gst_amount_cents < 0 OR NEW.gst_amount_cents > NEW.amount_cents
              OR NEW.gst_amount_cents > {GST_ADJUSTMENT_MAX_CENTS}
              OR NEW.manual_review_status NOT IN ('not_required', 'pending', 'approved', 'rejected')
              OR typeof(NEW.tax_slice_count) != 'integer' OR NEW.tax_slice_count < 1
              OR (NEW.tax_code = 'mixed' AND NEW.tax_slice_count < 2)
              OR (NEW.tax_code != 'mixed' AND NEW.tax_slice_count != 1)
                            OR NEW.source_record_type IS NULL
                            OR length(trim(COALESCE(NEW.source_record_type, ''), char(9) || char(10) || char(13) || ' ')) = 0
                            OR NEW.source_record_type NOT IN ('credit_note')
                            OR typeof(NEW.source_record_id) != 'integer'
                            OR NEW.source_record_id <= 0
              OR ((NEW.lifecycle_operation_type IS NULL) != (NEW.lifecycle_operation_id IS NULL))
              OR (NEW.lifecycle_operation_type IS NOT NULL AND
                  NEW.lifecycle_operation_type NOT IN ('credit_note_ap', 'credit_note_void_ap'))
              OR (NEW.lifecycle_operation_id IS NOT NULL AND (
                  typeof(NEW.lifecycle_operation_id) != 'integer'
                  OR NEW.lifecycle_operation_id <= 0
              ))
              OR ((NEW.event_type = 'reversal') != (NEW.reversal_of_event_id IS NOT NULL))
              OR ((NEW.adjustment_note_reference IS NULL) != (NEW.adjustment_note_held_date IS NULL))
              OR (NEW.adjustment_note_reference IS NOT NULL AND
                  length(trim(NEW.adjustment_note_reference, char(9) || char(10) || char(13) || ' ')) = 0)
              OR {effective_date_invalid}
              OR (NEW.awareness_date IS NOT NULL AND (
                  {awareness_date_invalid}
                    OR julianday(NEW.awareness_date) < julianday(NEW.effective_date)
                 ))
              OR (NEW.agreement_date IS NOT NULL AND (
                  {agreement_date_invalid}
                    OR julianday(NEW.agreement_date) < julianday(NEW.effective_date)
                 ))
              OR (NEW.refund_repayment_date IS NOT NULL AND (
                  {refund_date_invalid}
                    OR julianday(NEW.refund_repayment_date) < julianday(NEW.effective_date)
                 ))
              OR (NEW.adjustment_note_held_date IS NOT NULL AND (
                  {note_held_date_invalid}
                    OR julianday(NEW.adjustment_note_held_date) < julianday(NEW.effective_date)
                 ))
              OR (NEW.tax_code IN ('gst_free', 'input_taxed', 'none') AND NEW.gst_amount_cents != 0)
              OR (NEW.gst_amount_cents = 0 AND NEW.tax_code NOT IN ('gst_free', 'input_taxed', 'none', 'mixed'))
            BEGIN
                SELECT RAISE(ABORT, 'Invalid GST adjustment event');
            END'''
        changed = _ensure_trigger(conn, domain_trigger, domain_sql) or changed

        reversal_trigger = "trg_gst_adjustment_events_reversal_opposite"
        same_box_journal_condition = "0"
        if _table_exists(conn, "journal_entries"):
            same_box_journal_condition = """EXISTS (
                SELECT 1 FROM journal_entries AS original_journal
                JOIN journal_entries AS void_journal
                  ON void_journal.id = NEW.lifecycle_operation_id
                WHERE original_journal.id = original.lifecycle_operation_id
                  AND original_journal.source_type = 'credit_note_ap'
                  AND original_journal.source_id = original.source_record_id
                  AND original_journal.entry_date = original.effective_date
                  AND void_journal.source_type = 'credit_note_void_ap'
                  AND void_journal.source_id = NEW.source_record_id
                  AND void_journal.reverses_entry_id = original_journal.id
                  AND void_journal.entry_date = NEW.effective_date
            )"""
        no_active_application_condition = (
            "NOT EXISTS (SELECT 1 FROM credit_note_applications "
            "WHERE credit_note_id = original.source_record_id AND status = 'active')"
            if _table_exists(conn, "credit_note_applications")
            else "0"
        )
        no_active_refund_condition = (
            "NOT EXISTS (SELECT 1 FROM credit_note_refunds "
            "WHERE credit_note_id = original.source_record_id AND status = 'active')"
            if _table_exists(conn, "credit_note_refunds")
            else "0"
        )
        same_box_exception = f"""(
            original.projection_box = '1A'
            AND NEW.projection_box = '1A'
            AND original.source_direction = 'AP'
            AND NEW.source_direction = 'AP'
            AND original.lifecycle_operation_type = 'credit_note_ap'
            AND NEW.lifecycle_operation_type = 'credit_note_void_ap'
            AND original.lifecycle_operation_id IS NOT NULL
            AND NEW.lifecycle_operation_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM gst_adjustment_events AS prior_reversal
                WHERE prior_reversal.reversal_of_event_id = original.id
            )
            AND {same_box_journal_condition}
            AND {no_active_application_condition}
            AND {no_active_refund_condition}
        )"""
        if _table_exists(conn, "gst_adjustment_finalizations"):
            reversal_sql = f'''CREATE TRIGGER "{reversal_trigger}"
                BEFORE INSERT ON "gst_adjustment_events"
                WHEN NEW.reversal_of_event_id IS NOT NULL
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM gst_adjustment_events AS original
                        WHERE original.id = NEW.reversal_of_event_id
                          AND original.reversal_of_event_id IS NULL
                          AND original.source_direction = NEW.source_direction
                          AND original.adjustment_direction <> NEW.adjustment_direction
                         AND (original.projection_box <> NEW.projection_box
                             OR {same_box_exception})
                          AND original.amount_cents = NEW.amount_cents
                          AND original.gst_amount_cents = NEW.gst_amount_cents
                          AND original.tax_code = NEW.tax_code
                          AND original.policy_version = NEW.policy_version
                          AND original.source_record_type IS NEW.source_record_type
                          AND original.source_record_id IS NEW.source_record_id
                          AND original.tax_slice_count = NEW.tax_slice_count
                          AND EXISTS (
                              SELECT 1 FROM gst_adjustment_finalizations
                              WHERE event_id = original.id
                          )
                          AND julianday(NEW.effective_date) >= julianday(original.effective_date)
                    ) THEN RAISE(ABORT, 'GST adjustment reversal must be a full opposite linked event') END;
                END'''
        else:
            reversal_sql = f'''CREATE TRIGGER "{reversal_trigger}"
                BEFORE INSERT ON "gst_adjustment_events"
                WHEN NEW.reversal_of_event_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'GST adjustment finalization schema is unavailable');
                END'''
        changed = _ensure_trigger(conn, reversal_trigger, reversal_sql) or changed

    if _table_exists(conn, "gst_adjustment_tax_slices") and _table_exists(
        conn, "gst_adjustment_events"
    ):
        slice_trigger = "trg_gst_adjustment_tax_slices_domain"
        slice_sql = f'''CREATE TRIGGER "{slice_trigger}"
            BEFORE INSERT ON "gst_adjustment_tax_slices"
            WHEN length(trim(COALESCE(NEW.tax_code, ''))) = 0
              OR NEW.tax_code NOT IN ('standard', 'gst_free', 'input_taxed', 'capital', 'none')
              OR typeof(NEW.amount_cents) != 'integer'
              OR NEW.amount_cents < 1 OR NEW.amount_cents > {GST_ADJUSTMENT_MAX_CENTS}
              OR typeof(NEW.gst_amount_cents) != 'integer'
              OR NEW.gst_amount_cents < 0 OR NEW.gst_amount_cents > NEW.amount_cents
              OR NEW.gst_amount_cents > {GST_ADJUSTMENT_MAX_CENTS}
              OR (NEW.tax_code IN ('gst_free', 'input_taxed', 'none') AND NEW.gst_amount_cents != 0)
              OR (NEW.gst_amount_cents = 0 AND NEW.tax_code NOT IN ('gst_free', 'input_taxed', 'none'))
              OR NOT EXISTS (
                    SELECT 1 FROM gst_adjustment_events AS parent
                    WHERE parent.id = NEW.event_id
                                            AND (parent.tax_code = 'mixed' OR (
                                                        parent.tax_slice_count = 1 AND parent.tax_code = NEW.tax_code
                                            ))
                                            AND parent.tax_slice_count >= 1
                      AND (SELECT COUNT(*) FROM gst_adjustment_tax_slices
                           WHERE event_id = NEW.event_id) < parent.tax_slice_count
                      AND (parent.reversal_of_event_id IS NULL OR EXISTS (
                            SELECT 1 FROM gst_adjustment_tax_slices AS original_slice
                            WHERE original_slice.event_id = parent.reversal_of_event_id
                              AND original_slice.tax_code = NEW.tax_code
                              AND original_slice.amount_cents = NEW.amount_cents
                              AND original_slice.gst_amount_cents = NEW.gst_amount_cents
                      ))
              )
            BEGIN
                SELECT RAISE(ABORT, 'Invalid or unreconciled GST adjustment tax slice');
            END'''
        changed = _ensure_trigger(conn, slice_trigger, slice_sql) or changed

    if all(
        _table_exists(conn, table)
        for table in (
            "gst_adjustment_events",
            "gst_adjustment_tax_slices",
            "gst_adjustment_finalizations",
        )
    ):
        finalization_trigger = "trg_gst_adjustment_finalizations_validate"
        review_eligibility = (
            "((parent.manual_review_status = 'not_required' AND NOT EXISTS ("
            "SELECT 1 FROM gst_adjustment_manual_reviews WHERE event_id = parent.id)) "
            "OR (parent.manual_review_status = 'pending' AND "
            "(SELECT status FROM gst_adjustment_manual_reviews "
            "WHERE event_id = parent.id ORDER BY id DESC LIMIT 1) = 'approved'))"
            if _table_exists(conn, "gst_adjustment_manual_reviews")
            else "0"
        )
        lifecycle_operation_eligibility = """(
                        (parent.lifecycle_operation_type IS NULL AND parent.lifecycle_operation_id IS NULL)
                        OR (
                                parent.lifecycle_operation_type = 'credit_note_ap'
                                AND parent.event_type != 'reversal'
                                AND parent.source_direction = 'AP'
                                AND (SELECT COUNT(*) FROM journal_entries AS posting_journal
                                         WHERE posting_journal.id = parent.lifecycle_operation_id
                                             AND posting_journal.source_type = 'credit_note_ap'
                                             AND posting_journal.source_id = parent.source_record_id
                                             AND posting_journal.entry_date = parent.effective_date) = 1
                                AND (SELECT COUNT(*) FROM journal_entries AS matching_postings
                                         WHERE matching_postings.source_type = 'credit_note_ap'
                                             AND matching_postings.source_id = parent.source_record_id) = 1
                        )
                        OR (
                                parent.lifecycle_operation_type = 'credit_note_void_ap'
                                AND parent.event_type = 'reversal'
                                AND parent.source_direction = 'AP'
                                AND EXISTS (
                                        SELECT 1 FROM gst_adjustment_events AS original
                                        JOIN gst_adjustment_finalizations AS original_finalization
                                            ON original_finalization.event_id = original.id
                                        JOIN journal_entries AS posting_journal
                                            ON posting_journal.id = original.lifecycle_operation_id
                                        JOIN journal_entries AS void_journal
                                            ON void_journal.id = parent.lifecycle_operation_id
                                        WHERE original.id = parent.reversal_of_event_id
                                            AND original.reversal_of_event_id IS NULL
                                            AND original.source_direction = 'AP'
                                            AND original.lifecycle_operation_type = 'credit_note_ap'
                                            AND posting_journal.source_type = 'credit_note_ap'
                                            AND posting_journal.source_id = original.source_record_id
                                            AND posting_journal.entry_date = original.effective_date
                                            AND void_journal.source_type = 'credit_note_void_ap'
                                            AND void_journal.source_id = parent.source_record_id
                                            AND void_journal.reverses_entry_id = posting_journal.id
                                            AND void_journal.entry_date = parent.effective_date
                                            AND (SELECT COUNT(*) FROM journal_entries AS matching_postings
                                                     WHERE matching_postings.source_type = 'credit_note_ap'
                                                         AND matching_postings.source_id = parent.source_record_id) = 1
                                            AND (SELECT COUNT(*) FROM journal_entries AS matching_voids
                                                     WHERE matching_voids.source_type = 'credit_note_void_ap'
                                                         AND matching_voids.source_id = parent.source_record_id
                                                         AND matching_voids.reverses_entry_id = posting_journal.id) = 1
                                )
                                AND NOT EXISTS (
                                        SELECT 1 FROM credit_note_applications
                                        WHERE credit_note_id = parent.source_record_id AND status = 'active'
                                )
                                AND NOT EXISTS (
                                        SELECT 1 FROM credit_note_refunds
                                        WHERE credit_note_id = parent.source_record_id AND status = 'active'
                                )
                        )
                )""" if _table_exists(conn, "journal_entries") else "0"
        finalization_sql = f'''CREATE TRIGGER "{finalization_trigger}"
            BEFORE INSERT ON "gst_adjustment_finalizations"
            WHEN NOT EXISTS (
                SELECT 1 FROM gst_adjustment_events AS parent
                WHERE parent.id = NEW.event_id
                  AND NEW.tax_slice_count = parent.tax_slice_count
                  AND NEW.amount_cents = parent.amount_cents
                  AND NEW.gst_amount_cents = parent.gst_amount_cents
                                    AND parent.source_record_type IS NOT NULL
                                    AND length(trim(COALESCE(parent.source_record_type, ''), char(9) || char(10) || char(13) || ' ')) > 0
                                    AND parent.source_record_type IN ('credit_note')
                                    AND typeof(parent.source_record_id) = 'integer'
                                    AND parent.source_record_id > 0
                                      AND {lifecycle_operation_eligibility}
                  AND {review_eligibility}
                  AND (SELECT COUNT(*) FROM gst_adjustment_tax_slices
                       WHERE event_id = parent.id) = parent.tax_slice_count
                   AND (SELECT COALESCE(SUM(amount_cents), 0)
                       FROM gst_adjustment_tax_slices
                       WHERE event_id = parent.id) = parent.amount_cents
                   AND (SELECT COALESCE(SUM(gst_amount_cents), 0)
                       FROM gst_adjustment_tax_slices
                       WHERE event_id = parent.id) = parent.gst_amount_cents
                  AND (parent.reversal_of_event_id IS NULL OR (
                      EXISTS (SELECT 1 FROM gst_adjustment_finalizations
                              WHERE event_id = parent.reversal_of_event_id)
                      AND NOT EXISTS (
                          SELECT 1 FROM gst_adjustment_tax_slices AS original_slice
                          LEFT JOIN gst_adjustment_tax_slices AS reversal_slice
                            ON reversal_slice.event_id = parent.id
                           AND reversal_slice.tax_code = original_slice.tax_code
                           AND reversal_slice.amount_cents = original_slice.amount_cents
                           AND reversal_slice.gst_amount_cents = original_slice.gst_amount_cents
                          WHERE original_slice.event_id = parent.reversal_of_event_id
                            AND reversal_slice.id IS NULL
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM gst_adjustment_tax_slices AS reversal_slice
                          LEFT JOIN gst_adjustment_tax_slices AS original_slice
                            ON original_slice.event_id = parent.reversal_of_event_id
                           AND original_slice.tax_code = reversal_slice.tax_code
                           AND original_slice.amount_cents = reversal_slice.amount_cents
                           AND original_slice.gst_amount_cents = reversal_slice.gst_amount_cents
                          WHERE reversal_slice.event_id = parent.id
                            AND original_slice.id IS NULL
                      )
                  ))
            )
            BEGIN
                SELECT RAISE(ABORT, 'GST adjustment slices are incomplete or unreconciled');
            END'''
        changed = _ensure_trigger(conn, finalization_trigger, finalization_sql) or changed

    if _table_exists(conn, "gst_adjustment_evidence"):
        evidence_trigger = "trg_gst_adjustment_evidence_domain"
        evidence_sql = f'''CREATE TRIGGER "{evidence_trigger}"
            BEFORE INSERT ON "gst_adjustment_evidence"
                        WHEN length(trim(COALESCE(NEW.evidence_type, ''), char(9) || char(10) || char(13) || ' ')) = 0
                            OR NEW.evidence_type NOT IN ('document')
                            OR length(trim(COALESCE(NEW.evidence_reference, ''), char(9) || char(10) || char(13) || ' ')) = 0
              OR (NEW.content_sha256 IS NOT NULL AND (
                    length(NEW.content_sha256) != 64
                    OR lower(NEW.content_sha256) GLOB '*[^0-9a-f]*'
                 ))
            BEGIN
                SELECT RAISE(ABORT, 'Invalid GST adjustment evidence');
            END'''
        changed = _ensure_trigger(conn, evidence_trigger, evidence_sql) or changed

    if _table_exists(conn, "gst_adjustment_manual_reviews"):
        review_trigger = "trg_gst_adjustment_manual_reviews_domain"
        review_parent_condition = (
            "EXISTS (SELECT 1 FROM gst_adjustment_events "
            "WHERE id = NEW.event_id AND manual_review_status = 'pending')"
            if _table_exists(conn, "gst_adjustment_events")
            else "0"
        )
        terminal_review_condition = (
            "EXISTS (SELECT 1 FROM gst_adjustment_manual_reviews "
            "WHERE event_id = NEW.event_id AND status IN ('approved', 'rejected'))"
        )
        finalized_event_condition = (
            "EXISTS (SELECT 1 FROM gst_adjustment_finalizations "
            "WHERE event_id = NEW.event_id)"
            if _table_exists(conn, "gst_adjustment_finalizations")
            else "0"
        )
        review_sql = f'''CREATE TRIGGER "{review_trigger}"
            BEFORE INSERT ON "gst_adjustment_manual_reviews"
            WHEN NEW.status NOT IN ('pending', 'approved', 'rejected')
                            OR NOT ({review_parent_condition})
                            OR {terminal_review_condition}
              OR {finalized_event_condition}
              OR length(trim(COALESCE(NEW.reason, ''), char(9) || char(10) || char(13) || ' ')) = 0
              OR (NEW.reviewer IS NOT NULL AND
                  length(trim(NEW.reviewer, char(9) || char(10) || char(13) || ' ')) = 0)
              OR (NEW.status = 'pending' AND NEW.reviewed_at IS NOT NULL)
                  OR (NEW.status IN ('approved', 'rejected') AND (
                          NEW.reviewed_at IS NULL
                          OR length(NEW.reviewed_at) != 19
                          OR NEW.reviewed_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
                          OR strftime('%Y-%m-%d %H:%M:%S', NEW.reviewed_at) IS NULL
                          OR strftime('%Y-%m-%d %H:%M:%S', NEW.reviewed_at, '+0 seconds') IS NOT NEW.reviewed_at
                      ))
            BEGIN
                SELECT RAISE(ABORT, 'Invalid GST adjustment manual review');
            END'''
        changed = _ensure_trigger(conn, review_trigger, review_sql) or changed

    if (
        _table_exists(conn, "journal_entries")
        and _table_exists(conn, "gst_adjustment_events")
        and _table_exists(conn, "gst_adjustment_finalizations")
    ):
        referenced_journal = """EXISTS (
            SELECT 1 FROM gst_adjustment_events AS event
            JOIN gst_adjustment_finalizations AS finalization
              ON finalization.event_id = event.id
            WHERE event.lifecycle_operation_id = OLD.id
              AND event.lifecycle_operation_type = OLD.source_type
              AND event.source_record_id = OLD.source_id
        )"""
        journal_update_sql = f'''CREATE TRIGGER "trg_journal_entries_gst_adjustment_no_provenance_update"
                        BEFORE UPDATE OF id, source_type, source_id, reverses_entry_id, entry_date
            ON journal_entries
            WHEN {referenced_journal}
                            AND (OLD.id IS NOT NEW.id
                                     OR OLD.source_type IS NOT NEW.source_type
                   OR OLD.source_id IS NOT NEW.source_id
                   OR OLD.reverses_entry_id IS NOT NEW.reverses_entry_id
                   OR OLD.entry_date IS NOT NEW.entry_date)
            BEGIN
                SELECT RAISE(ABORT, 'Finalized GST adjustment journal provenance is immutable');
            END'''
        changed = _ensure_trigger(
            conn,
            "trg_journal_entries_gst_adjustment_no_provenance_update",
            journal_update_sql,
        ) or changed
        journal_delete_sql = f'''CREATE TRIGGER "trg_journal_entries_gst_adjustment_no_provenance_delete"
            BEFORE DELETE ON journal_entries
            WHEN {referenced_journal}
            BEGIN
                SELECT RAISE(ABORT, 'Finalized GST adjustment journal provenance is immutable');
            END'''
        changed = _ensure_trigger(
            conn,
            "trg_journal_entries_gst_adjustment_no_provenance_delete",
            journal_delete_sql,
        ) or changed
        journal_replace_sql = f'''CREATE TRIGGER "trg_journal_entries_gst_adjustment_no_provenance_replace"
            BEFORE INSERT ON journal_entries
            WHEN EXISTS (
                SELECT 1 FROM journal_entries AS existing
                WHERE (existing.id = NEW.id
                   OR (NEW.source_id IS NOT NULL
                       AND existing.source_type = NEW.source_type
                       AND existing.source_id = NEW.source_id)
                   OR (NEW.reverses_entry_id IS NOT NULL
                       AND existing.reverses_entry_id = NEW.reverses_entry_id))
                  AND EXISTS (
                      SELECT 1 FROM gst_adjustment_events AS event
                      JOIN gst_adjustment_finalizations AS finalization
                        ON finalization.event_id = event.id
                      WHERE event.lifecycle_operation_id = existing.id
                        AND event.lifecycle_operation_type = existing.source_type
                        AND event.source_record_id = existing.source_id
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'Finalized GST adjustment journal provenance is immutable');
            END'''
        changed = _ensure_trigger(
            conn,
            "trg_journal_entries_gst_adjustment_no_provenance_replace",
            journal_replace_sql,
        ) or changed
    return changed


def _normalise_trigger_sql(sql: str) -> str:
    output: list[str] = []
    pending_space = False
    in_string = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if in_string:
            output.append(character)
            if character == "'":
                if index + 1 < len(sql) and sql[index + 1] == "'":
                    output.append("'")
                    index += 1
                else:
                    in_string = False
        elif character == "'":
            if pending_space and output:
                output.append(" ")
            pending_space = False
            in_string = True
            output.append(character)
        elif character.isspace():
            pending_space = True
        else:
            if pending_space and output:
                output.append(" ")
            pending_space = False
            output.append(character.upper())
        index += 1
    return "".join(output).removesuffix(";")


def _ensure_trigger(conn, name: str, canonical_sql: str) -> bool:
    """Compare and restore the complete canonical SQL for one owned trigger."""
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name = :n"),
        {"n": name},
    ).fetchone()
    expected = _normalise_trigger_sql(canonical_sql)
    if row is not None and row[0] and _normalise_trigger_sql(str(row[0])) == expected:
        return False
    if row is not None:
        conn.execute(text(f'DROP TRIGGER "{name}"'))
    conn.execute(text(canonical_sql))
    installed = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name = :n"),
        {"n": name},
    ).scalar_one()
    if _normalise_trigger_sql(str(installed)) != expected:
        raise RuntimeError(f"Could not install canonical owned trigger {name}")
    return True


_ALLOCATION_HISTORY_INDEXES = (
    ("allocation_history_events", SQLiteIndexSignature(
        "uq_allocation_history_idempotency", ("idempotency_key",), unique=True,
        where="idempotency_key IS NOT NULL",
    )),
    ("allocation_history_events", SQLiteIndexSignature(
        "uq_allocation_history_version", ("allocation_id", "version"), unique=True,
        where="version IS NOT NULL",
    )),
    ("allocation_history_events", SQLiteIndexSignature(
        "uq_allocation_history_legacy_unknown", ("bank_transaction_id",), unique=True,
        where="operation = 'legacy_history_unknown'",
    )),
    ("allocation_history_events", SQLiteIndexSignature(
        "uq_allocation_history_reversed_event", ("linked_event_id",), unique=True,
        where="operation = 'allocation_reversal' AND linked_event_id IS NOT NULL",
    )),
    ("allocation_history_events", SQLiteIndexSignature(
        "uq_allocation_history_reversed_allocation", ("linked_allocation_id",), unique=True,
        where="operation = 'allocation_reversal' AND linked_allocation_id IS NOT NULL",
    )),
    ("allocation_history_events", SQLiteIndexSignature(
        "uq_allocation_history_snapshot", ("snapshot_id",), unique=True,
        where="snapshot_id IS NOT NULL",
    )),
    ("allocation_history_tax_components", SQLiteIndexSignature(
        "uq_allocation_history_tax_component", ("snapshot_id", "tax_code"), unique=True,
    )),
    ("allocation_history_baseline_bindings", SQLiteIndexSignature(
        "uq_allocation_history_baseline_allocation",
        ("baseline_event_id", "allocation_row_id"), unique=True,
    )),
    ("allocation_history_baseline_tax_bindings", SQLiteIndexSignature(
        "uq_allocation_history_baseline_component",
        ("allocation_binding_id", "source_component_row_id"), unique=True,
    )),
    ("allocation_history_baseline_tax_bindings", SQLiteIndexSignature(
        "uq_allocation_history_baseline_component_row",
        ("source_component_row_id",), unique=True,
    )),
    ("allocation_history_baseline_finalizations", SQLiteIndexSignature(
        "uq_allocation_history_baseline_finalization_transaction",
        ("bank_transaction_id",), unique=True,
    )),
    ("bas_period_lodgment_markers", SQLiteIndexSignature(
        "uq_bas_period_lodgment_period", ("period_id",), unique=True,
    )),
    ("allocation_period_correction_approvals", SQLiteIndexSignature(
        "uq_allocation_period_correction_approval",
        ("period_marker_id", "approved_idempotency_key"), unique=True,
    )),
)


def allocation_baseline_integrity_hash(rows) -> str:
    """Hash canonical JSON rows used by legacy allocation finalizations."""
    encoded_rows = sorted(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        for row in rows
    )
    payload = json.dumps(encoded_rows, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_ALLOCATION_BASELINE_MAX_CENTS = (1 << 63) - 1


def allocation_baseline_money_to_cents(value) -> int | None:
    """Return exact nonnegative SQLite-safe cents, or NULL for invalid money."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None

    sign, digits, exponent = amount.as_tuple()
    del sign
    if not any(digits):
        return 0

    scaled_exponent = exponent + 2
    digit_text = "".join(str(digit) for digit in digits)
    if scaled_exponent >= 0:
        if len(digit_text) + scaled_exponent > 19:
            return None
        cents = int(digit_text) * (10 ** scaled_exponent)
    else:
        discarded_digits = -scaled_exponent
        if discarded_digits >= len(digit_text):
            return None
        if any(digit != "0" for digit in digits[-discarded_digits:]):
            return None
        retained_digits = digits[:-discarded_digits]
        if len(retained_digits) > 19:
            return None
        cents = int("".join(str(digit) for digit in retained_digits))

    if cents > _ALLOCATION_BASELINE_MAX_CENTS:
        return None
    return cents


class _AllocationBaselineHash:
    def __init__(self):
        self.rows = []

    def step(self, row_json):
        self.rows.append(json.loads(row_json))

    def finalize(self):
        return allocation_baseline_integrity_hash(self.rows)


def _register_allocation_baseline_hash(dbapi_connection, *_):
    dbapi_connection.create_aggregate(
        "allocation_baseline_sha256", 1, _AllocationBaselineHash
    )
    dbapi_connection.create_function(
        "allocation_baseline_cents",
        1,
        allocation_baseline_money_to_cents,
        deterministic=True,
    )


def _install_allocation_baseline_hash(engine: Engine) -> None:
    if getattr(engine, "_allocation_baseline_hash_installed", False):
        return
    event.listen(engine, "checkout", _register_allocation_baseline_hash)
    engine._allocation_baseline_hash_installed = True


def _ensure_allocation_history_schema(conn) -> bool:
    """Create or repair each owned table independently without backfilling data."""
    tables = (
        "allocation_history_snapshots",
        "allocation_history_tax_components",
        "allocation_history_finalizations",
        "bas_period_lodgment_markers",
        "allocation_period_correction_approvals",
        "allocation_history_events",
        "allocation_history_baseline_bindings",
        "allocation_history_baseline_tax_bindings",
        "allocation_history_baseline_finalizations",
    )
    metadata = _load_company_model_tables()
    changed = False
    for name in tables:
        model_table = metadata[name]
        if not _table_exists(conn, name):
            model_table.create(conn, checkfirst=True)
            changed = True
            continue

        existing = _existing_columns(conn, name)
        missing = [column for column in model_table.columns if column.name not in existing]
        if not missing:
            continue
        if any(column.primary_key for column in missing):
            raise DataRecoveryRequiredError(
                f"Cannot repair partial allocation-history table {name}: its primary key "
                "is missing; records were left untouched."
            )
        # New columns stay nullable here: existing rows are preserved as-is,
        # with no attempt to reconstruct facts that the partial schema omitted.
        for column in missing:
            type_sql = column.type.compile(dialect=conn.dialect)
            conn.exec_driver_sql(
                f'ALTER TABLE "{name}" ADD COLUMN "{column.name}" {type_sql}'
            )
        changed = True

    for table, signature in _ALLOCATION_HISTORY_INDEXES:
        if _repair_named_index(conn, table=table, signature=signature):
            changed = True
    return changed


_ALLOCATION_BATCH_INDEXES = (
    SQLiteIndexSignature(
        "uq_allocation_batch_idempotency", ("idempotency_key",), unique=True,
    ),
    SQLiteIndexSignature(
        "uq_allocation_batch_transaction_version",
        ("bank_transaction_id", "transaction_version"), unique=True,
    ),
    SQLiteIndexSignature(
        "uq_allocation_batch_line_order", ("batch_id", "line_order"), unique=True,
    ),
    SQLiteIndexSignature(
        "uq_allocation_batch_line_identity",
        ("batch_id", "source_kind", "invoice_or_bill_id"), unique=True,
    ),
    SQLiteIndexSignature(
        "uq_allocation_batch_component_tax_code",
        ("allocation_batch_line_id", "tax_code"), unique=True,
    ),
)

MIGRATION_INDEX_SIGNATURES["allocation_batches"] = _ALLOCATION_BATCH_INDEXES[:2]
MIGRATION_INDEX_SIGNATURES["allocation_batch_lines"] = _ALLOCATION_BATCH_INDEXES[2:4]
MIGRATION_INDEX_SIGNATURES["allocation_batch_tax_components"] = _ALLOCATION_BATCH_INDEXES[4:]


def _ensure_allocation_batch_schema(conn) -> bool:
    """Create or safely repair empty partial batch tables without backfill."""
    tables = (
        "allocation_batches",
        "allocation_batch_lines",
        "allocation_batch_tax_components",
        "allocation_batch_finalizations",
    )
    metadata = _load_company_model_tables()
    changed = False
    malformed = []
    for name in tables:
        model_table = metadata[name]
        if not _table_exists(conn, name):
            model_table.create(conn, checkfirst=True)
            changed = True
            continue

        existing = _existing_columns(conn, name)
        model_columns = {column.name for column in model_table.columns}
        if existing != model_columns or table_signature_mismatches(
            conn, inspect(conn), model_table, include_indexes=False
        ):
            malformed.append(name)

    if malformed:
        if any(_table_exists(conn, name) and _table_has_rows(conn, name) for name in tables):
            raise DataRecoveryRequiredError(
                "Cannot safely repair populated partial allocation-batch tables; "
                "missing or weakened historic constraints were not inferred."
            )
        _drop_allocation_batch_triggers(conn)
        for name in reversed(tables):
            if name in malformed and _table_exists(conn, name):
                conn.execute(text(f'DROP TABLE "{name}"'))
        for name in tables:
            if name in malformed:
                metadata[name].create(conn, checkfirst=True)
        changed = True

    for signature in _ALLOCATION_BATCH_INDEXES:
        table = {
            "uq_allocation_batch_idempotency": "allocation_batches",
            "uq_allocation_batch_transaction_version": "allocation_batches",
            "uq_allocation_batch_line_order": "allocation_batch_lines",
            "uq_allocation_batch_line_identity": "allocation_batch_lines",
            "uq_allocation_batch_component_tax_code": "allocation_batch_tax_components",
        }[signature.name]
        try:
            changed = _repair_named_index(
                conn, table=table, signature=signature
            ) or changed
        except Exception as exc:
            raise DataRecoveryRequiredError(
                f"Cannot safely repair allocation-batch index {signature.name}; "
                "records were left untouched."
            ) from exc
    return changed


def _ensure_allocation_history_guards(conn) -> bool:
    """Install canonical append-only, domain, finalization, and cash-row guards."""
    tables = (
        "allocation_history_events",
        "allocation_history_snapshots",
        "allocation_history_tax_components",
        "allocation_history_finalizations",
        "allocation_history_baseline_bindings",
        "allocation_history_baseline_tax_bindings",
        "allocation_history_baseline_finalizations",
        "bas_period_lodgment_markers",
        "allocation_period_correction_approvals",
    )
    changed = False
    for table in tables:
        if not _table_exists(conn, table):
            continue
        for operation in ("UPDATE", "DELETE"):
            name = f"trg_{table}_no_{operation.lower()}"
            sql = (
                f'CREATE TRIGGER "{name}" BEFORE {operation} ON "{table}" '
                "BEGIN SELECT RAISE(ABORT, 'Allocation history is append-only'); END"
            )
            changed = _ensure_trigger(conn, name, sql) or changed

    replace_conditions = {
        "allocation_history_events": (
            "EXISTS (SELECT 1 FROM allocation_history_events WHERE id=NEW.id)",
            "(NEW.idempotency_key IS NOT NULL AND EXISTS (SELECT 1 FROM allocation_history_events "
            "WHERE idempotency_key=NEW.idempotency_key))",
            "(NEW.version IS NOT NULL AND EXISTS (SELECT 1 FROM allocation_history_events "
            "WHERE allocation_id=NEW.allocation_id AND version=NEW.version))",
            "(NEW.operation='legacy_history_unknown' AND EXISTS (SELECT 1 FROM allocation_history_events "
            "WHERE bank_transaction_id=NEW.bank_transaction_id AND operation='legacy_history_unknown'))",
            "(NEW.linked_event_id IS NOT NULL AND NEW.operation='allocation_reversal' AND EXISTS "
            "(SELECT 1 FROM allocation_history_events WHERE linked_event_id=NEW.linked_event_id "
            "AND operation='allocation_reversal'))",
            "(NEW.linked_allocation_id IS NOT NULL AND NEW.operation='allocation_reversal' AND EXISTS "
            "(SELECT 1 FROM allocation_history_events WHERE linked_allocation_id=NEW.linked_allocation_id "
            "AND operation='allocation_reversal'))",
            "(NEW.snapshot_id IS NOT NULL AND EXISTS (SELECT 1 FROM allocation_history_events "
            "WHERE snapshot_id=NEW.snapshot_id))",
        ),
        "allocation_history_snapshots": (
            "EXISTS (SELECT 1 FROM allocation_history_snapshots WHERE id=NEW.id)",
        ),
        "allocation_history_tax_components": (
            "EXISTS (SELECT 1 FROM allocation_history_tax_components WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM allocation_history_tax_components "
            "WHERE snapshot_id=NEW.snapshot_id AND tax_code=NEW.tax_code)",
        ),
        "allocation_history_finalizations": (
            "EXISTS (SELECT 1 FROM allocation_history_finalizations WHERE snapshot_id=NEW.snapshot_id)",
        ),
        "bas_period_lodgment_markers": (
            "EXISTS (SELECT 1 FROM bas_period_lodgment_markers WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM bas_period_lodgment_markers WHERE period_id=NEW.period_id)",
        ),
        "allocation_period_correction_approvals": (
            "EXISTS (SELECT 1 FROM allocation_period_correction_approvals WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM allocation_period_correction_approvals "
            "WHERE period_marker_id=NEW.period_marker_id "
            "AND approved_idempotency_key=NEW.approved_idempotency_key)",
        ),
        "allocation_history_baseline_bindings": (
            "EXISTS (SELECT 1 FROM allocation_history_baseline_bindings WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM allocation_history_baseline_bindings "
            "WHERE baseline_event_id=NEW.baseline_event_id "
            "AND allocation_row_id=NEW.allocation_row_id)",
        ),
        "allocation_history_baseline_tax_bindings": (
            "EXISTS (SELECT 1 FROM allocation_history_baseline_tax_bindings WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM allocation_history_baseline_tax_bindings "
            "WHERE source_component_row_id=NEW.source_component_row_id)",
        ),
        "allocation_history_baseline_finalizations": (
            "EXISTS (SELECT 1 FROM allocation_history_baseline_finalizations "
            "WHERE baseline_event_id=NEW.baseline_event_id)",
            "EXISTS (SELECT 1 FROM allocation_history_baseline_finalizations "
            "WHERE bank_transaction_id=NEW.bank_transaction_id)",
        ),
    }
    for table, conditions in replace_conditions.items():
        if not _table_exists(conn, table):
            continue
        name = f"trg_{table}_no_replace_insert"
        sql = (
            f'CREATE TRIGGER "{name}" BEFORE INSERT ON "{table}" '
            f'WHEN {" OR ".join(conditions)} '
            "BEGIN SELECT RAISE(ABORT, 'Allocation history is append-only'); END"
        )
        changed = _ensure_trigger(conn, name, sql) or changed

    nonblank = lambda value: (
        f"length(trim(COALESCE({value}, ''), char(9)||char(10)||char(13)||' '))=0"
    )
    snapshot_date = _sqlite_invalid_iso_date("NEW.created_at")
    snapshot_sql = f'''CREATE TRIGGER "trg_allocation_history_snapshots_domain"
        BEFORE INSERT ON "allocation_history_snapshots"
        WHEN typeof(NEW.gross_cents)!='integer' OR NEW.gross_cents<=0
          OR typeof(NEW.gst_cents)!='integer' OR NEW.gst_cents<0 OR NEW.gst_cents>NEW.gross_cents
          OR NEW.source_kind NOT IN ('invoice','bill')
          OR {nonblank('NEW.created_at')}
                    OR julianday(NEW.created_at) IS NULL
          OR NOT EXISTS (SELECT 1 FROM invoices i WHERE i.id=NEW.invoice_id
             AND ((NEW.source_kind='invoice' AND i.direction='AR')
               OR (NEW.source_kind='bill' AND i.direction='AP')))
        BEGIN SELECT RAISE(ABORT, 'Invalid allocation snapshot'); END'''
    changed = _ensure_trigger(
        conn, "trg_allocation_history_snapshots_domain", snapshot_sql
    ) or changed

    component_sql = f'''CREATE TRIGGER "trg_allocation_history_tax_components_domain"
        BEFORE INSERT ON "allocation_history_tax_components"
        WHEN typeof(NEW.gross_cents)!='integer' OR NEW.gross_cents<=0
          OR typeof(NEW.gst_cents)!='integer' OR NEW.gst_cents<0 OR NEW.gst_cents>NEW.gross_cents
          OR NEW.tax_code NOT IN ('standard','gst_free','input_taxed','capital','none')
          OR {nonblank('NEW.tax_code')}
                    OR NOT EXISTS (SELECT 1 FROM allocation_history_snapshots s WHERE s.id=NEW.snapshot_id)
          OR EXISTS (SELECT 1 FROM allocation_history_finalizations f WHERE f.snapshot_id=NEW.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'Invalid or finalized allocation tax component'); END'''
    changed = _ensure_trigger(
        conn, "trg_allocation_history_tax_components_domain", component_sql
    ) or changed

    finalization_sql = f'''CREATE TRIGGER "trg_allocation_history_finalizations_domain"
        BEFORE INSERT ON "allocation_history_finalizations"
        WHEN typeof(NEW.component_count)!='integer' OR NEW.component_count<1
          OR typeof(NEW.gross_cents)!='integer' OR typeof(NEW.gst_cents)!='integer'
          OR {nonblank('NEW.finalized_at')}
                    OR julianday(NEW.finalized_at) IS NULL
          OR NOT EXISTS (SELECT 1 FROM allocation_history_snapshots s
             WHERE s.id=NEW.snapshot_id AND s.gross_cents=NEW.gross_cents
               AND s.gst_cents=NEW.gst_cents)
          OR NEW.component_count != (SELECT COUNT(*) FROM allocation_history_tax_components c
             WHERE c.snapshot_id=NEW.snapshot_id)
          OR NEW.gross_cents != COALESCE((SELECT SUM(gross_cents)
             FROM allocation_history_tax_components c WHERE c.snapshot_id=NEW.snapshot_id),-1)
          OR NEW.gst_cents != COALESCE((SELECT SUM(gst_cents)
             FROM allocation_history_tax_components c WHERE c.snapshot_id=NEW.snapshot_id),-1)
        BEGIN SELECT RAISE(ABORT, 'Allocation snapshot does not reconcile'); END'''
    changed = _ensure_trigger(
        conn, "trg_allocation_history_finalizations_domain", finalization_sql
    ) or changed

    if all(_table_exists(conn, table) for table in (
        "allocation_history_events",
        "allocation_history_baseline_bindings",
        "allocation_history_baseline_tax_bindings",
        "allocation_history_baseline_finalizations",
        "invoice_payment_allocations",
        "invoice_payment_tax_components",
        "invoices",
        "bank_transactions",
    )):
        baseline_binding_sql = f'''CREATE TRIGGER "trg_allocation_history_baseline_bindings_domain"
                BEFORE INSERT ON "allocation_history_baseline_bindings"
                WHEN typeof(NEW.allocation_row_id)!='integer' OR NEW.allocation_row_id<=0
                    OR (NEW.source_kind IS NOT NULL AND NEW.source_kind NOT IN ('invoice','bill'))
                    OR (NEW.invoice_or_bill_id IS NOT NULL AND typeof(NEW.invoice_or_bill_id)!='integer')
                    OR (NEW.allocated_gross_cents IS NOT NULL
                            AND typeof(NEW.allocated_gross_cents)!='integer')
                    OR (NEW.allocated_gst_cents IS NOT NULL
                            AND typeof(NEW.allocated_gst_cents)!='integer')
                    OR (NEW.allocated_gross_source IS NOT NULL
                            AND typeof(NEW.allocated_gross_source)!='text')
                    OR (NEW.allocated_gst_source IS NOT NULL
                            AND typeof(NEW.allocated_gst_source)!='text')
                    OR NOT EXISTS (SELECT 1 FROM allocation_history_events e
                            WHERE e.id=NEW.baseline_event_id
                                AND e.operation='legacy_history_unknown'
                                AND e.bank_transaction_id=NEW.bank_transaction_id)
                    OR EXISTS (SELECT 1 FROM allocation_history_baseline_finalizations f
                            WHERE f.baseline_event_id=NEW.baseline_event_id)
                BEGIN SELECT RAISE(ABORT, 'Invalid or finalized legacy allocation binding'); END'''
        changed = _ensure_trigger(
                conn, "trg_allocation_history_baseline_bindings_domain", baseline_binding_sql
        ) or changed

        baseline_tax_binding_sql = f'''CREATE TRIGGER "trg_allocation_history_baseline_tax_bindings_domain"
                BEFORE INSERT ON "allocation_history_baseline_tax_bindings"
                WHEN typeof(NEW.source_component_row_id)!='integer'
                    OR NEW.source_component_row_id<=0
                    OR (NEW.component_gross_cents IS NOT NULL
                            AND typeof(NEW.component_gross_cents)!='integer')
                    OR (NEW.component_gst_cents IS NOT NULL
                            AND typeof(NEW.component_gst_cents)!='integer')
                    OR (NEW.tax_code IS NOT NULL AND typeof(NEW.tax_code)!='text')
                    OR NOT EXISTS (SELECT 1 FROM allocation_history_baseline_bindings b
                            WHERE b.id=NEW.allocation_binding_id)
                    OR EXISTS (SELECT 1 FROM allocation_history_baseline_finalizations f
                            JOIN allocation_history_baseline_bindings b
                                ON b.baseline_event_id=f.baseline_event_id
                            WHERE b.id=NEW.allocation_binding_id)
                BEGIN SELECT RAISE(ABORT, 'Invalid or finalized legacy tax binding'); END'''
        changed = _ensure_trigger(
                conn,
                "trg_allocation_history_baseline_tax_bindings_domain",
                baseline_tax_binding_sql,
        ) or changed

        allocation_identity_mismatch = """EXISTS (
                SELECT 1 FROM invoice_payment_allocations a
                LEFT JOIN allocation_history_baseline_bindings b
                    ON b.baseline_event_id=NEW.baseline_event_id
                 AND b.allocation_row_id=a.id
                LEFT JOIN invoices i ON i.id=a.invoice_id
                WHERE a.bank_transaction_id=NEW.bank_transaction_id
                    AND (b.id IS NULL OR b.bank_transaction_id IS NOT a.bank_transaction_id
                        OR b.invoice_or_bill_id IS NOT a.invoice_id
                        OR b.source_kind IS NOT CASE i.direction
                                 WHEN 'AR' THEN 'invoice' WHEN 'AP' THEN 'bill' ELSE NULL END
                        OR b.allocated_gross_source IS NOT CAST(a.amount AS TEXT)
                        OR b.allocated_gst_source IS NOT CAST(a.gst_amount AS TEXT)
                        OR b.allocated_gross_cents IS NOT allocation_baseline_cents(a.amount)
                        OR b.allocated_gst_cents IS NOT allocation_baseline_cents(a.gst_amount)
                        OR b.allocation_created_at IS NOT CAST(a.created_at AS TEXT))
        ) OR EXISTS (
                SELECT 1 FROM allocation_history_baseline_bindings b
                LEFT JOIN invoice_payment_allocations a ON a.id=b.allocation_row_id
                WHERE b.baseline_event_id=NEW.baseline_event_id
                    AND (a.id IS NULL OR a.bank_transaction_id IS NOT b.bank_transaction_id
                        OR a.bank_transaction_id IS NOT NEW.bank_transaction_id)
        )"""
        component_identity_mismatch = """EXISTS (
                SELECT 1 FROM invoice_payment_tax_components c
                JOIN invoice_payment_allocations a ON a.id=c.allocation_id
                LEFT JOIN allocation_history_baseline_bindings ab
                    ON ab.baseline_event_id=NEW.baseline_event_id AND ab.allocation_row_id=a.id
                LEFT JOIN allocation_history_baseline_tax_bindings tb
                    ON tb.allocation_binding_id=ab.id AND tb.source_component_row_id=c.id
                WHERE a.bank_transaction_id=NEW.bank_transaction_id
                    AND (tb.id IS NULL OR c.allocation_id IS NOT ab.allocation_row_id
                        OR c.tax_code IS NOT tb.tax_code
                        OR tb.component_gross_source IS NOT CAST(c.gross_amount AS TEXT)
                        OR tb.component_gst_source IS NOT CAST(c.gst_amount AS TEXT)
                        OR tb.component_gross_cents IS NOT allocation_baseline_cents(c.gross_amount)
                        OR tb.component_gst_cents IS NOT allocation_baseline_cents(c.gst_amount))
        ) OR EXISTS (
                SELECT 1 FROM allocation_history_baseline_tax_bindings tb
                JOIN allocation_history_baseline_bindings ab
                    ON ab.id=tb.allocation_binding_id
                LEFT JOIN invoice_payment_tax_components c
                    ON c.id=tb.source_component_row_id
                WHERE ab.baseline_event_id=NEW.baseline_event_id
                    AND (c.id IS NULL OR c.allocation_id IS NOT ab.allocation_row_id)
        )"""
        baseline_hash_sql = """(
                SELECT allocation_baseline_sha256(row_json) FROM (
                    SELECT json_array('meta', NEW.baseline_event_id,
                            NEW.bank_transaction_id, NEW.observed_payment_date, NEW.observed_at,
                            NEW.observed_allocation_count, NEW.observed_component_count,
                            NEW.gross_allocation_total_cents, NEW.gst_total_cents,
                            NEW.status, NEW.incomplete_reason_codes) AS row_json
                    UNION ALL
                    SELECT json_array('allocation', b.id, b.baseline_event_id,
                            b.bank_transaction_id, b.allocation_row_id, b.source_kind,
                            b.invoice_or_bill_id, b.allocated_gross_cents,
                            b.allocated_gross_source, b.allocated_gst_cents,
                            b.allocated_gst_source, b.allocation_created_at)
                    FROM allocation_history_baseline_bindings b
                    WHERE b.baseline_event_id=NEW.baseline_event_id
                    UNION ALL
                    SELECT json_array('component', tb.id, tb.allocation_binding_id,
                            tb.source_component_row_id, tb.tax_code, tb.component_gross_cents,
                            tb.component_gross_source, tb.component_gst_cents,
                            tb.component_gst_source)
                    FROM allocation_history_baseline_tax_bindings tb
                    JOIN allocation_history_baseline_bindings b
                        ON b.id=tb.allocation_binding_id
                    WHERE b.baseline_event_id=NEW.baseline_event_id
                )
        )"""

        reasons_sql = f"""(
            SELECT json_group_array(code) FROM (
                SELECT code FROM (
                    SELECT 1 AS ordering, 'missing_tax_components' AS code
                    WHERE EXISTS (SELECT 1 FROM allocation_history_baseline_bindings b
                        WHERE b.baseline_event_id=NEW.baseline_event_id
                            AND NOT EXISTS (SELECT 1 FROM allocation_history_baseline_tax_bindings tb
                                WHERE tb.allocation_binding_id=b.id))
                    UNION ALL
                    SELECT 2, 'invalid_amount' WHERE EXISTS (
                        SELECT 1 FROM allocation_history_baseline_bindings b
                        JOIN invoice_payment_allocations a ON a.id=b.allocation_row_id
                        WHERE b.baseline_event_id=NEW.baseline_event_id AND (
                            a.amount<=0 OR allocation_baseline_cents(a.amount) IS NULL
                            OR a.gst_amount<0 OR a.gst_amount>a.amount
                            OR allocation_baseline_cents(a.gst_amount) IS NULL
                            OR b.allocated_gross_cents IS NULL OR b.allocated_gst_cents IS NULL
                            OR EXISTS (SELECT 1 FROM allocation_history_baseline_tax_bindings tb
                                WHERE tb.allocation_binding_id=b.id AND (
                                    tb.component_gross_cents IS NULL OR tb.component_gst_cents IS NULL
                                    OR tb.component_gross_cents<=0 OR tb.component_gst_cents<0
                                    OR tb.component_gst_cents>tb.component_gross_cents
                                    OR tb.component_gross_source IS NULL OR tb.component_gst_source IS NULL))
                        ))
                    UNION ALL
                    SELECT 3, 'unreconciled_totals' WHERE EXISTS (
                        SELECT 1 FROM allocation_history_baseline_bindings b
                        WHERE b.baseline_event_id=NEW.baseline_event_id AND (
                            EXISTS (SELECT 1 FROM allocation_history_baseline_tax_bindings tb
                                WHERE tb.allocation_binding_id=b.id) AND (
                            b.allocated_gross_cents IS NOT COALESCE((SELECT SUM(tb.component_gross_cents)
                                FROM allocation_history_baseline_tax_bindings tb
                                WHERE tb.allocation_binding_id=b.id),0)
                            OR b.allocated_gst_cents IS NOT COALESCE((SELECT SUM(tb.component_gst_cents)
                                FROM allocation_history_baseline_tax_bindings tb
                                WHERE tb.allocation_binding_id=b.id),0))))
                    UNION ALL
                    SELECT 4, 'unsupported_tax_evidence' WHERE EXISTS (
                        SELECT 1 FROM allocation_history_baseline_tax_bindings tb
                        JOIN allocation_history_baseline_bindings b
                            ON b.id=tb.allocation_binding_id
                        WHERE b.baseline_event_id=NEW.baseline_event_id
                            AND (tb.tax_code IS NULL OR tb.tax_code NOT IN
                                ('standard','gst_free','input_taxed','capital','none')))
                    UNION ALL
                    SELECT 5, 'insufficient_source_data' WHERE EXISTS (
                        SELECT 1 FROM allocation_history_baseline_bindings b
                        LEFT JOIN invoices i ON i.id=b.invoice_or_bill_id
                        JOIN bank_transactions t ON t.id=b.bank_transaction_id
                        WHERE b.baseline_event_id=NEW.baseline_event_id AND (
                            b.invoice_or_bill_id IS NULL OR b.source_kind IS NULL OR i.id IS NULL
                            OR (b.source_kind='invoice' AND (i.direction!='AR' OR t.direction!='in'))
                            OR (b.source_kind='bill' AND (i.direction!='AP' OR t.direction!='out'))))
                                        UNION ALL
                                        SELECT 6, 'insufficient_source_data'
                                                WHERE NEW.observed_payment_date IS NULL
                                                    OR {_sqlite_invalid_iso_date('NEW.observed_payment_date')}
                ) ORDER BY ordering
            )
        )"""
        baseline_finalization_sql = f'''CREATE TRIGGER "trg_allocation_history_baseline_finalizations_domain"
                BEFORE INSERT ON "allocation_history_baseline_finalizations"
                WHEN typeof(NEW.observed_allocation_count)!='integer'
                    OR NEW.observed_allocation_count<0
                    OR typeof(NEW.observed_component_count)!='integer'
                    OR NEW.observed_component_count<0
                    OR typeof(NEW.gross_allocation_total_cents)!='integer'
                    OR typeof(NEW.gst_total_cents)!='integer'
                    OR {nonblank('NEW.observed_at')} OR julianday(NEW.observed_at) IS NULL
                    OR length(NEW.integrity_hash)!=64
                    OR lower(NEW.integrity_hash) GLOB '*[^0-9a-f]*'
                    OR NEW.status NOT IN ('complete','incomplete')
                    OR json_valid(NEW.incomplete_reason_codes)!=1
                    OR json_type(NEW.incomplete_reason_codes)!='array'
                    OR NOT EXISTS (SELECT 1 FROM allocation_history_events e
                            WHERE e.id=NEW.baseline_event_id
                                AND e.operation='legacy_history_unknown'
                                AND e.bank_transaction_id=NEW.bank_transaction_id)
                    OR NOT EXISTS (SELECT 1 FROM bank_transactions t
                            WHERE t.id=NEW.bank_transaction_id
                                AND t.occurred_at IS NEW.observed_payment_date)
                    OR NEW.observed_allocation_count != (SELECT COUNT(*)
                            FROM allocation_history_baseline_bindings b
                            WHERE b.baseline_event_id=NEW.baseline_event_id)
                    OR NEW.observed_component_count != (SELECT COUNT(*)
                            FROM allocation_history_baseline_tax_bindings tb
                            JOIN allocation_history_baseline_bindings b
                                ON b.id=tb.allocation_binding_id
                            WHERE b.baseline_event_id=NEW.baseline_event_id)
                    OR NEW.gross_allocation_total_cents != COALESCE((SELECT SUM(b.allocated_gross_cents)
                            FROM allocation_history_baseline_bindings b
                            WHERE b.baseline_event_id=NEW.baseline_event_id),0)
                    OR NEW.gst_total_cents != COALESCE((SELECT SUM(tb.component_gst_cents)
                            FROM allocation_history_baseline_tax_bindings tb
                            JOIN allocation_history_baseline_bindings b
                                ON b.id=tb.allocation_binding_id
                            WHERE b.baseline_event_id=NEW.baseline_event_id),0)
                    OR (SELECT COUNT(*) FROM invoice_payment_allocations a
                            WHERE a.bank_transaction_id=NEW.bank_transaction_id)
                         != (SELECT COUNT(*) FROM allocation_history_baseline_bindings b
                            WHERE b.baseline_event_id=NEW.baseline_event_id)
                    OR {allocation_identity_mismatch}
                    OR (SELECT COUNT(*) FROM invoice_payment_tax_components c
                            JOIN invoice_payment_allocations a ON a.id=c.allocation_id
                            WHERE a.bank_transaction_id=NEW.bank_transaction_id)
                         != (SELECT COUNT(*) FROM allocation_history_baseline_tax_bindings tb
                            JOIN allocation_history_baseline_bindings b
                                ON b.id=tb.allocation_binding_id
                            WHERE b.baseline_event_id=NEW.baseline_event_id)
                    OR {component_identity_mismatch}
                    OR NEW.integrity_hash != {baseline_hash_sql}
                    OR NEW.incomplete_reason_codes != {reasons_sql}
                    OR (NEW.status='complete' AND {reasons_sql}!='[]')
                    OR (NEW.status='incomplete' AND {reasons_sql}='[]')
                BEGIN SELECT RAISE(ABORT, 'Legacy allocation baseline does not match observed rows'); END'''
        changed = _ensure_trigger(
                conn,
                "trg_allocation_history_baseline_finalizations_domain",
                baseline_finalization_sql,
        ) or changed

        finalized_allocation = """EXISTS (
                SELECT 1 FROM allocation_history_baseline_finalizations f
                JOIN allocation_history_baseline_bindings b
                    ON b.baseline_event_id=f.baseline_event_id
                WHERE f.bank_transaction_id={transaction}
                    OR b.allocation_row_id={allocation_id})"""
        allocation_guards = (
                ("insert", "INSERT", "NEW", f"{finalized_allocation.format(transaction='NEW.bank_transaction_id', allocation_id='NEW.id')}"),
                ("update", "UPDATE", "OLD", f"{finalized_allocation.format(transaction='OLD.bank_transaction_id', allocation_id='OLD.id')} OR {finalized_allocation.format(transaction='NEW.bank_transaction_id', allocation_id='NEW.id')}"),
                ("delete", "DELETE", "OLD", finalized_allocation.format(transaction="OLD.bank_transaction_id", allocation_id="OLD.id")),
        )
        for suffix, operation, _row, condition in allocation_guards:
                trigger_name = f"trg_invoice_payment_allocations_baseline_no_{suffix}"
                sql = (
                    f'CREATE TRIGGER "{trigger_name}" BEFORE {operation} '
                        f'ON "invoice_payment_allocations" WHEN {condition} '
                        "BEGIN SELECT RAISE(ABORT, 'Finalized legacy allocation baseline is immutable'); END"
                )
                changed = _ensure_trigger(conn, trigger_name, sql) or changed
        allocation_replace_sql = f'''CREATE TRIGGER "trg_invoice_payment_allocations_baseline_no_replace"
                BEFORE INSERT ON "invoice_payment_allocations"
                WHEN {finalized_allocation.format(transaction='NEW.bank_transaction_id', allocation_id='NEW.id')}
                BEGIN SELECT RAISE(ABORT, 'Finalized legacy allocation baseline is immutable'); END'''
        changed = _ensure_trigger(
                conn, "trg_invoice_payment_allocations_baseline_no_replace", allocation_replace_sql
        ) or changed

        finalized_component = """EXISTS (
                SELECT 1 FROM allocation_history_baseline_finalizations f
                JOIN allocation_history_baseline_bindings b
                    ON b.baseline_event_id=f.baseline_event_id
                JOIN allocation_history_baseline_tax_bindings tb
                    ON tb.allocation_binding_id=b.id
                WHERE b.allocation_row_id={allocation_id}
                    OR tb.source_component_row_id={component_id})"""
        component_guards = (
                ("insert", "INSERT", finalized_component.format(
                        allocation_id="NEW.allocation_id", component_id="NEW.id")),
                ("update", "UPDATE", f"{finalized_component.format(allocation_id='OLD.allocation_id', component_id='OLD.id')} OR {finalized_component.format(allocation_id='NEW.allocation_id', component_id='NEW.id')}"),
                ("delete", "DELETE", finalized_component.format(
                        allocation_id="OLD.allocation_id", component_id="OLD.id")),
        )
        for suffix, operation, condition in component_guards:
                trigger_name = f"trg_invoice_payment_tax_components_baseline_no_{suffix}"
                sql = (
                    f'CREATE TRIGGER "{trigger_name}" BEFORE {operation} '
                        f'ON "invoice_payment_tax_components" WHEN {condition} '
                        "BEGIN SELECT RAISE(ABORT, 'Finalized legacy allocation baseline is immutable'); END"
                )
                changed = _ensure_trigger(conn, trigger_name, sql) or changed
        component_replace_sql = f'''CREATE TRIGGER "trg_invoice_payment_tax_components_baseline_no_replace"
                BEFORE INSERT ON "invoice_payment_tax_components"
                WHEN {finalized_component.format(allocation_id='NEW.allocation_id', component_id='NEW.id')}
                BEGIN SELECT RAISE(ABORT, 'Finalized legacy allocation baseline is immutable'); END'''
        changed = _ensure_trigger(
                conn,
                "trg_invoice_payment_tax_components_baseline_no_replace",
                component_replace_sql,
        ) or changed

    period_start_invalid = _sqlite_invalid_iso_date("NEW.period_start")
    period_end_invalid = _sqlite_invalid_iso_date("NEW.period_end")
    marker_sql = f'''CREATE TRIGGER "trg_bas_period_lodgment_markers_domain"
        BEFORE INSERT ON "bas_period_lodgment_markers"
        WHEN {nonblank('NEW.period_id')} OR {nonblank('NEW.actor_placeholder')}
          OR {nonblank('NEW.evidence_reference')}
          OR {nonblank('NEW.internally_asserted_at')}
                    OR julianday(NEW.internally_asserted_at) IS NULL
          OR {period_start_invalid} OR {period_end_invalid}
          OR julianday(NEW.period_end)<julianday(NEW.period_start)
          OR NEW.lodged_status != 1
          OR NEW.evidence_scope != 'internal_assertion_not_ato_lodgment_evidence'
        BEGIN SELECT RAISE(ABORT, 'Invalid internal BAS period marker'); END'''
    changed = _ensure_trigger(
        conn, "trg_bas_period_lodgment_markers_domain", marker_sql
    ) or changed

    approval_sql = f'''CREATE TRIGGER "trg_allocation_period_correction_approvals_domain"
        BEFORE INSERT ON "allocation_period_correction_approvals"
        WHEN {nonblank('NEW.approved_idempotency_key')}
          OR {nonblank('NEW.actor_placeholder')} OR {nonblank('NEW.reason')}
          OR {nonblank('NEW.evidence_reference')} OR {nonblank('NEW.recorded_at')}
                    OR julianday(NEW.recorded_at) IS NULL
          OR NOT EXISTS (SELECT 1 FROM bas_period_lodgment_markers m
             WHERE m.id=NEW.period_marker_id)
          OR NOT EXISTS (SELECT 1 FROM invoice_payment_allocations a
             WHERE a.id=NEW.allocation_id)
        BEGIN SELECT RAISE(ABORT, 'Invalid allocation correction approval'); END'''
    changed = _ensure_trigger(
        conn, "trg_allocation_period_correction_approvals_domain", approval_sql
    ) or changed

    # A new event supersedes one exact version. Version zero is the explicit
    # existing allocation row; subsequent versions point to the preceding event.
    latest_version = (
        "COALESCE((SELECT MAX(e.version) FROM allocation_history_events e "
        "WHERE e.allocation_id=NEW.allocation_id AND e.version IS NOT NULL),0)"
    )
    other_cash_total = (
        "(SELECT COALESCE(SUM(CASE WHEN latest.operation='allocation_reversal' THEN 0 "
        "WHEN latest.id IS NULL THEN CAST(ROUND(a.amount*100) AS INTEGER) "
        "ELSE s.gross_cents END),0) FROM invoice_payment_allocations a "
        "LEFT JOIN allocation_history_events latest ON latest.id=(SELECT e.id "
        "FROM allocation_history_events e WHERE e.allocation_id=a.id "
        "AND e.version IS NOT NULL ORDER BY e.version DESC LIMIT 1) "
        "LEFT JOIN allocation_history_snapshots s ON s.id=latest.snapshot_id "
        "WHERE a.bank_transaction_id=NEW.bank_transaction_id AND a.id!=NEW.allocation_id)"
    )
    event_sql = f'''CREATE TRIGGER "trg_allocation_history_events_domain"
        BEFORE INSERT ON "allocation_history_events"
                WHEN NOT EXISTS (SELECT 1 FROM bank_transactions t WHERE t.id=NEW.bank_transaction_id)
                    OR (NEW.operation='legacy_history_unknown' AND (
             NEW.allocation_id IS NOT NULL OR NEW.snapshot_id IS NOT NULL
             OR NEW.version IS NOT NULL OR NEW.effective_date IS NOT NULL
             OR NEW.recorded_at IS NOT NULL OR NEW.idempotency_key IS NOT NULL
             OR NEW.expected_prior_version IS NOT NULL OR NEW.actor_placeholder IS NOT NULL
             OR NEW.reason IS NOT NULL OR NEW.evidence_reference IS NOT NULL
             OR NEW.evidence_sha256 IS NOT NULL OR NEW.linked_event_id IS NOT NULL
             OR NEW.linked_allocation_id IS NOT NULL OR NEW.approval_id IS NOT NULL))
          OR (NEW.operation IN ('allocation_correction','allocation_reversal') AND (
             NEW.allocation_id IS NULL OR NEW.snapshot_id IS NULL
             OR typeof(NEW.version)!='integer' OR NEW.version<1
             OR typeof(NEW.expected_prior_version)!='integer' OR NEW.expected_prior_version<0
             OR NEW.version!=NEW.expected_prior_version+1
             OR {nonblank('NEW.effective_date')} OR {_sqlite_invalid_iso_date('NEW.effective_date')}
             OR {nonblank('NEW.recorded_at')} OR julianday(NEW.recorded_at) IS NULL
             OR {nonblank('NEW.idempotency_key')}
             OR {nonblank('NEW.actor_placeholder')} OR {nonblank('NEW.reason')}
             OR {nonblank('NEW.evidence_reference')}
             OR (NEW.evidence_sha256 IS NOT NULL AND (
                 length(NEW.evidence_sha256)!=64 OR NEW.evidence_sha256 GLOB '*[^0-9a-fA-F]*'))
             OR ((NEW.linked_event_id IS NOT NULL) = (NEW.linked_allocation_id IS NOT NULL))
             OR NOT EXISTS (SELECT 1 FROM invoice_payment_allocations a
                 WHERE a.id=NEW.allocation_id AND a.bank_transaction_id=NEW.bank_transaction_id)
             OR NOT EXISTS (SELECT 1 FROM allocation_history_finalizations f
                 WHERE f.snapshot_id=NEW.snapshot_id)
             OR NOT EXISTS (SELECT 1 FROM allocation_history_snapshots s
                 JOIN invoices i ON i.id=s.invoice_id
                 JOIN bank_transactions t ON t.id=NEW.bank_transaction_id
                 WHERE s.id=NEW.snapshot_id AND s.source_kind=CASE i.direction
                   WHEN 'AR' THEN 'invoice' WHEN 'AP' THEN 'bill' ELSE '' END
                   AND ((i.direction='AR' AND t.direction='in')
                     OR (i.direction='AP' AND t.direction='out')))
             OR NEW.expected_prior_version!={latest_version}
             OR (NEW.expected_prior_version=0 AND (
                 NEW.linked_allocation_id IS NOT NEW.allocation_id OR NEW.linked_event_id IS NOT NULL))
             OR (NEW.expected_prior_version>0 AND (
                 NEW.linked_allocation_id IS NOT NULL OR NOT EXISTS (
                   SELECT 1 FROM allocation_history_events prior
                   WHERE prior.id=NEW.linked_event_id AND prior.allocation_id=NEW.allocation_id
                     AND prior.version=NEW.expected_prior_version
                     AND prior.version={latest_version}
                     AND prior.operation!='allocation_reversal')))
             OR (NEW.operation='allocation_reversal' AND (
                 (NEW.linked_event_id IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM allocation_history_events prior
                   JOIN allocation_history_snapshots ps ON ps.id=prior.snapshot_id
                   JOIN allocation_history_snapshots ns ON ns.id=NEW.snapshot_id
                   WHERE prior.id=NEW.linked_event_id
                     AND julianday(NEW.effective_date)>julianday(prior.effective_date)
                     AND ps.invoice_id=ns.invoice_id AND ps.gross_cents=ns.gross_cents
                                         AND ps.gst_cents=ns.gst_cents))
                 OR (NEW.linked_allocation_id IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM invoice_payment_allocations prior
                   JOIN allocation_history_snapshots ns ON ns.id=NEW.snapshot_id
                   JOIN invoices i ON i.id=prior.invoice_id
                   JOIN bank_transactions t ON t.id=prior.bank_transaction_id
                   WHERE prior.id=NEW.linked_allocation_id AND prior.id=NEW.allocation_id
                     AND julianday(NEW.effective_date)>julianday(t.occurred_at)
                     AND ns.invoice_id=prior.invoice_id
                     AND ns.gross_cents=CAST(ROUND(prior.amount*100) AS INTEGER)
                    AND ns.gst_cents=CAST(ROUND(prior.gst_amount*100) AS INTEGER)))))
             OR (NEW.operation='allocation_correction' AND NEW.linked_event_id IS NOT NULL
                 AND EXISTS (SELECT 1 FROM allocation_history_events prior
                   WHERE prior.id=NEW.linked_event_id AND prior.operation='allocation_reversal'))
             OR (NEW.operation='allocation_correction' AND NEW.linked_allocation_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations a
                   WHERE a.id=NEW.linked_allocation_id AND a.id=NEW.allocation_id))
             OR (CAST(ROUND((SELECT amount FROM bank_transactions
                   WHERE id=NEW.bank_transaction_id)*100) AS INTEGER)
                   < {other_cash_total} + CASE WHEN NEW.operation='allocation_reversal' THEN 0
                       ELSE (SELECT gross_cents FROM allocation_history_snapshots
                             WHERE id=NEW.snapshot_id) END)
             OR (EXISTS (SELECT 1 FROM bas_period_lodgment_markers m
                   WHERE NEW.effective_date BETWEEN m.period_start AND m.period_end)
                 AND NOT EXISTS (SELECT 1 FROM allocation_period_correction_approvals a
                   JOIN bas_period_lodgment_markers m ON m.id=a.period_marker_id
                   WHERE a.id=NEW.approval_id AND a.allocation_id=NEW.allocation_id
                     AND a.approved_idempotency_key=NEW.idempotency_key
                     AND NEW.effective_date BETWEEN m.period_start AND m.period_end))
          ))
        BEGIN SELECT RAISE(ABORT, 'Invalid allocation history event'); END'''
    changed = _ensure_trigger(
        conn, "trg_allocation_history_events_domain", event_sql
    ) or changed

    reversal_components_sql = '''CREATE TRIGGER "trg_allocation_history_reversal_components"
                BEFORE INSERT ON "allocation_history_events"
                WHEN NEW.operation='allocation_reversal' AND (
                    (NEW.linked_event_id IS NOT NULL AND EXISTS (
                        SELECT 1 FROM allocation_history_events prior
                        JOIN allocation_history_snapshots ps ON ps.id=prior.snapshot_id
                        JOIN allocation_history_snapshots ns ON ns.id=NEW.snapshot_id
                        WHERE prior.id=NEW.linked_event_id AND (
                            EXISTS (SELECT tc.tax_code, tc.gross_cents, tc.gst_cents
                                FROM allocation_history_tax_components tc WHERE tc.snapshot_id=ps.id
                                EXCEPT
                                SELECT nc.tax_code, nc.gross_cents, nc.gst_cents
                                FROM allocation_history_tax_components nc WHERE nc.snapshot_id=ns.id)
                            OR EXISTS (SELECT nc.tax_code, nc.gross_cents, nc.gst_cents
                                FROM allocation_history_tax_components nc WHERE nc.snapshot_id=ns.id
                                EXCEPT
                                SELECT tc.tax_code, tc.gross_cents, tc.gst_cents
                                FROM allocation_history_tax_components tc WHERE tc.snapshot_id=ps.id))))
                    OR (NEW.linked_allocation_id IS NOT NULL
                        AND EXISTS (SELECT 1 FROM invoice_payment_tax_components c
                                                WHERE c.allocation_id=NEW.linked_allocation_id)
                        AND (
                            EXISTS (SELECT c.tax_code, CAST(ROUND(c.gross_amount*100) AS INTEGER),
                                                         CAST(ROUND(c.gst_amount*100) AS INTEGER)
                                FROM invoice_payment_tax_components c
                                WHERE c.allocation_id=NEW.linked_allocation_id
                                EXCEPT
                                SELECT nc.tax_code, nc.gross_cents, nc.gst_cents
                                FROM allocation_history_tax_components nc WHERE nc.snapshot_id=NEW.snapshot_id)
                            OR EXISTS (SELECT nc.tax_code, nc.gross_cents, nc.gst_cents
                                FROM allocation_history_tax_components nc WHERE nc.snapshot_id=NEW.snapshot_id
                                EXCEPT
                                SELECT c.tax_code, CAST(ROUND(c.gross_amount*100) AS INTEGER),
                                                         CAST(ROUND(c.gst_amount*100) AS INTEGER)
                                FROM invoice_payment_tax_components c
                                WHERE c.allocation_id=NEW.linked_allocation_id))))
                BEGIN SELECT RAISE(ABORT, 'Allocation reversal components do not match prior version'); END'''
    changed = _ensure_trigger(
        conn, "trg_allocation_history_reversal_components", reversal_components_sql
    ) or changed

    bank_delete_sql = '''CREATE TRIGGER "trg_bank_transactions_allocation_history_no_delete"
        BEFORE DELETE ON "bank_transactions"
        WHEN EXISTS (SELECT 1 FROM allocation_history_events e
                     WHERE e.bank_transaction_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'Bank transaction has allocation history'); END'''
    bank_update_sql = '''CREATE TRIGGER "trg_bank_transactions_allocation_history_identity"
        BEFORE UPDATE ON "bank_transactions"
        WHEN EXISTS (SELECT 1 FROM allocation_history_events e
                     WHERE e.bank_transaction_id=OLD.id)
          AND (NEW.id IS NOT OLD.id OR NEW.occurred_at IS NOT OLD.occurred_at)
        BEGIN SELECT RAISE(ABORT, 'Bank transaction identity is protected by allocation history'); END'''
    bank_replace_sql = '''CREATE TRIGGER "trg_bank_transactions_allocation_history_no_replace"
        BEFORE INSERT ON "bank_transactions"
        WHEN EXISTS (SELECT 1 FROM bank_transactions old
          WHERE (old.id=NEW.id OR (NEW.dedup_key IS NOT NULL
            AND old.bank_account_id=NEW.bank_account_id AND old.dedup_key=NEW.dedup_key))
            AND EXISTS (SELECT 1 FROM allocation_history_events e
                        WHERE e.bank_transaction_id=old.id))
        BEGIN SELECT RAISE(ABORT, 'Bank transaction has allocation history'); END'''
    if _table_exists(conn, "bank_transactions"):
        changed = _ensure_trigger(
            conn, "trg_bank_transactions_allocation_history_no_delete", bank_delete_sql
        ) or changed
        changed = _ensure_trigger(
            conn, "trg_bank_transactions_allocation_history_identity", bank_update_sql
        ) or changed
        changed = _ensure_trigger(
            conn, "trg_bank_transactions_allocation_history_no_replace", bank_replace_sql
        ) or changed
    return changed


def _ensure_allocation_batch_guards(conn) -> bool:
    """Install canonical transaction-wide allocation-batch database controls."""
    tables = (
        "allocation_batches",
        "allocation_batch_lines",
        "allocation_batch_tax_components",
        "allocation_batch_finalizations",
    )
    changed = False

    def ensure_if_ready(name: str, sql: str, dependencies: tuple[str, ...]) -> bool:
        if all(_table_exists(conn, table) for table in dependencies):
            return _ensure_trigger(conn, name, sql)
        if conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=:name"
        ), {"name": name}).first() is not None:
            conn.execute(text(f'DROP TRIGGER "{name}"'))
            return True
        return False

    for table in tables:
        if not _table_exists(conn, table):
            continue
        for operation in ("UPDATE", "DELETE"):
            name = f"trg_{table}_no_{operation.lower()}"
            sql = (
                f'CREATE TRIGGER "{name}" BEFORE {operation} ON "{table}" '
                "BEGIN SELECT RAISE(ABORT, 'Allocation batches are append-only'); END"
            )
            changed = _ensure_trigger(conn, name, sql) or changed

    replace_conditions = {
        "allocation_batches": (
            "EXISTS (SELECT 1 FROM allocation_batches WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM allocation_batches WHERE idempotency_key=NEW.idempotency_key)",
            "EXISTS (SELECT 1 FROM allocation_batches WHERE bank_transaction_id=NEW.bank_transaction_id "
            "AND transaction_version=NEW.transaction_version)",
        ),
        "allocation_batch_lines": (
            "EXISTS (SELECT 1 FROM allocation_batch_lines WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM allocation_batch_lines WHERE batch_id=NEW.batch_id "
            "AND line_order=NEW.line_order)",
            "EXISTS (SELECT 1 FROM allocation_batch_lines WHERE batch_id=NEW.batch_id "
            "AND source_kind=NEW.source_kind AND invoice_or_bill_id=NEW.invoice_or_bill_id)",
        ),
        "allocation_batch_tax_components": (
            "EXISTS (SELECT 1 FROM allocation_batch_tax_components WHERE id=NEW.id)",
            "EXISTS (SELECT 1 FROM allocation_batch_tax_components "
            "WHERE allocation_batch_line_id=NEW.allocation_batch_line_id AND tax_code=NEW.tax_code)",
        ),
        "allocation_batch_finalizations": (
            "EXISTS (SELECT 1 FROM allocation_batch_finalizations WHERE batch_id=NEW.batch_id)",
        ),
    }
    for table, conditions in replace_conditions.items():
        if not _table_exists(conn, table):
            continue
        name = f"trg_{table}_no_replace_insert"
        sql = (
            f'CREATE TRIGGER "{name}" BEFORE INSERT ON "{table}" '
            f'WHEN {" OR ".join(conditions)} '
            "BEGIN SELECT RAISE(ABORT, 'Allocation batches are append-only'); END"
        )
        changed = _ensure_trigger(conn, name, sql) or changed

    nonblank = lambda value: (
        f"length(trim(COALESCE({value}, ''), char(9)||char(10)||char(13)||' '))=0"
    )
    batch_sql = f'''CREATE TRIGGER "trg_allocation_batches_domain"
        BEFORE INSERT ON "allocation_batches"
        WHEN NOT EXISTS (SELECT 1 FROM bank_transactions t WHERE t.id=NEW.bank_transaction_id)
          OR NEW.operation NOT IN ('allocation_correction','allocation_reversal','allocation_reallocation')
          OR typeof(NEW.transaction_version)!='integer' OR NEW.transaction_version<=0
          OR typeof(NEW.expected_prior_version)!='integer' OR NEW.expected_prior_version<0
          OR NEW.transaction_version!=NEW.expected_prior_version+1
          OR NEW.transaction_version!=COALESCE((SELECT MAX(b.transaction_version)+1
              FROM allocation_batches b WHERE b.bank_transaction_id=NEW.bank_transaction_id),1)
          OR {nonblank('NEW.effective_date')} OR {_sqlite_invalid_iso_date('NEW.effective_date')}
          OR {nonblank('NEW.recorded_at')} OR julianday(NEW.recorded_at) IS NULL
          OR {nonblank('NEW.idempotency_key')}
          OR length(NEW.canonical_payload_hash)!=64
          OR lower(NEW.canonical_payload_hash) GLOB '*[^0-9a-f]*'
          OR {nonblank('NEW.reason')} OR {nonblank('NEW.evidence_reference')}
          OR {nonblank('NEW.actor_placeholder')}
          OR (NEW.evidence_sha256 IS NOT NULL AND (
              length(NEW.evidence_sha256)!=64 OR lower(NEW.evidence_sha256) GLOB '*[^0-9a-f]*'))
          OR NEW.manual_review_status NOT IN ('not_required','required','approved','rejected')
          OR typeof(NEW.residual_amount_cents)!='integer' OR NEW.residual_amount_cents<0
          OR ((NEW.residual_amount_cents=0) != (NEW.residual_account_id IS NULL))
                    OR (NEW.residual_amount_cents>0 AND NOT EXISTS (
                            SELECT 1 FROM accounts a
                            JOIN bank_transactions t ON t.id=NEW.bank_transaction_id
                            WHERE a.id=NEW.residual_account_id AND a.active=1
                                AND a.code NOT IN ('1100','1200','2000','2100')
                                AND NOT EXISTS (SELECT 1 FROM bank_accounts ba
                                        WHERE ba.ledger_account_id=a.id)
                                AND ((t.direction='in' AND a.type='LIABILITY')
                                    OR (t.direction='out' AND a.type='ASSET'))))
          OR NOT EXISTS (SELECT 1 FROM allocation_history_baseline_finalizations f
              WHERE f.baseline_event_id=NEW.baseline_finalization_id
                AND f.bank_transaction_id=NEW.bank_transaction_id AND f.status='complete'
                AND NEW.recorded_at>=f.observed_at
                AND NEW.effective_date>=f.observed_payment_date)
          OR (NEW.expected_prior_version=0 AND (
              NEW.linked_prior_batch_id IS NOT NULL OR NOT EXISTS (
                SELECT 1 FROM allocation_history_baseline_finalizations f
                WHERE f.baseline_event_id=NEW.baseline_finalization_id
                  AND f.gross_allocation_total_cents>=0)))
          OR (NEW.expected_prior_version>0 AND NOT EXISTS (
              SELECT 1 FROM allocation_batches prior
              WHERE prior.id=NEW.linked_prior_batch_id
                AND prior.bank_transaction_id=NEW.bank_transaction_id
                AND prior.transaction_version=NEW.expected_prior_version
                AND EXISTS (SELECT 1 FROM allocation_batch_finalizations pf
                    WHERE pf.batch_id=prior.id)))
          OR (NEW.operation='allocation_correction' AND (
              NEW.effective_date IS NOT (SELECT t.occurred_at FROM bank_transactions t
                  WHERE t.id=NEW.bank_transaction_id)
              OR (NEW.expected_prior_version>0 AND NOT EXISTS (
                  SELECT 1 FROM allocation_batches prior WHERE prior.id=NEW.linked_prior_batch_id
                    AND prior.operation='allocation_correction'))
              OR (EXISTS (SELECT 1 FROM bas_period_lodgment_markers m
                    WHERE NEW.effective_date BETWEEN m.period_start AND m.period_end)
                  AND NOT EXISTS (SELECT 1 FROM allocation_period_correction_approvals a
                    JOIN bas_period_lodgment_markers m ON m.id=a.period_marker_id
                    WHERE a.id=NEW.internal_correction_approval_id
                      AND a.approved_idempotency_key=NEW.idempotency_key
                                            AND NEW.effective_date BETWEEN m.period_start AND m.period_end
                                            AND (EXISTS (SELECT 1 FROM allocation_history_baseline_bindings bb
                                                    WHERE bb.baseline_event_id=NEW.baseline_finalization_id
                                                        AND bb.allocation_row_id=a.allocation_id)
                                                OR EXISTS (SELECT 1 FROM invoice_payment_allocations current_a
                                                    WHERE current_a.id=a.allocation_id
                                                        AND current_a.bank_transaction_id=NEW.bank_transaction_id))))))
          OR (NEW.operation='allocation_reversal' AND (
                            NEW.internal_correction_approval_id IS NOT NULL
                            OR
              (NEW.expected_prior_version>0 AND NOT EXISTS (
                  SELECT 1 FROM allocation_batches prior WHERE prior.id=NEW.linked_prior_batch_id
                    AND prior.operation IN ('allocation_correction','allocation_reallocation')
                    AND julianday(NEW.effective_date)>julianday(prior.effective_date)))
              OR (NEW.expected_prior_version=0 AND NOT EXISTS (
                  SELECT 1 FROM allocation_history_baseline_finalizations f
                  WHERE f.baseline_event_id=NEW.baseline_finalization_id
                    AND julianday(NEW.effective_date)>julianday(f.observed_payment_date)))
              OR EXISTS (SELECT 1 FROM bas_period_lodgment_markers m
                  WHERE NEW.effective_date BETWEEN m.period_start AND m.period_end)))
          OR (NEW.operation='allocation_reallocation' AND (
              NEW.expected_prior_version=0 OR NOT EXISTS (
                  SELECT 1 FROM allocation_batches prior WHERE prior.id=NEW.linked_prior_batch_id
                    AND prior.operation='allocation_reversal'
                    AND julianday(NEW.effective_date)>julianday(prior.effective_date))))
        BEGIN SELECT RAISE(ABORT, 'Invalid allocation batch'); END'''
    batch_dependencies = (
        "allocation_batches",
        "bank_transactions",
        "bank_accounts",
        "accounts",
        "allocation_history_baseline_finalizations",
        "allocation_batch_finalizations",
        "allocation_history_baseline_bindings",
        "bas_period_lodgment_markers",
        "allocation_period_correction_approvals",
        "invoice_payment_allocations",
    )
    changed = ensure_if_ready(
        "trg_allocation_batches_domain", batch_sql, batch_dependencies
    ) or changed

    line_sql = f'''CREATE TRIGGER "trg_allocation_batch_lines_domain"
        BEFORE INSERT ON "allocation_batch_lines"
        WHEN typeof(NEW.line_order)!='integer' OR NEW.line_order<0
          OR NEW.source_kind NOT IN ('invoice','bill')
          OR typeof(NEW.invoice_or_bill_id)!='integer' OR NEW.invoice_or_bill_id<=0
          OR typeof(NEW.gross_cents)!='integer' OR NEW.gross_cents<=0
          OR typeof(NEW.gst_cents)!='integer' OR NEW.gst_cents<0 OR NEW.gst_cents>NEW.gross_cents
          OR NOT EXISTS (SELECT 1 FROM allocation_batches b
              JOIN bank_transactions t ON t.id=b.bank_transaction_id
              JOIN invoices i ON i.id=NEW.invoice_or_bill_id
              WHERE b.id=NEW.batch_id
                AND ((NEW.source_kind='invoice' AND i.direction='AR' AND t.direction='in')
                  OR (NEW.source_kind='bill' AND i.direction='AP' AND t.direction='out')))
          OR EXISTS (SELECT 1 FROM allocation_batch_finalizations f WHERE f.batch_id=NEW.batch_id)
        BEGIN SELECT RAISE(ABORT, 'Invalid or finalized allocation batch line'); END'''
    line_dependencies = (
        "allocation_batch_lines",
        "allocation_batches",
        "bank_transactions",
        "invoices",
        "allocation_batch_finalizations",
    )
    changed = ensure_if_ready(
        "trg_allocation_batch_lines_domain", line_sql, line_dependencies
    ) or changed

    component_sql = f'''CREATE TRIGGER "trg_allocation_batch_tax_components_domain"
        BEFORE INSERT ON "allocation_batch_tax_components"
        WHEN NEW.tax_code NOT IN ('standard','gst_free','input_taxed','capital','none')
          OR {nonblank('NEW.tax_code')}
          OR typeof(NEW.gross_cents)!='integer' OR NEW.gross_cents<=0
          OR typeof(NEW.gst_cents)!='integer' OR NEW.gst_cents<0 OR NEW.gst_cents>NEW.gross_cents
          OR NOT EXISTS (SELECT 1 FROM allocation_batch_lines l WHERE l.id=NEW.allocation_batch_line_id)
          OR EXISTS (SELECT 1 FROM allocation_batch_lines l
              JOIN allocation_batch_finalizations f ON f.batch_id=l.batch_id
              WHERE l.id=NEW.allocation_batch_line_id)
        BEGIN SELECT RAISE(ABORT, 'Invalid or finalized allocation batch component'); END'''
    component_dependencies = (
        "allocation_batch_tax_components",
        "allocation_batch_lines",
        "allocation_batch_finalizations",
    )
    changed = ensure_if_ready(
        "trg_allocation_batch_tax_components_domain", component_sql,
        component_dependencies,
    ) or changed

    before_vector = '''SELECT source_kind, tax_code, SUM(gross_cents) gross_cents, SUM(gst_cents) gst_cents
        FROM (
          SELECT b.source_kind, tb.tax_code, tb.component_gross_cents gross_cents,
                 tb.component_gst_cents gst_cents
          FROM allocation_history_baseline_bindings b
          JOIN allocation_history_baseline_tax_bindings tb ON tb.allocation_binding_id=b.id
          WHERE b.baseline_event_id=(SELECT baseline_event_id FROM allocation_history_baseline_finalizations
              WHERE bank_transaction_id=(SELECT bank_transaction_id FROM allocation_batches WHERE id=NEW.batch_id))
            AND (SELECT expected_prior_version FROM allocation_batches WHERE id=NEW.batch_id)=0
          UNION ALL
          SELECT l.source_kind, c.tax_code, c.gross_cents, c.gst_cents
          FROM allocation_batch_lines l JOIN allocation_batch_tax_components c
            ON c.allocation_batch_line_id=l.id
          WHERE l.batch_id=(SELECT linked_prior_batch_id FROM allocation_batches WHERE id=NEW.batch_id)
            AND (SELECT expected_prior_version FROM allocation_batches WHERE id=NEW.batch_id)>0
        ) GROUP BY source_kind, tax_code'''
    after_vector = '''SELECT l.source_kind, c.tax_code, SUM(c.gross_cents) gross_cents,
               SUM(c.gst_cents) gst_cents
        FROM allocation_batch_lines l JOIN allocation_batch_tax_components c
          ON c.allocation_batch_line_id=l.id
        WHERE l.batch_id=NEW.batch_id GROUP BY l.source_kind, c.tax_code'''
    vector_diff = (
        f"EXISTS (SELECT * FROM ({before_vector}) before_rows "
        f"EXCEPT SELECT * FROM ({after_vector}) after_rows) OR "
        f"EXISTS (SELECT * FROM ({after_vector}) after_rows "
        f"EXCEPT SELECT * FROM ({before_vector}) before_rows)"
    )

    batch_hash_rows = '''SELECT json_object('kind','batch',
          'bank_transaction_id',b.bank_transaction_id,'operation',b.operation,
          'transaction_version',b.transaction_version,'expected_prior_version',b.expected_prior_version,
          'effective_date',b.effective_date,'recorded_at',b.recorded_at,
          'idempotency_key',b.idempotency_key,'reason',b.reason,
          'evidence_reference',b.evidence_reference,'evidence_sha256',b.evidence_sha256,
          'actor_placeholder',b.actor_placeholder,'linked_prior_batch_id',b.linked_prior_batch_id,
          'internal_correction_approval_id',b.internal_correction_approval_id,
          'manual_review_status',b.manual_review_status,
          'residual_amount_cents',b.residual_amount_cents,'residual_account_id',b.residual_account_id,
          'baseline_finalization_id',b.baseline_finalization_id) AS payload
        FROM allocation_batches b WHERE b.id=NEW.batch_id
        UNION ALL SELECT json_object('kind','line','line_order',l.line_order,
          'source_kind',l.source_kind,'invoice_or_bill_id',l.invoice_or_bill_id,
          'gross_cents',l.gross_cents,'gst_cents',l.gst_cents) AS payload
        FROM allocation_batch_lines l WHERE l.batch_id=NEW.batch_id
        UNION ALL SELECT json_object('kind','component','line_order',l.line_order,
          'tax_code',c.tax_code,'gross_cents',c.gross_cents,'gst_cents',c.gst_cents) AS payload
        FROM allocation_batch_lines l JOIN allocation_batch_tax_components c
          ON c.allocation_batch_line_id=l.id WHERE l.batch_id=NEW.batch_id'''
    integrity_hash = f"(SELECT allocation_baseline_sha256(payload) FROM ({batch_hash_rows}) AS hash_rows)"

    finalization_sql = f'''CREATE TRIGGER "trg_allocation_batch_finalizations_domain"
        BEFORE INSERT ON "allocation_batch_finalizations"
        WHEN typeof(NEW.allocation_line_count)!='integer' OR NEW.allocation_line_count<0
          OR typeof(NEW.component_count)!='integer' OR NEW.component_count<0
          OR typeof(NEW.total_allocated_cents)!='integer' OR NEW.total_allocated_cents<0
          OR typeof(NEW.total_gst_cents)!='integer' OR NEW.total_gst_cents<0
          OR typeof(NEW.residual_cents)!='integer' OR NEW.residual_cents<0
          OR typeof(NEW.bank_cash_cents)!='integer' OR NEW.bank_cash_cents<=0
          OR length(NEW.canonical_integrity_hash)!=64
          OR lower(NEW.canonical_integrity_hash) GLOB '*[^0-9a-f]*'
          OR {nonblank('NEW.finalized_at')} OR julianday(NEW.finalized_at) IS NULL
          OR NOT EXISTS (SELECT 1 FROM allocation_batches b WHERE b.id=NEW.batch_id)
          OR NEW.allocation_line_count!=(SELECT COUNT(*) FROM allocation_batch_lines l WHERE l.batch_id=NEW.batch_id)
          OR NEW.component_count!=(SELECT COUNT(*) FROM allocation_batch_lines l
              JOIN allocation_batch_tax_components c ON c.allocation_batch_line_id=l.id
              WHERE l.batch_id=NEW.batch_id)
          OR NEW.total_allocated_cents!=COALESCE((SELECT SUM(gross_cents)
              FROM allocation_batch_lines WHERE batch_id=NEW.batch_id),0)
          OR NEW.total_gst_cents!=COALESCE((SELECT SUM(gst_cents)
              FROM allocation_batch_lines WHERE batch_id=NEW.batch_id),0)
          OR NEW.residual_cents!=(SELECT residual_amount_cents FROM allocation_batches WHERE id=NEW.batch_id)
          OR allocation_baseline_cents((SELECT amount FROM bank_transactions
              WHERE id=(SELECT bank_transaction_id FROM allocation_batches WHERE id=NEW.batch_id))) IS NULL
          OR NEW.bank_cash_cents!=allocation_baseline_cents((SELECT amount FROM bank_transactions
              WHERE id=(SELECT bank_transaction_id FROM allocation_batches WHERE id=NEW.batch_id)))
          OR NEW.bank_cash_cents!=NEW.total_allocated_cents+NEW.residual_cents
          OR NEW.canonical_integrity_hash!={integrity_hash}
          OR NEW.canonical_integrity_hash!=(SELECT canonical_payload_hash
              FROM allocation_batches WHERE id=NEW.batch_id)
          OR EXISTS (SELECT 1 FROM allocation_batch_lines l WHERE l.batch_id=NEW.batch_id
              AND ((SELECT COUNT(*) FROM allocation_batch_tax_components c
                    WHERE c.allocation_batch_line_id=l.id)=0
                OR l.gross_cents!=(SELECT COALESCE(SUM(c.gross_cents),-1)
                    FROM allocation_batch_tax_components c WHERE c.allocation_batch_line_id=l.id)
                OR l.gst_cents!=(SELECT COALESCE(SUM(c.gst_cents),-1)
                    FROM allocation_batch_tax_components c WHERE c.allocation_batch_line_id=l.id)))
          OR (({vector_diff}) AND (SELECT manual_review_status FROM allocation_batches
              WHERE id=NEW.batch_id) NOT IN ('required','approved'))
          OR (NOT ({vector_diff}) AND (SELECT manual_review_status FROM allocation_batches
              WHERE id=NEW.batch_id)='required')
          OR (({vector_diff}) AND (SELECT manual_review_status FROM allocation_batches
              WHERE id=NEW.batch_id)!='approved')
          OR ((NOT ({vector_diff})) AND (SELECT manual_review_status FROM allocation_batches
              WHERE id=NEW.batch_id) NOT IN ('not_required','approved'))
          OR ((SELECT operation FROM allocation_batches WHERE id=NEW.batch_id)='allocation_reversal'
              AND EXISTS (SELECT 1 FROM allocation_batch_lines l
                WHERE l.batch_id=NEW.batch_id AND NOT EXISTS (
                  SELECT 1 FROM allocation_batch_lines prior_l
                  JOIN allocation_batch_tax_components prior_c
                    ON prior_c.allocation_batch_line_id=prior_l.id
                  WHERE prior_l.batch_id=(SELECT linked_prior_batch_id FROM allocation_batches
                    WHERE id=NEW.batch_id)
                    AND prior_l.source_kind=l.source_kind
                    AND prior_l.invoice_or_bill_id=l.invoice_or_bill_id
                                        AND prior_l.gross_cents>=l.gross_cents AND prior_l.gst_cents>=l.gst_cents
                                    UNION ALL
                                    SELECT 1 FROM allocation_history_baseline_bindings base_l
                                    WHERE base_l.baseline_event_id=(SELECT baseline_finalization_id
                                            FROM allocation_batches WHERE id=NEW.batch_id)
                                        AND base_l.source_kind=l.source_kind
                                        AND base_l.invoice_or_bill_id=l.invoice_or_bill_id
                                        AND base_l.allocated_gross_cents>=l.gross_cents
                                        AND base_l.allocated_gst_cents>=l.gst_cents
                                        AND (SELECT expected_prior_version FROM allocation_batches
                                            WHERE id=NEW.batch_id)=0)))
                    OR ((SELECT operation FROM allocation_batches WHERE id=NEW.batch_id)='allocation_reversal'
                            AND EXISTS (SELECT 1 FROM allocation_batch_lines l
                                JOIN allocation_batch_tax_components c ON c.allocation_batch_line_id=l.id
                                WHERE l.batch_id=NEW.batch_id AND NOT EXISTS (
                                    SELECT 1 FROM allocation_batch_lines prior_l
                                    JOIN allocation_batch_tax_components prior_c
                                        ON prior_c.allocation_batch_line_id=prior_l.id
                                    WHERE prior_l.batch_id=(SELECT linked_prior_batch_id FROM allocation_batches
                                        WHERE id=NEW.batch_id)
                                        AND prior_l.source_kind=l.source_kind
                                        AND prior_l.invoice_or_bill_id=l.invoice_or_bill_id
                                        AND prior_c.tax_code=c.tax_code
                                        AND prior_c.gross_cents>=c.gross_cents AND prior_c.gst_cents>=c.gst_cents
                                    UNION ALL
                                    SELECT 1 FROM allocation_history_baseline_bindings base_l
                                    JOIN allocation_history_baseline_tax_bindings base_c
                                        ON base_c.allocation_binding_id=base_l.id
                                    WHERE base_l.baseline_event_id=(SELECT baseline_finalization_id
                                            FROM allocation_batches WHERE id=NEW.batch_id)
                                        AND base_l.source_kind=l.source_kind
                                        AND base_l.invoice_or_bill_id=l.invoice_or_bill_id
                                        AND base_c.tax_code=c.tax_code
                                        AND base_c.component_gross_cents>=c.gross_cents
                                        AND base_c.component_gst_cents>=c.gst_cents
                                        AND (SELECT expected_prior_version FROM allocation_batches
                                            WHERE id=NEW.batch_id)=0)))
        BEGIN SELECT RAISE(ABORT, 'Allocation batch does not reconcile or requires manual GST review'); END'''
    finalization_dependencies = (
        "allocation_batch_finalizations",
        "allocation_batches",
        "allocation_batch_lines",
        "allocation_batch_tax_components",
        "bank_transactions",
        "allocation_history_baseline_finalizations",
        "allocation_history_baseline_bindings",
        "allocation_history_baseline_tax_bindings",
    )
    changed = ensure_if_ready(
        "trg_allocation_batch_finalizations_domain", finalization_sql,
        finalization_dependencies,
    ) or changed

    bank_cash_sql = '''CREATE TRIGGER "trg_bank_transactions_allocation_batch_cash_immutable"
        BEFORE UPDATE ON "bank_transactions"
        WHEN EXISTS (SELECT 1 FROM allocation_batches b WHERE b.bank_transaction_id=OLD.id)
          AND (NEW.amount IS NOT OLD.amount OR NEW.direction IS NOT OLD.direction)
        BEGIN SELECT RAISE(ABORT, 'Bank cash is immutable after allocation batching'); END'''
    bank_trigger_dependencies = ("bank_transactions", "allocation_batches")
    changed = ensure_if_ready(
        "trg_bank_transactions_allocation_batch_cash_immutable", bank_cash_sql,
        bank_trigger_dependencies,
    ) or changed
    bank_delete_sql = '''CREATE TRIGGER "trg_bank_transactions_allocation_batch_no_delete"
        BEFORE DELETE ON "bank_transactions"
        WHEN EXISTS (SELECT 1 FROM allocation_batches b WHERE b.bank_transaction_id=OLD.id)
        BEGIN SELECT RAISE(ABORT, 'Bank transaction has allocation batches'); END'''
    changed = ensure_if_ready(
        "trg_bank_transactions_allocation_batch_no_delete", bank_delete_sql,
        bank_trigger_dependencies,
    ) or changed
    bank_replace_sql = '''CREATE TRIGGER "trg_bank_transactions_allocation_batch_no_replace"
        BEFORE INSERT ON "bank_transactions"
        WHEN EXISTS (SELECT 1 FROM bank_transactions prior
          WHERE (prior.id=NEW.id OR (NEW.dedup_key IS NOT NULL
            AND prior.bank_account_id=NEW.bank_account_id AND prior.dedup_key=NEW.dedup_key))
            AND EXISTS (SELECT 1 FROM allocation_batches b
              WHERE b.bank_transaction_id=prior.id))
        BEGIN SELECT RAISE(ABORT, 'Bank transaction has allocation batches'); END'''
    changed = ensure_if_ready(
        "trg_bank_transactions_allocation_batch_no_replace", bank_replace_sql,
        bank_trigger_dependencies,
    ) or changed
    return changed


def _drop_allocation_batch_bank_transaction_triggers(conn) -> tuple[str, ...]:
    """Drop all owned batch triggers whose SQL depends on bank_transactions."""
    rows = conn.execute(text(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
        "AND (lower(name) GLOB 'trg_allocation_batch*' "
        "OR lower(name) GLOB 'trg_bank_transactions_allocation_batch*')"
    )).fetchall()
    dropped = []
    for name, sql in rows:
        if "bank_transactions" not in (sql or "").lower():
            continue
        conn.execute(text(f'DROP TRIGGER "{name}"'))
        dropped.append(name)
    return tuple(dropped)


def _drop_allocation_batch_triggers(conn) -> tuple[str, ...]:
    """Drop all owned batch triggers before recreating empty partial tables."""
    rows = conn.execute(text(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
        "AND (lower(name) GLOB 'trg_allocation_batch*' "
        "OR lower(name) GLOB 'trg_bank_transactions_allocation_batch*')"
    )).fetchall()
    dropped = []
    for name, sql in rows:
        conn.execute(text(f'DROP TRIGGER "{name}"'))
        dropped.append(name)
    return tuple(dropped)


_BANK_DEDUP_INDEX = SQLiteIndexSignature(
    "uq_bank_txn_dedup",
    ("bank_account_id", "dedup_key"),
    unique=True,
    where="dedup_key IS NOT NULL",
)


def _required_unique_indexes() -> dict[str, tuple[SQLiteIndexSignature, ...]]:
    required = dict(MIGRATION_INDEX_SIGNATURES)
    required["bank_transactions"] = (_BANK_DEDUP_INDEX,)
    return required


def _table_has_rows(conn, table: str) -> bool:
    return (
        conn.execute(text(f'SELECT 1 FROM "{table}" LIMIT 1')).fetchone()
        is not None
    )


def _bank_constraint_mismatches(conn) -> list[str]:
    if not _table_exists(conn, "bank_transactions"):
        return []
    from ..models.company import BankTransaction

    return table_signature_mismatches(
        conn,
        inspect(conn),
        BankTransaction.__table__,
        include_indexes=False,
    )


def _bank_needs_rebuild(conn) -> bool:
    if not _table_exists(conn, "bank_transactions"):
        return False
    return bool(
        _bank_constraint_mismatches(conn)
        or "linked_trust_entry_id" in _existing_columns(conn, "bank_transactions")
    )


def _bank_rebuild_data_violations(conn) -> list[str]:
    """Return unsafe rows that must be resolved before a constraint rebuild."""

    table = "bank_transactions"
    if not _table_exists(conn, table):
        return []
    columns = _existing_columns(conn, table)
    required = {
        "id",
        "bank_account_id",
        "direction",
        "amount",
        "occurred_at",
        "gst_amount",
        "tax_code",
        "created_at",
    }
    missing = sorted(required - columns)
    if missing:
        return [f"missing required columns {missing!r}"]

    violations: list[str] = []
    unapplied_columns = {"unapplied_account_id", "unapplied_amount"}
    present_unapplied_columns = unapplied_columns & columns
    if present_unapplied_columns and present_unapplied_columns != unapplied_columns:
        violations.append(
            "the unapplied-cash column pair is only partially present: "
            + repr(sorted(present_unapplied_columns))
        )
    null_predicate = " OR ".join(f'"{column}" IS NULL' for column in sorted(required))
    null_count = conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {null_predicate}")
    ).scalar_one()
    if null_count:
        violations.append(f"{null_count} row(s) contain NULL in required columns")

    duplicate_ids = conn.execute(
        text(
            f"SELECT COUNT(*) FROM (SELECT id FROM {table} "
            "WHERE id IS NOT NULL GROUP BY id HAVING COUNT(*) > 1)"
        )
    ).scalar_one()
    if duplicate_ids:
        violations.append(f"{duplicate_ids} duplicated id value(s)")

    bad_amount = conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE amount <= 0")
    ).scalar_one()
    if bad_amount:
        violations.append(f"{bad_amount} row(s) have non-positive amount")
    bad_gst = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE gst_amount < 0 OR gst_amount > amount"
        )
    ).scalar_one()
    if bad_gst:
        violations.append(f"{bad_gst} row(s) have invalid GST bounds")
    if present_unapplied_columns == unapplied_columns:
        bad_unapplied = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {table} WHERE "
                "unapplied_amount < 0 OR unapplied_amount > amount OR "
                "(unapplied_amount = 0 AND unapplied_account_id IS NOT NULL) OR "
                "(unapplied_amount > 0 AND unapplied_account_id IS NULL)"
            )
        ).scalar_one()
        if bad_unapplied:
            violations.append(
                f"{bad_unapplied} row(s) have invalid unapplied-cash bounds/account"
            )

    orphan_banks = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {table} AS txn "
            "LEFT JOIN bank_accounts AS bank ON bank.id = txn.bank_account_id "
            "WHERE txn.bank_account_id IS NOT NULL AND bank.id IS NULL"
        )
    ).scalar_one()
    if orphan_banks:
        violations.append(f"{orphan_banks} row(s) reference a missing bank account")
    orphan_accounts = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {table} AS txn "
            "LEFT JOIN accounts AS account ON account.id = txn.account_id "
            "WHERE txn.account_id IS NOT NULL AND account.id IS NULL"
        )
    ).scalar_one()
    if orphan_accounts:
        violations.append(f"{orphan_accounts} row(s) reference a missing account")
    if present_unapplied_columns == unapplied_columns:
        orphan_unapplied_accounts = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {table} AS txn "
                "LEFT JOIN accounts AS account ON account.id = txn.unapplied_account_id "
                "WHERE txn.unapplied_account_id IS NOT NULL AND account.id IS NULL"
            )
        ).scalar_one()
        if orphan_unapplied_accounts:
            violations.append(
                f"{orphan_unapplied_accounts} row(s) reference a missing unapplied account"
            )
    duplicate_dedup = conn.execute(
        text(
            f"SELECT COUNT(*) FROM (SELECT bank_account_id, dedup_key FROM {table} "
            "WHERE dedup_key IS NOT NULL GROUP BY bank_account_id, dedup_key "
            "HAVING COUNT(*) > 1)"
        )
    ).scalar_one()
    if duplicate_dedup:
        violations.append(f"{duplicate_dedup} duplicated import dedup key(s)")
    return violations


def _duplicate_groups_for_index(
    conn, table: str, signature: SQLiteIndexSignature
) -> int:
    columns = ", ".join(f'"{column}"' for column in signature.columns)
    where = f" WHERE {signature.where}" if signature.where else ""
    return int(
        conn.execute(
            text(
                f'SELECT COUNT(*) FROM (SELECT {columns} FROM "{table}"'
                f"{where} GROUP BY {columns} HAVING COUNT(*) > 1)"
            )
        ).scalar_one()
    )


def _create_index_sql(table: str, signature: SQLiteIndexSignature) -> str:
    unique = "UNIQUE " if signature.unique else ""
    columns = ", ".join(f'"{column}"' for column in signature.columns)
    where = f" WHERE {signature.where}" if signature.where else ""
    return (
        f'CREATE {unique}INDEX "{signature.name}" ON "{table}" '
        f"({columns}){where}"
    )


def _repair_named_index(
    conn,
    *,
    table: str,
    signature: SQLiteIndexSignature,
) -> bool:
    """Safely create or replace one expected named index."""

    if not _table_exists(conn, table):
        return False
    columns = _existing_columns(conn, table)
    if not set(signature.columns).issubset(columns):
        return False
    if sqlite_index_matches(conn, table, signature):
        return False
    if signature.unique:
        duplicates = _duplicate_groups_for_index(conn, table, signature)
        if duplicates:
            duplicate_detail = f"found {duplicates} duplicated key group(s)"
            if table == "accounts" and signature.columns == ("code",):
                duplicate_code = conn.execute(
                    text(
                        "SELECT code FROM accounts GROUP BY code "
                        "HAVING COUNT(*) > 1 ORDER BY code LIMIT 1"
                    )
                ).scalar_one()
                duplicate_detail = f"code {duplicate_code} is not unique"
            raise DataRecoveryRequiredError(
                f"Cannot repair unique index {signature.name} on {table}: "
                f"{duplicate_detail}. A pre-repair "
                "backup was preserved; resolve the duplicates through an "
                "operator-reviewed recovery workflow."
            )
    if _index_exists(conn, signature.name):
        conn.execute(text(f'DROP INDEX "{signature.name}"'))
    conn.execute(text(_create_index_sql(table, signature)))
    if not sqlite_index_matches(conn, table, signature):
        raise RuntimeError(
            f"Index repair did not produce the required signature: "
            f"{table}.{signature.name}"
        )
    return True


def _load_company_model_tables():
    # The caller controls whether the optional documents module is registered.
    # Never mutate CompanyBase.metadata after create_all has already run.
    from ..models import company as _company_models  # noqa: F401

    return CompanyBase.metadata.tables


def _pending_index_repairs(conn) -> list[tuple[str, SQLiteIndexSignature]]:
    pending: dict[tuple[str, str], tuple[str, SQLiteIndexSignature]] = {}
    for table_name, signatures in _required_unique_indexes().items():
        if not _table_exists(conn, table_name):
            continue
        for signature in signatures:
            if not sqlite_index_matches(conn, table_name, signature):
                pending[(table_name, signature.name)] = (table_name, signature)
    for table in _load_company_model_tables().values():
        if not _table_exists(conn, table.name):
            continue
        for signature in model_sqlite_indexes(table).values():
            if not sqlite_index_matches(conn, table.name, signature):
                pending[(table.name, signature.name)] = (table.name, signature)
    return list(pending.values())


def _pending_unexpected_nonunique_indexes(conn) -> list[tuple[str, str]]:
    pending: list[tuple[str, str]] = []
    for table in _load_company_model_tables().values():
        if not _table_exists(conn, table.name):
            continue
        expected = set(model_sqlite_indexes(table))
        for name, signature in live_sqlite_indexes(conn, table.name).items():
            if name.startswith("sqlite_autoindex_") or name in expected:
                continue
            if not signature.unique:
                pending.append((table.name, name))
    return pending


def _constraint_repair_backup_reasons(
    conn, *, include_unrepaired_schema: bool = False
) -> list[str]:
    reasons: list[str] = []
    if _bank_needs_rebuild(conn):
        reasons.append("bank_transactions_constraints")
    for table, signature in _pending_index_repairs(conn):
        # Missing/wrong indexes on empty fresh tables carry no data risk. A
        # populated table receives a recovery point before any index mutation
        # or duplicate preflight failure.
        if _table_has_rows(conn, table):
            reasons.append(f"index:{table}.{signature.name}")
    for table, name in _pending_unexpected_nonunique_indexes(conn):
        if _table_has_rows(conn, table):
            reasons.append(f"drop_index:{table}.{name}")
    if include_unrepaired_schema:
        for table, problems in _unrepaired_table_signature_problems(conn):
            if problems and _table_has_rows(conn, table):
                reasons.append(f"schema:{table}")
    return reasons


def _unrepaired_table_signature_problems(conn) -> list[tuple[str, list[str]]]:
    """Constraint drift that targeted migrations do not safely auto-rebuild."""

    inspector = inspect(conn)
    problems: list[tuple[str, list[str]]] = []
    for table in _load_company_model_tables().values():
        if not _table_exists(conn, table.name):
            continue
        mismatches = table_signature_mismatches(conn, inspector, table)
        if mismatches:
            problems.append((table.name, mismatches))
    return problems


def _require_no_unrepaired_table_signature_drift(conn) -> None:
    problems = _unrepaired_table_signature_problems(conn)
    if not problems:
        return
    details = "; ".join(
        f"{table}: {', '.join(mismatches[:3])}"
        for table, mismatches in problems[:5]
    )
    raise DataRecoveryRequiredError(
        "Company schema still has unsafe constraint drift after targeted "
        f"repairs ({details}). A pre-repair backup was preserved for any "
        "populated affected table. Startup refused to apply data backfills or "
        "serve writes; use an explicit table-rebuild recovery workflow."
    )


def _system_account_reconciliation_plan(conn) -> list[dict]:
    """Describe canonical account rows that startup must create or repair.

    Existing names, GST display flags, hierarchy and descriptions are not part
    of the runtime identity: posting and reporting resolve these accounts by
    code. Only code presence, type and active state are reconciled.
    """
    if not _table_exists(conn, "accounts"):
        raise RuntimeError("Cannot reconcile system accounts: accounts table is missing")

    required_columns = {"id", "code", "name", "type", "is_gst", "active"}
    missing_columns = required_columns - _existing_columns(conn, "accounts")
    if missing_columns:
        raise RuntimeError(
            "Cannot reconcile system accounts: accounts table is missing columns "
            + ", ".join(sorted(missing_columns))
        )

    rows_by_code: dict[str, list[dict]] = {
        code: [] for code in SYSTEM_ACCOUNT_SPECS
    }
    rows = conn.execute(
        text("SELECT id, code, name, type, is_gst, active FROM accounts")
    ).mappings()
    for row in rows:
        code = row["code"]
        if code in rows_by_code:
            rows_by_code[code].append(dict(row))

    plan: list[dict] = []
    for code, spec in SYSTEM_ACCOUNT_SPECS.items():
        matches = rows_by_code[code]
        if len(matches) > 1:
            raise RuntimeError(
                f"System account code {code} is not unique ({len(matches)} rows); "
                "reconciliation aborted without changing the database"
            )
        if not matches:
            plan.append({"code": code, "action": "create", "row": None})
            continue

        row = matches[0]
        wrong_type = row["type"] != spec.type.value
        inactive = not bool(row["active"])
        if wrong_type or inactive:
            plan.append(
                {
                    "code": code,
                    "action": "repair",
                    "row": row,
                    "wrong_type": wrong_type,
                    "inactive": inactive,
                }
            )
    return plan


def _destructive_backup_reasons(
    conn,
    *,
    include_system_accounts: bool = False,
    include_unrepaired_schema: bool = False,
) -> list[str]:
    """Describe pending destructive work that warrants an online backup.

    The outgoing rebuild can filter rows even when the retired feature tables
    are absent.  Conversely, an older database may already have the lean
    outgoing shape while still carrying populated tables that step 1 drops.
    Both cases, plus an explicitly requested system-account identity repair,
    need the same pre-migration recovery point.
    """
    reasons: list[str] = []
    if _table_exists(conn, "outgoing_documents") and (
        REMOVED_OUTDOC_COLUMNS & _existing_columns(conn, "outgoing_documents")
    ):
        reasons.append("outgoing_documents")

    for table, _indexes in COMPANY_DB_TABLE_DROPS:
        if not _table_exists(conn, table):
            continue
        has_rows = conn.execute(
            text(f'SELECT 1 FROM "{table}" LIMIT 1')
        ).fetchone()
        if has_rows is not None:
            reasons.append(f"drop_table:{table}")
    if include_system_accounts and _system_account_reconciliation_plan(conn):
        reasons.append("system_accounts")
    if _payment_reconciliation_counts(conn):
        reasons.append("payment_reconciliation")
    reasons.extend(
        _constraint_repair_backup_reasons(
            conn,
            include_unrepaired_schema=include_unrepaired_schema,
        )
    )
    # Keep the backup log stable if one table needs more than one index repair.
    return list(dict.fromkeys(reasons))


def _create_destructive_migration_backup(
    engine: Engine,
    *,
    include_system_accounts: bool = False,
    include_unrepaired_schema: bool = False,
) -> tuple[Path | None, list[str]]:
    """Create a consistent backup before destructive work or identity repair.

    The SQLite online-backup API includes committed WAL contents and is safe while
    other readers exist.  In-memory databases have no durable file to preserve,
    so test-only/in-memory engines intentionally return ``None``.

    A new timestamped file is allocated with O_EXCL for every attempted legacy
    migration attempt.  A failed attempt therefore never overwrites the recovery
    point made by an earlier attempt.
    """
    if engine.dialect.name != "sqlite":
        return None, []
    database = engine.url.database
    if not database or database == ":memory:":
        return None, []

    db_path = Path(database).resolve()
    if not db_path.is_file():
        return None, []

    with engine.connect() as conn:
        reasons = _destructive_backup_reasons(
            conn,
            include_system_accounts=include_system_accounts,
            include_unrepaired_schema=include_unrepaired_schema,
        )
        if not reasons:
            return None, []

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path: Path | None = None
        for suffix in range(1000):
            suffix_text = "" if suffix == 0 else f"-{suffix}"
            candidate = db_path.with_name(
                f"{db_path.name}.pre-destructive-migration-"
                f"{stamp}{suffix_text}.bak"
            )
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            else:
                os.close(fd)
                backup_path = candidate
                break
        if backup_path is None:
            raise RuntimeError("Could not allocate a unique migration backup")

        try:
            raw_source = conn.connection.driver_connection
            with sqlite3.connect(backup_path) as destination:
                raw_source.backup(destination)
                result = destination.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise RuntimeError(
                        "Migration backup failed SQLite integrity_check"
                    )
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise

    return backup_path, reasons


def _assert_sqlite_integrity(conn, *, context: str) -> None:
    """Fail the migration before commit if SQLite or FK integrity is broken."""
    integrity_rows = conn.execute(text("PRAGMA integrity_check")).fetchall()
    if not integrity_rows or any(row[0] != "ok" for row in integrity_rows):
        raise DataRecoveryRequiredError(
            f"SQLite integrity_check failed after {context}. Restore from a "
            "verified backup or use an operator-reviewed recovery workflow."
        )

    try:
        fk_violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    except Exception as exc:
        raise DataRecoveryRequiredError(
            f"SQLite foreign_key_check could not validate the schema after "
            f"{context}: {exc}. A referenced parent key or FK declaration is "
            "structurally invalid; startup refused to continue."
        ) from exc
    if fk_violations:
        raise DataRecoveryRequiredError(
            f"SQLite foreign_key_check found {len(fk_violations)} violation(s) "
            f"after {context}. Restore or repair the orphaned rows before retrying."
        )


@contextmanager
def _migration_transaction(
    engine: Engine, *, foreign_keys_off: bool = False
) -> Generator:
    """Run migration work transactionally and never poison a pooled connection.

    SQLite ignores ``PRAGMA foreign_keys = ON`` while a transaction is active.
    Reassert it after both commit and rollback, in AUTOCOMMIT mode, before the
    connection can return to the pool.
    """
    conn = engine.connect()
    try:
        try:
            if foreign_keys_off:
                raw = conn.connection.driver_connection
                raw.execute("PRAGMA foreign_keys = OFF")
                if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                    raise RuntimeError(
                        "Migration rebuild requires foreign_keys=OFF before its transaction"
                    )
            with conn.begin():
                if foreign_keys_off and conn.dialect.name == "sqlite":
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                    if not conn.connection.driver_connection.in_transaction:
                        raise RuntimeError(
                            "SQLite migration rebuild did not start a database transaction"
                        )
                yield conn
        finally:
            # Restore the exact DBAPI connection that ran the migration before
            # it can return to the pool.  Opening a second Engine connection is
            # insufficient when the pool has more than one live checkout.
            raw = conn.connection.driver_connection
            # A failed SQLite DDL sequence can leave the DBAPI connection in a
            # transaction even after SQLAlchemy unwinds its context.  PRAGMA
            # foreign_keys=ON is ignored in that state, so clear it explicitly
            # before returning the connection to the pool.
            if getattr(raw, "in_transaction", False):
                raw.rollback()
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
            if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError(
                    "Could not restore SQLite foreign-key enforcement after migration"
                )
    finally:
        conn.close()


def _bank_accounts_need_rebuild(conn) -> bool:
    if not _table_exists(conn, "bank_accounts"):
        return False
    if "ledger_account_id" not in _existing_columns(conn, "bank_accounts"):
        return True
    foreign_keys = conn.execute(
        text("PRAGMA foreign_key_list(bank_accounts)")
    ).fetchall()
    has_restrict_fk = any(
        row[2] == "accounts"
        and row[3] == "ledger_account_id"
        and row[6].upper() == "RESTRICT"
        for row in foreign_keys
    )
    unique_mapping = False
    for row in conn.execute(text("PRAGMA index_list(bank_accounts)")).fetchall():
        if not row[2]:
            continue
        index_name = str(row[1]).replace('"', '""')
        columns = conn.execute(text(f'PRAGMA index_info("{index_name}")')).fetchall()
        if [item[2] for item in columns] == ["ledger_account_id"]:
            unique_mapping = True
            break
    return not (has_restrict_fk and unique_mapping)


def _rebuild_bank_accounts(conn) -> None:
    """Apply the mapping FK/unique constraint while preserving bank rows."""
    from ..models.company import BankAccount

    old_columns = _existing_columns(conn, "bank_accounts")
    if "ledger_account_id" in old_columns:
        duplicate = conn.execute(
            text(
                "SELECT ledger_account_id FROM bank_accounts "
                "WHERE ledger_account_id IS NOT NULL GROUP BY ledger_account_id "
                "HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        if duplicate is not None:
            raise DataRecoveryRequiredError(
                "Cannot add unique bank ledger mappings: multiple bank accounts already "
                f"reference account {duplicate[0]}"
            )

    model_columns = [column.name for column in BankAccount.__table__.columns]
    copied_columns = [column for column in model_columns if column in old_columns]
    high_water_id = _autoincrement_high_water(conn, "bank_accounts")
    index_rows = conn.execute(
        text(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='bank_accounts' AND sql IS NOT NULL"
        )
    ).fetchall()
    index_sqls = [row[0] for row in index_rows]

    ddl = str(CreateTable(BankAccount.__table__).compile(dialect=conn.dialect))
    ddl = ddl.replace("CREATE TABLE bank_accounts", "CREATE TABLE new_bank_accounts", 1)
    conn.execute(text("DROP TABLE IF EXISTS new_bank_accounts"))
    conn.execute(text(ddl))
    quoted = ", ".join(f'"{column}"' for column in copied_columns)
    conn.execute(
        text(
            f"INSERT INTO new_bank_accounts ({quoted}) "
            f"SELECT {quoted} FROM bank_accounts"
        )
    )
    conn.execute(text("DROP TABLE bank_accounts"))
    conn.execute(text("ALTER TABLE new_bank_accounts RENAME TO bank_accounts"))
    _restore_autoincrement_high_water(
        conn,
        table="bank_accounts",
        helper_table="new_bank_accounts",
        high_water_id=high_water_id,
    )
    for index_sql in index_sqls:
        conn.execute(text(index_sql))


def _backfill_bank_account_ledger_mapping(conn) -> int:
    if not _table_exists(conn, "bank_accounts") or not _table_exists(conn, "accounts"):
        return 0
    bank_count = conn.execute(text("SELECT COUNT(*) FROM bank_accounts")).scalar_one()
    if bank_count != 1:
        return 0
    bank = conn.execute(
        text("SELECT id, ledger_account_id FROM bank_accounts LIMIT 1")
    ).one()
    if bank.ledger_account_id is not None:
        return 0
    accounts = conn.execute(
        text(
            "SELECT id FROM accounts WHERE code = '1000' AND active = 1 "
            "AND type = 'ASSET' AND is_gst = 0"
        )
    ).fetchall()
    if len(accounts) != 1:
        return 0
    result = conn.execute(
        text(
            "UPDATE bank_accounts SET ledger_account_id = :account_id "
            "WHERE id = :bank_id AND ledger_account_id IS NULL"
        ),
        {"account_id": accounts[0][0], "bank_id": bank.id},
    )
    return int(result.rowcount or 0)


def _apply_system_account_reconciliation(conn) -> list[str]:
    """Apply one freshly-computed reconciliation plan inside one transaction."""
    applied: list[str] = []
    for item in _system_account_reconciliation_plan(conn):
        code = item["code"]
        spec = SYSTEM_ACCOUNT_SPECS[code]
        if item["action"] == "create":
            conn.execute(
                text(
                    "INSERT INTO accounts "
                    "(code, name, type, parent_id, is_gst, active, description) "
                    "VALUES (:code, :name, :type, NULL, :is_gst, 1, NULL)"
                ),
                {
                    "code": spec.code,
                    "name": spec.default_name,
                    "type": spec.type.value,
                    "is_gst": spec.is_gst,
                },
            )
            applied.append(f"reconcile:system_account:{code}:created")
            continue

        row = item["row"]
        result = conn.execute(
            text(
                "UPDATE accounts SET type = :type, active = 1 "
                "WHERE id = :id AND code = :code"
            ),
            {"type": spec.type.value, "id": row["id"], "code": code},
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"System account {code} changed during reconciliation; "
                "transaction rolled back"
            )
        repairs: list[str] = []
        if item["wrong_type"]:
            repairs.append(f"type={spec.type.value}")
        if item["inactive"]:
            repairs.append("active")
        applied.append(
            f"reconcile:system_account:{code}:" + "+".join(repairs)
        )

    residual = _system_account_reconciliation_plan(conn)
    if residual:
        codes = ", ".join(item["code"] for item in residual)
        raise RuntimeError(
            f"System account reconciliation did not converge for: {codes}"
        )
    _assert_sqlite_integrity(conn, context="system account reconciliation")
    return applied


def reconcile_system_accounts(engine: Engine) -> list[str]:
    """Backup and atomically repair canonical account identity at startup.

    The initial read is intentionally non-mutating. Duplicate legacy codes or
    malformed schemas fail before a backup/transaction; all database errors
    propagate. A durable SQLite database is copied with the online-backup API
    before any required row is inserted or updated.
    """
    with engine.connect() as conn:
        if not _system_account_reconciliation_plan(conn):
            return []

    backup_path, _reasons = _create_destructive_migration_backup(
        engine, include_system_accounts=True
    )
    applied = ["backup:system_accounts"] if backup_path is not None else []
    with _migration_transaction(engine) as conn:
        applied.extend(_apply_system_account_reconciliation(conn))
    return applied


def _autoincrement_high_water(conn, table: str) -> int:
    """Return the highest live or historically allocated ID for ``table``."""
    max_id = conn.execute(
        text(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
    ).scalar_one()
    old_sequence = 0
    if _table_exists(conn, "sqlite_sequence"):
        old_sequence = (
            conn.execute(
                text("SELECT seq FROM sqlite_sequence WHERE name = :table"),
                {"table": table},
            ).scalar_one_or_none()
            or 0
        )
    return max(int(max_id), int(old_sequence))


def _restore_autoincrement_high_water(
    conn,
    *,
    table: str,
    helper_table: str,
    high_water_id: int,
) -> None:
    """Keep deleted legacy IDs from being reused after a table rebuild."""
    conn.execute(
        text(
            "DELETE FROM sqlite_sequence "
            "WHERE name IN (:table, :helper_table)"
        ),
        {"table": table, "helper_table": helper_table},
    )
    conn.execute(
        text("INSERT INTO sqlite_sequence(name, seq) VALUES (:table, :seq)"),
        {"table": table, "seq": high_water_id},
    )


def _indexes_referencing_column(conn, table: str, column: str) -> list[str]:
    """Names of indexes on `table` that reference `column`.

    SQLite's ALTER TABLE ... DROP COLUMN fails if any index still references
    the column being dropped (e.g. the auto-created ix_bank_accounts_kind on
    an indexed column). Caller drops these first. Uses PRAGMA index_info so
    it matches the actual indexed columns, not a substring of the DDL.
    """
    rows = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name = :t AND name NOT LIKE 'sqlite_%'"
        ),
        {"t": table},
    ).fetchall()
    matching: list[str] = []
    for (index_name,) in rows:
        cols = conn.execute(
            text(f'PRAGMA index_info("{index_name}")')
        ).fetchall()
        if any(c[2] == column for c in cols):
            matching.append(index_name)
    return matching


def _rebuild_bank_transactions(conn) -> None:
    """Rebuild bank_transactions table to apply CHECK constraints + FK ondelete.

    SQLite can't ALTER CHECK / FK rules in place. The supported pattern is:
      1. CREATE TABLE new_X with the desired schema
      2. INSERT new_X SELECT * FROM X
      3. DROP TABLE X
      4. ALTER TABLE new_X RENAME TO X
      5. recreate indexes

    foreign_keys must be OFF during this dance so dropping the old table
    doesn't cascade-trash referencing rows.

    The DDL below must mirror the LIVE model (models.company.BankTransaction)
    — a stale snapshot here silently drops newer columns and their data.
    `sync_missing_columns` runs first in `init_company_db`. The direct legacy
    runner also accepts databases from before the unapplied-cash columns; their
    unambiguous historical values (NULL account and zero amount) are supplied
    by the INSERT…SELECT below.
    """
    high_water_id = _autoincrement_high_water(conn, "bank_transactions")
    old_columns = _existing_columns(conn, "bank_transactions")
    unapplied_account_source = (
        "unapplied_account_id" if "unapplied_account_id" in old_columns else "NULL"
    )
    unapplied_amount_source = (
        "unapplied_amount" if "unapplied_amount" in old_columns else "0"
    )

    # Snapshot original index DDL so we can recreate post-rename.
    index_rows = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='bank_transactions' "
            "AND sql IS NOT NULL"
        )
    ).fetchall()
    index_sqls = [r[0] for r in index_rows]

    # Toggle FK off, rebuild, re-enable.
    conn.execute(text("PRAGMA foreign_keys = OFF"))
    if conn.execute(text("PRAGMA foreign_keys")).scalar_one() != 0:
        raise RuntimeError(
            "bank_transactions rebuild requires foreign_keys=OFF before its transaction"
        )

    savepoint = "p1_bank_transactions_rebuild"
    conn.exec_driver_sql(f"SAVEPOINT {savepoint}")
    try:
        # A failed older attempt can leave the fixed-name helper behind.  Drop
        # it inside the savepoint so both a clean retry and rollback are safe.
        conn.execute(text("DROP TABLE IF EXISTS new_bank_transactions"))
        conn.execute(
            text(
                """
                CREATE TABLE new_bank_transactions (
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
                    unapplied_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
                    unapplied_amount NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    dedup_key VARCHAR(64),
                    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                    CONSTRAINT ck_bank_txn_amount_positive CHECK (amount > 0),
                    CONSTRAINT ck_bank_txn_gst_nonneg CHECK (gst_amount >= 0),
                    CONSTRAINT ck_bank_txn_gst_within CHECK (gst_amount <= amount),
                    CONSTRAINT ck_bank_txn_unapplied_within CHECK (
                        unapplied_amount >= 0 AND unapplied_amount <= amount
                    ),
                    CONSTRAINT ck_bank_txn_unapplied_account CHECK (
                        (unapplied_amount = 0 AND unapplied_account_id IS NULL) OR
                        (unapplied_amount > 0 AND unapplied_account_id IS NOT NULL)
                    )
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                INSERT INTO new_bank_transactions (
                    id, bank_account_id, direction, amount, occurred_at, memo,
                    counter_party_name, account_id, gst_amount, tax_code,
                    unapplied_account_id, unapplied_amount, dedup_key, created_at
                )
                SELECT
                    id, bank_account_id, direction, amount, occurred_at, memo,
                    counter_party_name, account_id, gst_amount, tax_code,
                    {unapplied_account_source}, {unapplied_amount_source},
                    dedup_key, created_at
                FROM bank_transactions
                """
            )
        )
        conn.execute(text("DROP TABLE bank_transactions"))
        conn.execute(text("ALTER TABLE new_bank_transactions RENAME TO bank_transactions"))
        _restore_autoincrement_high_water(
            conn,
            table="bank_transactions",
            helper_table="new_bank_transactions",
            high_water_id=high_water_id,
        )
        for sql in index_sqls:
            # Skip indexes on columns the rebuild dropped (e.g. the auto index
            # on the removed linked_trust_entry_id) — recreating them would
            # fail with "no such column". Step 4 recreates the real ones.
            if "linked_trust_entry_id" in sql:
                continue
            conn.execute(text(sql))
        _assert_sqlite_integrity(conn, context="bank_transactions rebuild")
    except Exception:
        conn.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
    finally:
        conn.execute(text("PRAGMA foreign_keys = ON"))


def _rebuild_outgoing_documents(conn) -> None:
    """Rebuild outgoing_documents to the lean Receipt-only schema.

    The Receipt-only rewrite removed the SA/PR/Invoice/Partner columns
    (REMOVED_OUTDOC_COLUMNS). Several can't go via ALTER TABLE ... DROP COLUMN:
    they are self-FKs (service_agreement_id / voided_by_sa_id / related_doc_id →
    outgoing_documents.id) or an FK to the already-dropped `partners` table, and
    SQLite's DROP COLUMN integrity check rejects those. The supported pattern is
    the same new-table → copy → drop → rename dance used for bank_transactions.

    Only receipts survive (WHERE doc_type='receipt'); legacy SA/PR/Invoice rows
    and partner rows (partner_ref_id NOT NULL) are purged. All such data is
    test-only, per the removal decision.

    The DDL below must mirror the LIVE model (models.outgoing.OutgoingDocument).
    """
    old_cols = _existing_columns(conn, "outgoing_documents")

    high_water_id = _autoincrement_high_water(conn, "outgoing_documents")

    index_rows = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='outgoing_documents' "
            "AND sql IS NOT NULL"
        )
    ).fetchall()
    index_sqls = [r[0] for r in index_rows]

    # Partner rows are purged too when the column is still present.
    partner_clause = (
        " AND partner_ref_id IS NULL" if "partner_ref_id" in old_cols else ""
    )

    conn.execute(text("PRAGMA foreign_keys = OFF"))
    if conn.execute(text("PRAGMA foreign_keys")).scalar_one() != 0:
        # Continuing with FK enforcement enabled would make DROP TABLE cascade
        # away even the retained receipt lines.  Stop before any destructive DDL.
        raise RuntimeError(
            "outgoing_documents rebuild requires foreign_keys=OFF before its transaction"
        )

    savepoint = "p0_outgoing_documents_rebuild"
    conn.exec_driver_sql(f"SAVEPOINT {savepoint}")
    try:
        # A pre-fix crash could leave this fixed-name helper behind.  Always
        # remove it before CREATE so the next startup can recover.
        conn.execute(text("DROP TABLE IF EXISTS new_outgoing_documents"))
        conn.execute(
            text(
                """
                CREATE TABLE new_outgoing_documents (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    doc_type VARCHAR(20) NOT NULL,
                    doc_number VARCHAR(40) NOT NULL,
                    issue_date DATE NOT NULL,
                    customer_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                    client_ref_id INTEGER REFERENCES clients(id) ON DELETE RESTRICT,
                    customer_name VARCHAR(200) NOT NULL,
                    customer_address VARCHAR(500),
                    customer_abn VARCHAR(20),
                    customer_email VARCHAR(200),
                    customer_phone VARCHAR(50),
                    currency VARCHAR(3) NOT NULL DEFAULT 'AUD',
                    subtotal NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    gst_amount NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    total NUMERIC(16, 2) NOT NULL DEFAULT 0,
                    status VARCHAR(20) NOT NULL DEFAULT 'draft',
                    paid_date DATE,
                    payment_method VARCHAR(100),
                    notes VARCHAR(1000),
                    pdf_rel_path VARCHAR(500),
                    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                    updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                    CONSTRAINT uq_outdoc_type_number UNIQUE (doc_type, doc_number)
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO new_outgoing_documents ("
                "id, doc_type, doc_number, issue_date, customer_id, client_ref_id, "
                "customer_name, customer_address, customer_abn, customer_email, "
                "customer_phone, currency, subtotal, gst_amount, total, status, "
                "paid_date, payment_method, notes, pdf_rel_path, created_at, updated_at"
                ") SELECT "
                "id, doc_type, doc_number, issue_date, customer_id, client_ref_id, "
                "customer_name, customer_address, customer_abn, customer_email, "
                "customer_phone, currency, subtotal, gst_amount, total, status, "
                "paid_date, payment_method, notes, pdf_rel_path, created_at, updated_at "
                "FROM outgoing_documents "
                f"WHERE doc_type = 'receipt'{partner_clause}"
            )
        )

        # FK enforcement is deliberately disabled for the parent-table dance,
        # so ON DELETE CASCADE cannot protect us here.  Remove every child whose
        # parent was not copied (and any pre-existing orphan) before IDs can ever
        # be reused by a future receipt.
        if _table_exists(conn, "outgoing_document_lines"):
            line_cols = _existing_columns(conn, "outgoing_document_lines")
            if "document_id" in line_cols:
                conn.execute(
                    text(
                        "DELETE FROM outgoing_document_lines AS line "
                        "WHERE NOT EXISTS ("
                        "SELECT 1 FROM new_outgoing_documents AS doc "
                        "WHERE doc.id = line.document_id"
                        ")"
                    )
                )
        conn.execute(text("DROP TABLE outgoing_documents"))
        conn.execute(
            text("ALTER TABLE new_outgoing_documents RENAME TO outgoing_documents")
        )

        # CREATE/INSERT seeds AUTOINCREMENT from retained rows only.  Restore the
        # old high-water mark (including a sqlite_sequence value above MAX(id))
        # so a purged legacy ID can never be assigned to a new receipt.
        _restore_autoincrement_high_water(
            conn,
            table="outgoing_documents",
            helper_table="new_outgoing_documents",
            high_water_id=high_water_id,
        )
        for sql in index_sqls:
            # Skip indexes on columns the rebuild dropped (service_agreement_id,
            # voided_by_sa_id, partner_ref_id, staff_member_id, …); recreating
            # them would fail with "no such column".
            if any(col in sql for col in REMOVED_OUTDOC_COLUMNS):
                continue
            conn.execute(text(sql))
        _assert_sqlite_integrity(conn, context="outgoing_documents rebuild")
    except Exception:
        conn.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
        raise
    else:
        conn.exec_driver_sql(f"RELEASE SAVEPOINT {savepoint}")
    finally:
        conn.execute(text("PRAGMA foreign_keys = ON"))


def _ensure_bank_txn_dedup_unique_index(conn) -> None:
    """Partial UNIQUE index for statement-import deduplication."""
    _repair_named_index(
        conn,
        table="bank_transactions",
        signature=_BANK_DEDUP_INDEX,
    )


def _ensure_invoice_source_unique_index(conn) -> None:
    """Partial UNIQUE index that catches re-imports of the same source row.

    Same Excel row / same PDF file should produce the same source_ref
    when re-uploaded; this index makes the second import fail at the
    DB level instead of silently creating a duplicate Invoice.

    Scoped by `source` so identical source_ref strings across different
    import paths don't collide (e.g. Excel "row 1" doesn't conflict
    with PDF "row 1" — they're in different sources).
    """
    [signature] = MIGRATION_INDEX_SIGNATURES["invoices"]
    _repair_named_index(
        conn,
        table="invoices",
        signature=signature,
    )


def _ensure_journal_source_unique_indexes(conn) -> None:
    """Unique source provenance for generated journal entries."""
    for signature in MIGRATION_INDEX_SIGNATURES["journal_entries"]:
        _repair_named_index(
            conn,
            table="journal_entries",
            signature=signature,
        )


def _payment_reconciliation_counts(conn) -> dict[str, int]:
    """Pending fail-safe repairs for pre-allocation-ledger payment metadata."""

    required = {
        "invoices",
        "journal_entries",
        "bank_transactions",
        "accounts",
        "invoice_payment_allocations",
        "payment_reconciliation_events",
    }
    if any(not _table_exists(conn, table) for table in required):
        return {}

    invalid_allocation_txns = int(
        conn.execute(
            text(
                "SELECT COUNT(DISTINCT a.bank_transaction_id) "
                "FROM invoice_payment_allocations AS a "
                "JOIN invoices AS i ON i.id = a.invoice_id "
                "LEFT JOIN journal_entries AS j ON j.source_id = i.id AND "
                "((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
                " (i.direction = 'AP' AND j.source_type = 'invoice_ap')) "
                "WHERE j.id IS NULL AND NOT EXISTS (SELECT 1 "
                "FROM allocation_history_events e WHERE e.bank_transaction_id=a.bank_transaction_id "
                "AND e.operation='legacy_history_unknown')"
            )
        ).scalar_one()
    )
    unallocated_controls = int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM bank_transactions AS t "
                "JOIN accounts AS a ON a.id = t.account_id "
                "WHERE ((a.code = '1100' AND t.direction = 'in') OR "
                "       (a.code = '2000' AND t.direction = 'out')) "
                "AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
                "                WHERE p.bank_transaction_id = t.id) "
                "AND NOT EXISTS (SELECT 1 FROM allocation_history_events e "
                "                WHERE e.bank_transaction_id=t.id "
                "                  AND e.operation='legacy_history_unknown')"
            )
        ).scalar_one()
    )
    stale_invoices = int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM invoices AS i WHERE "
                "((COALESCE(i.paid_amount, 0) > 0 OR i.status IN ('partial', 'paid')) "
                " AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
                "                 WHERE p.invoice_id = i.id)) "
                "OR (i.status NOT IN ('draft', 'void') AND NOT EXISTS ("
                "    SELECT 1 FROM journal_entries AS j WHERE j.source_id = i.id "
                "    AND ((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
                "         (i.direction = 'AP' AND j.source_type = 'invoice_ap')))) "
                "AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations p "
                "JOIN allocation_history_events e ON e.bank_transaction_id=p.bank_transaction_id "
                "WHERE p.invoice_id=i.id AND e.operation='legacy_history_unknown')"
            )
        ).scalar_one()
    )
    counts = {
        "invalid_allocation_txns": invalid_allocation_txns,
        "unallocated_controls": unallocated_controls,
        "stale_invoices": stale_invoices,
    }
    return {name: count for name, count in counts.items() if count}


def _record_payment_reconciliation_event(
    conn,
    *,
    event_key: str,
    event_type: str,
    invoice_id: int | None = None,
    bank_transaction_id: int | None = None,
    details: dict,
) -> None:
    conn.execute(
        text(
            "INSERT OR IGNORE INTO payment_reconciliation_events "
            "(event_key, event_type, invoice_id, bank_transaction_id, details_json) "
            "VALUES (:event_key, :event_type, :invoice_id, :bank_transaction_id, "
            ":details_json)"
        ),
        {
            "event_key": event_key,
            "event_type": event_type,
            "invoice_id": invoice_id,
            "bank_transaction_id": bank_transaction_id,
            "details_json": json.dumps(
                details, ensure_ascii=False, sort_keys=True, default=str
            ),
        },
    )


def _reconcile_legacy_payment_metadata(conn) -> dict[str, int]:
    """Preserve evidence, then return unverifiable legacy state to workflow."""

    counts = _payment_reconciliation_counts(conn)
    if not counts:
        return {}

    invalid_txn_rows = conn.execute(
        text(
            "SELECT DISTINCT t.id, t.account_id, t.gst_amount, t.tax_code "
            "FROM bank_transactions AS t "
            "JOIN invoice_payment_allocations AS a ON a.bank_transaction_id = t.id "
            "JOIN invoices AS i ON i.id = a.invoice_id "
            "LEFT JOIN journal_entries AS j ON j.source_id = i.id AND "
            "((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
            " (i.direction = 'AP' AND j.source_type = 'invoice_ap')) "
            "WHERE j.id IS NULL AND NOT EXISTS (SELECT 1 "
            "FROM allocation_history_events e WHERE e.bank_transaction_id=t.id "
            "AND e.operation='legacy_history_unknown')"
        )
    ).mappings().all()
    for row in invalid_txn_rows:
        allocation_rows = conn.execute(
            text(
                "SELECT invoice_id, amount, gst_amount "
                "FROM invoice_payment_allocations WHERE bank_transaction_id = :id "
                "ORDER BY id"
            ),
            {"id": row["id"]},
        ).mappings().all()
        _record_payment_reconciliation_event(
            conn,
            event_key=f"legacy-invalid-allocation-txn-{row['id']}",
            event_type="invalid_allocation_without_invoice_journal",
            bank_transaction_id=row["id"],
            details={
                "account_id": row["account_id"],
                "gst_amount": row["gst_amount"],
                "tax_code": row["tax_code"],
                "allocations": [dict(item) for item in allocation_rows],
            },
        )
        conn.execute(
            text(
                "DELETE FROM invoice_payment_allocations "
                "WHERE bank_transaction_id = :id"
            ),
            {"id": row["id"]},
        )
        conn.execute(
            text(
                "UPDATE bank_transactions SET account_id = NULL, gst_amount = 0, "
                "tax_code = 'none', unapplied_account_id = NULL, "
                "unapplied_amount = 0 WHERE id = :id"
            ),
            {"id": row["id"]},
        )

    control_rows = conn.execute(
        text(
            "SELECT t.id, t.account_id, t.gst_amount, t.tax_code, a.code "
            "FROM bank_transactions AS t JOIN accounts AS a ON a.id = t.account_id "
            "WHERE ((a.code = '1100' AND t.direction = 'in') OR "
            "       (a.code = '2000' AND t.direction = 'out')) "
            "AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
                "                WHERE p.bank_transaction_id = t.id) "
                "AND NOT EXISTS (SELECT 1 FROM allocation_history_events e "
                "                WHERE e.bank_transaction_id=t.id "
                "                  AND e.operation='legacy_history_unknown')"
        )
    ).mappings().all()
    for row in control_rows:
        _record_payment_reconciliation_event(
            conn,
            event_key=f"legacy-unallocated-control-txn-{row['id']}",
            event_type="unallocated_legacy_control_transaction",
            bank_transaction_id=row["id"],
            details=dict(row),
        )
        conn.execute(
            text(
                "UPDATE bank_transactions SET account_id = NULL, gst_amount = 0, "
                "tax_code = 'none', unapplied_account_id = NULL, "
                "unapplied_amount = 0 WHERE id = :id"
            ),
            {"id": row["id"]},
        )

    invoice_rows = conn.execute(
        text(
            "SELECT i.id, i.status, i.paid_amount, i.paid_date, i.authorised_at, "
            "EXISTS(SELECT 1 FROM journal_entries AS j WHERE j.source_id = i.id "
            " AND ((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
            "      (i.direction = 'AP' AND j.source_type = 'invoice_ap'))) AS has_journal "
            "FROM invoices AS i WHERE "
            "((COALESCE(i.paid_amount, 0) > 0 OR i.status IN ('partial', 'paid')) "
            " AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations AS p "
            "                 WHERE p.invoice_id = i.id)) "
            "OR (i.status NOT IN ('draft', 'void') AND NOT EXISTS ("
            "    SELECT 1 FROM journal_entries AS j WHERE j.source_id = i.id "
            "    AND ((i.direction = 'AR' AND j.source_type = 'invoice_ar') OR "
            "         (i.direction = 'AP' AND j.source_type = 'invoice_ap')))) "
            "AND NOT EXISTS (SELECT 1 FROM invoice_payment_allocations p "
            "JOIN allocation_history_events e ON e.bank_transaction_id=p.bank_transaction_id "
            "WHERE p.invoice_id=i.id AND e.operation='legacy_history_unknown')"
        )
    ).mappings().all()
    for row in invoice_rows:
        _record_payment_reconciliation_event(
            conn,
            event_key=f"legacy-stale-invoice-{row['id']}",
            event_type="unverifiable_legacy_invoice_payment_state",
            invoice_id=row["id"],
            details=dict(row),
        )
        target_status = "authorised" if row["has_journal"] else "draft"
        conn.execute(
            text(
                "UPDATE invoices SET status = :status, paid_amount = 0, "
                "paid_date = NULL, authorised_at = CASE WHEN :has_journal = 1 "
                "THEN authorised_at ELSE NULL END WHERE id = :id"
            ),
            {
                "id": row["id"],
                "status": target_status,
                "has_journal": int(bool(row["has_journal"])),
            },
        )
    return {
        "invalid_allocation_txns": len(invalid_txn_rows),
        "unallocated_controls": len(control_rows),
        "stale_invoices": len(invoice_rows),
    }


def _backfill_invoice_line_tax_codes(conn) -> int:
    if not _table_exists(conn, "invoice_lines"):
        return 0
    columns = _existing_columns(conn, "invoice_lines")
    if "tax_code" not in columns:
        return 0
    result = conn.execute(
        text(
            "UPDATE invoice_lines SET tax_code = CASE "
            "WHEN COALESCE(line_gst, 0) > 0 THEN 'standard' ELSE 'gst_free' END "
            "WHERE tax_code IS NULL OR tax_code = '' OR "
            "(tax_code = 'gst_free' AND COALESCE(line_gst, 0) > 0)"
        )
    )
    return int(result.rowcount or 0)


def _backfill_journal_source_type(conn) -> int:
    """Existing journal rows predate provenance and are manual entries."""
    if not _table_exists(conn, "journal_entries"):
        return 0
    if "source_type" not in _existing_columns(conn, "journal_entries"):
        return 0
    result = conn.execute(
        text(
            "UPDATE journal_entries "
            "SET source_type = 'manual' "
            "WHERE source_type IS NULL"
        )
    )
    return result.rowcount or 0


def _backfill_invoice_authorised_at(conn) -> int:
    """Historical unpaid/partial/paid invoices were already operator-approved."""
    if not _table_exists(conn, "invoices"):
        return 0
    cols = _existing_columns(conn, "invoices")
    if "authorised_at" not in cols or "status" not in cols:
        return 0
    fallback = "created_at" if "created_at" in cols else "issue_date"
    result = conn.execute(
        text(
            f"UPDATE invoices "
            f"SET authorised_at = COALESCE(authorised_at, {fallback}, issue_date) "
            f"WHERE authorised_at IS NULL "
            f"AND status IN ('unpaid', 'partial', 'paid', 'authorised')"
        )
    )
    return result.rowcount or 0


def _backfill_invoice_contact_snapshots(conn) -> int:
    if not _table_exists(conn, "invoices") or not _table_exists(conn, "contacts"):
        return 0
    invoice_columns = _existing_columns(conn, "invoices")
    snapshot_columns = {
        "contact_name_snapshot",
        "contact_abn_snapshot",
        "contact_address_snapshot",
        "contact_email_snapshot",
        "contact_phone_snapshot",
    }
    if not {"id", "contact_id", *snapshot_columns}.issubset(invoice_columns):
        return 0
    contact_columns = {"id", "name", "abn", "address", "email", "phone"}
    if not contact_columns.issubset(_existing_columns(conn, "contacts")):
        return 0

    missing_contacts = conn.execute(
        text(
            "SELECT i.id FROM invoices AS i "
            "LEFT JOIN contacts AS c ON c.id = i.contact_id "
            "WHERE i.contact_name_snapshot IS NULL AND c.id IS NULL "
            "ORDER BY i.id"
        )
    ).scalars().all()
    if missing_contacts:
        raise DataRecoveryRequiredError(
            "Cannot backfill invoice contact snapshots: invoice(s) "
            f"{missing_contacts!r} reference a missing Contact. Restore the Contact "
            "or resolve the invoice through an operator-reviewed recovery workflow."
        )

    result = conn.execute(
        text(
            "UPDATE invoices SET "
            "contact_name_snapshot = (SELECT c.name FROM contacts AS c "
            "WHERE c.id = invoices.contact_id), "
            "contact_abn_snapshot = (SELECT c.abn FROM contacts AS c "
            "WHERE c.id = invoices.contact_id), "
            "contact_address_snapshot = (SELECT c.address FROM contacts AS c "
            "WHERE c.id = invoices.contact_id), "
            "contact_email_snapshot = (SELECT c.email FROM contacts AS c "
            "WHERE c.id = invoices.contact_id), "
            "contact_phone_snapshot = (SELECT c.phone FROM contacts AS c "
            "WHERE c.id = invoices.contact_id) "
            "WHERE contact_name_snapshot IS NULL"
        )
    )
    return int(result.rowcount or 0)


def _backfill_doc_number_version_suffix(conn) -> int:
    """Rename `XX-YYYY-NNNN` documents to `XX-YYYY-NNNN-1`.

    The numbering scheme now stamps a version suffix on every doc; the
    original issue is `-1` and each void+reissue bumps the suffix.
    Existing data pre-dates that — bring it up to date in one shot.

    Returns the number of rows renamed. Idempotent: a second run finds
    nothing matching the regex (every doc already has a `-N` suffix).
    """
    if not _table_exists(conn, "outgoing_documents"):
        return 0

    # SQLite's REGEXP is optional; use LIKE + a SQL-side check that the
    # number does NOT already contain a fourth `-` segment.
    # Pattern of unversioned numbers: <prefix>-<year>-<serial> where there
    # are exactly two `-`. We detect by counting `-` in doc_number.
    rows = conn.execute(
        text(
            "SELECT id, doc_number FROM outgoing_documents "
            "WHERE doc_number IS NOT NULL"
        )
    ).fetchall()
    renamed = 0
    for row in rows:
        doc_id, number = row[0], row[1]
        if not isinstance(number, str):
            continue
        # New shape: at least three '-' (PREFIX-YYYY-NNNN-V).
        # Old shape: exactly two '-' (PREFIX-YYYY-NNNN).
        if number.count("-") != 2:
            continue
        new_number = f"{number}-1"
        # Guard against a hypothetical collision with a row that's already -1.
        existing = conn.execute(
            text("SELECT 1 FROM outgoing_documents WHERE doc_number = :n"),
            {"n": new_number},
        ).fetchone()
        if existing is not None:
            # Collision is theoretically impossible given the old scheme
            # never wrote `-1`, but if it ever happens, skip rather than crash.
            continue
        conn.execute(
            text("UPDATE outgoing_documents SET doc_number = :new WHERE id = :id"),
            {"new": new_number, "id": doc_id},
        )
        renamed += 1
    return renamed


def run_company_migrations(
    engine: Engine, *, enforce_schema_gate: bool = False
) -> list[str]:
    """Apply pending schema migrations on a per-company DB.

    Returns the list of step names that ran this call. Idempotent.
    """
    _install_allocation_baseline_hash(engine)
    applied: list[str] = []
    backup_path, backup_reasons = _create_destructive_migration_backup(
        engine,
        include_unrepaired_schema=enforce_schema_gate,
    )
    if backup_path is not None:
        applied.extend(f"backup:{reason}" for reason in backup_reasons)
    with engine.connect() as check_conn:
        rebuild_bank_accounts = _bank_accounts_need_rebuild(check_conn)
        rebuild_bank_transactions = _bank_needs_rebuild(check_conn)
    with _migration_transaction(
        engine,
        foreign_keys_off=(rebuild_bank_accounts or rebuild_bank_transactions),
    ) as conn:
        if not _table_exists(conn, "bank_transactions"):
            dropped_batch_triggers = _drop_allocation_batch_bank_transaction_triggers(conn)
            if dropped_batch_triggers:
                applied.append("drop_triggers:allocation_batches_missing_bank_table")

        # 1. Table drops (for tables removed from the schema). FKs are
        # toggled off so a referenced table can be dropped even when some
        # *other* table still has the dangling FK column (cleaned up in
        # step 2). All data has already been wiped for these legacy tables.
        if not rebuild_bank_accounts:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
        try:
            for table, indexes in COMPANY_DB_TABLE_DROPS:
                existed = _table_exists(conn, table)
                dropped_indexes = False
                for index in indexes:
                    if _index_exists(conn, index):
                        conn.execute(text(f'DROP INDEX IF EXISTS "{index}"'))
                        dropped_indexes = True
                if existed:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
                    applied.append(f"drop_table:{table}")
                elif dropped_indexes:
                    applied.append(f"drop_indexes:{table}")
        finally:
            if not rebuild_bank_accounts:
                conn.execute(text("PRAGMA foreign_keys = ON"))

        # 2b. Column drops (for columns removed from the schema).
        # (Additive column changes are handled by schema_sync.sync_missing_columns.)
        #
        # FK pragma off: the column being dropped may itself be a FK
        # source whose target table was dropped in step 1 (e.g.
        # bank_transactions.linked_trust_entry_id → the dropped
        # trust_ledger_entries table). SQLite's ALTER TABLE … DROP COLUMN
        # runs an integrity check that rejects "FK references missing
        # table" otherwise.
        if not rebuild_bank_accounts:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
        try:
            for table, column in COMPANY_DB_COLUMN_DROPS:
                if not _table_exists(conn, table):
                    continue
                if column not in _existing_columns(conn, table):
                    continue
                # SQLite's DROP COLUMN runs an integrity check that fails if any
                # index still references the column (e.g. ix_bank_accounts_kind
                # on the indexed `kind` column). Drop those indexes first.
                for index_name in _indexes_referencing_column(conn, table, column):
                    conn.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
                    applied.append(f"drop_index:{index_name}")
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN "{column}"'))
                applied.append(f"drop_column:{table}.{column}")
        finally:
            if not rebuild_bank_accounts:
                conn.execute(text("PRAGMA foreign_keys = ON"))

        # 3. Table rebuilds for constraint/FK changes.
        #
        # bank_transactions is rebuilt to (a) apply the full model constraint
        # signature and (b) drop the legacy linked_trust_entry_id column. The
        # column can't go via plain DROP COLUMN: its FK targets the
        # already-dropped trust_ledger_entries table, which SQLite's DROP COLUMN
        # integrity check rejects. The rebuild (new table → copy → drop → rename)
        # sidesteps that. Full signature comparison also catches partially
        # applied legacy rebuilds, including missing GST checks or FKs.
        if rebuild_bank_accounts:
            _rebuild_bank_accounts(conn)
            applied.append("rebuild:bank_accounts")

        for table in TABLE_REBUILDS:
            if not _table_exists(conn, table):
                continue
            if table != "bank_transactions" or not _bank_needs_rebuild(conn):
                continue
            violations = _bank_rebuild_data_violations(conn)
            if violations:
                raise DataRecoveryRequiredError(
                    "Cannot safely rebuild bank_transactions: "
                    + "; ".join(violations)
                    + ". A pre-repair backup was preserved; correct the data "
                    "through an operator-reviewed recovery workflow."
                )
            dropped_batch_triggers = _drop_allocation_batch_bank_transaction_triggers(conn)
            if dropped_batch_triggers:
                applied.append("drop_triggers:allocation_batches_bank_transaction")
            for trigger in (
                "trg_allocation_history_events_domain",
                "trg_allocation_history_baseline_finalizations_domain",
                "trg_bank_transactions_allocation_history_no_delete",
                "trg_bank_transactions_allocation_history_identity",
                "trg_bank_transactions_allocation_history_no_replace",
            ):
                conn.execute(text(f'DROP TRIGGER IF EXISTS "{trigger}"'))
            _rebuild_bank_transactions(conn)
            applied.append(f"rebuild:{table}")
            if dropped_batch_triggers and _ensure_allocation_batch_guards(conn):
                applied.append("guards:allocation_batches_append_only")

        # 3b. outgoing_documents rebuild — the SA/PR/Invoice/Partner columns were
        # removed when the document model was reduced to Receipt-only. Several are
        # self-FKs or an FK to the dropped `partners` table, so a plain DROP COLUMN
        # can't remove them. Rebuild to the lean schema keeping only receipts.
        if _table_exists(conn, "outgoing_documents"):
            if REMOVED_OUTDOC_COLUMNS & _existing_columns(conn, "outgoing_documents"):
                _rebuild_outgoing_documents(conn)
                applied.append("rebuild:outgoing_documents")

        if _ensure_allocation_history_schema(conn):
            applied.append("schema:allocation_history")
        if _ensure_allocation_batch_schema(conn):
            applied.append("schema:allocation_batches")

        # 4. Repair every missing/wrong model index plus the migration-owned
        # partial unique indexes by full signature, never by name alone.
        for table, signature in _pending_index_repairs(conn):
            if _repair_named_index(conn, table=table, signature=signature):
                applied.append(f"index:{signature.name}")
        for _table, index_name in _pending_unexpected_nonunique_indexes(conn):
            conn.execute(text(f'DROP INDEX "{index_name}"'))
            applied.append(f"drop_index:{index_name}")

        if _ensure_gst_adjustment_append_only_guards(conn):
            applied.append("guards:gst_adjustment_append_only")
        if _ensure_allocation_history_guards(conn):
            applied.append("guards:allocation_history_append_only")
        if _ensure_allocation_batch_guards(conn):
            applied.append("guards:allocation_batches_append_only")

        # Do not mutate financial rows while any non-targeted table constraint
        # remains absent or counterfeit. Populated drifted tables received an
        # online backup before this transaction began.
        if enforce_schema_gate:
            _require_no_unrepaired_table_signature_drift(conn)

        # 5. Data backfills run only after the structural enforcement gate.
        payment_repairs = _reconcile_legacy_payment_metadata(conn)
        for repair, count in payment_repairs.items():
            if count:
                applied.append(f"reconcile:payments:{repair}:{count}")
        invoice_tax_rows = _backfill_invoice_line_tax_codes(conn)
        if invoice_tax_rows:
            applied.append(f"backfill:invoice_line_tax_code:{invoice_tax_rows}")
        journal_rows = _backfill_journal_source_type(conn)
        if journal_rows:
            applied.append(f"backfill:journal_source_type:{journal_rows}")
        invoice_rows = _backfill_invoice_authorised_at(conn)
        if invoice_rows:
            applied.append(f"backfill:invoice_authorised_at:{invoice_rows}")
        invoice_contact_rows = _backfill_invoice_contact_snapshots(conn)
        if invoice_contact_rows:
            applied.append(
                f"backfill:invoice_contact_snapshot:{invoice_contact_rows}"
            )

        bank_mapping_rows = _backfill_bank_account_ledger_mapping(conn)
        if bank_mapping_rows:
            applied.append(f"backfill:bank_account_ledger_mapping:{bank_mapping_rows}")

        # 6. Data backfill: ensure every outgoing_documents row has a
        # version-suffixed doc_number (`XX-YYYY-NNNN-1` for the original).
        renamed = _backfill_doc_number_version_suffix(conn)
        if renamed:
            applied.append(f"backfill:doc_number_v1:{renamed}")

        # This is deliberately unconditional.  A no-op migration still must not
        # let a legacy FK violation or corrupt SQLite file enter the application.
        _assert_sqlite_integrity(conn, context="company migrations")

    return applied
