"""P0 schema-signature gate and safe index/bank remediation coverage."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import migrations
from app.db.base import CompanyBase
from app.db.errors import DataRecoveryRequiredError
from app.db.migrations import (
    allocation_baseline_integrity_hash,
    run_company_migrations,
)
from app.db.schema_sync import (
    MIGRATION_INDEX_SIGNATURES,
    SQLiteIndexSignature,
    detect_drift,
    require_clean_schema,
    sqlite_index_matches,
)
from app.models import company as _company_models  # noqa: F401
from app.models import outgoing as _outgoing_models  # noqa: F401
from app.models.company import gst_adjustment_money_to_cents


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


def test_allocation_batch_migration_leaves_schema_drift_clean(tmp_path):
    engine = _engine(tmp_path / "allocation-batch-drift.db")
    run_company_migrations(engine)

    report = detect_drift(engine, CompanyBase, "allocation-batch-schema")
    assert report.is_clean
    with engine.connect() as connection:
        batch_indexes = connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'uq_allocation_batch_%'"
        )).scalars().all()
        assert {
            "uq_allocation_batch_idempotency",
            "uq_allocation_batch_transaction_version",
            "uq_allocation_batch_line_order",
            "uq_allocation_batch_line_identity",
            "uq_allocation_batch_component_tax_code",
        } <= set(batch_indexes)
    engine.dispose()


def _valid_gst_event(**overrides):
    values = {
        "event_type": "agreement",
        "source_direction": "AR",
        "adjustment_direction": "increasing",
        "projection_box": "1A",
        "amount": "10.00",
        "gst_amount": "1.00",
        "tax_code": "standard",
        "policy_version": "F-02-v1",
        "reason": "Synthetic test event",
        "effective_date": "2026-10-01",
        "awareness_date": None,
        "agreement_date": None,
        "refund_repayment_date": None,
        "source_record_type": "credit_note",
        "source_record_id": 1,
        "lifecycle_operation_type": None,
        "lifecycle_operation_id": None,
        "adjustment_note_reference": None,
        "adjustment_note_held_date": None,
        "manual_review_status": "not_required",
        "tax_slice_count": 1,
        "reversal_of_event_id": None,
    }
    values.update(overrides)
    values["amount_cents"] = gst_adjustment_money_to_cents(values.pop("amount"))
    values["gst_amount_cents"] = gst_adjustment_money_to_cents(values.pop("gst_amount"))
    return values


_INSERT_GST_EVENT = text(
    "INSERT INTO gst_adjustment_events ("
    "event_type, source_direction, adjustment_direction, projection_box, "
    "amount_cents, gst_amount_cents, tax_code, policy_version, reason, effective_date, "
    "awareness_date, agreement_date, refund_repayment_date, source_record_type, "
    "source_record_id, lifecycle_operation_type, lifecycle_operation_id, "
    "adjustment_note_reference, adjustment_note_held_date, "
    "manual_review_status, tax_slice_count, reversal_of_event_id"
    ") VALUES ("
    ":event_type, :source_direction, :adjustment_direction, :projection_box, "
    ":amount_cents, :gst_amount_cents, :tax_code, :policy_version, :reason, :effective_date, "
    ":awareness_date, :agreement_date, :refund_repayment_date, :source_record_type, "
    ":source_record_id, :lifecycle_operation_type, :lifecycle_operation_id, "
    ":adjustment_note_reference, :adjustment_note_held_date, "
    ":manual_review_status, :tax_slice_count, :reversal_of_event_id) RETURNING id"
)


def _insert_gst_event(conn, **overrides):
    return conn.execute(_INSERT_GST_EVENT, _valid_gst_event(**overrides)).scalar_one()


def _orm_gst_event(**overrides):
    values = _valid_gst_event(**overrides)
    for field in (
        "effective_date",
        "awareness_date",
        "agreement_date",
        "refund_repayment_date",
        "adjustment_note_held_date",
    ):
        if values[field] is not None:
            values[field] = date.fromisoformat(values[field])
    return _company_models.GSTAdjustmentEvent(**values)


def _insert_gst_slice(conn, event_id, tax_code, amount, gst_amount):
    return conn.execute(
        text(
            "INSERT INTO gst_adjustment_tax_slices "
            "(event_id, tax_code, amount_cents, gst_amount_cents) "
            "VALUES (:event_id, :tax_code, :amount_cents, :gst_amount_cents)"
        ),
        {
            "event_id": event_id,
            "tax_code": tax_code,
            "amount_cents": gst_adjustment_money_to_cents(amount),
            "gst_amount_cents": gst_adjustment_money_to_cents(gst_amount),
        },
    )


def _finalize_gst_event(conn, event_id):
    return conn.execute(
        text(
            "INSERT INTO gst_adjustment_finalizations "
            "(event_id, tax_slice_count, amount_cents, gst_amount_cents) "
            "SELECT id, tax_slice_count, amount_cents, gst_amount_cents "
            "FROM gst_adjustment_events WHERE id=:event_id "
            "RETURNING event_id"
        ),
        {"event_id": event_id},
    ).scalar_one()


def _insert_journal_entry(
    conn,
    source_type,
    source_id,
    entry_date="2026-10-01",
    reverses_entry_id=None,
):
    return conn.execute(
        text(
            "INSERT INTO journal_entries "
            "(entry_date, memo, source_type, source_id, reverses_entry_id) "
            "VALUES (:entry_date, 'Synthetic credit-note journal', :source_type, "
            ":source_id, :reverses_entry_id) RETURNING id"
        ),
        {
            "entry_date": entry_date,
            "source_type": source_type,
            "source_id": source_id,
            "reverses_entry_id": reverses_entry_id,
        },
    ).scalar_one()


def _seed_ap_credit_note(conn, credit_note_id=42):
    conn.execute(
        text(
            "INSERT INTO contacts (id, kind, name, active, created_at) "
            "VALUES (1, 'supplier', 'Synthetic AP supplier', 1, CURRENT_TIMESTAMP)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO invoices ("
            "id, direction, contact_id, invoice_number, issue_date, currency, "
            "subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
            "source, created_at, updated_at"
            ") VALUES (1, 'AP', 1, 'INV-AP-SYNTHETIC', '2026-09-01', 'AUD', "
            "10, 1, 11, 1, 'draft', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO credit_notes ("
            "id, source_invoice_id, direction, contact_id, credit_note_number, "
            "issue_date, currency, subtotal, gst_amount, total, gst_inclusive, "
            "status, created_at, updated_at"
            ") VALUES (:id, 1, 'AP', 1, 'CN-AP-SYNTHETIC', '2026-09-02', 'AUD', "
            "10, 1, 11, 1, 'authorised', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"id": credit_note_id},
    )


def _insert_ap_original(
    conn,
    *,
    source_record_id=42,
    source_direction="AP",
    projection_box="1A",
    event_type="agreement",
    operation_type="credit_note_ap",
    journal_source_type="credit_note_ap",
    journal_source_id=None,
    effective_date="2026-10-01",
    journal_entry_date=None,
    amount="10.00",
    gst_amount="1.00",
    tax_code="standard",
    policy_version="F-02-v1",
    tax_slice_count=1,
    slices=None,
    finalize=True,
):
    journal_id = _insert_journal_entry(
        conn,
        journal_source_type,
        source_record_id if journal_source_id is None else journal_source_id,
        entry_date=effective_date if journal_entry_date is None else journal_entry_date,
    )
    event_id = _insert_gst_event(
        conn,
        event_type=event_type,
        source_direction=source_direction,
        projection_box=projection_box,
        adjustment_direction="decreasing",
        effective_date=effective_date,
        amount=amount,
        gst_amount=gst_amount,
        tax_code=tax_code,
        policy_version=policy_version,
        tax_slice_count=tax_slice_count,
        source_record_id=source_record_id,
        lifecycle_operation_type=operation_type,
        lifecycle_operation_id=journal_id if operation_type is not None else None,
    )
    for slice_code, slice_amount, slice_gst in (
        ((tax_code, amount, gst_amount),) if slices is None else slices
    ):
        _insert_gst_slice(conn, event_id, slice_code, slice_amount, slice_gst)
    if finalize:
        _finalize_gst_event(conn, event_id)
    return event_id, journal_id


def _insert_ap_void_reversal(
    conn,
    original_id,
    original_journal_id,
    *,
    source_direction="AP",
    projection_box="1A",
    adjustment_direction="increasing",
    event_type="reversal",
    operation_type="credit_note_void_ap",
    journal_source_type="credit_note_void_ap",
    journal_source_id=42,
    journal_reverses_entry_id=None,
    effective_date="2026-10-02",
    journal_date=None,
    amount="10.00",
    gst_amount="1.00",
    tax_code="standard",
    policy_version="F-02-v1",
    tax_slice_count=1,
    source_record_type="credit_note",
    source_record_id=42,
    finalize=True,
    slices=None,
):
    void_journal_id = _insert_journal_entry(
        conn,
        journal_source_type,
        journal_source_id,
        entry_date=effective_date if journal_date is None else journal_date,
        reverses_entry_id=(
            original_journal_id
            if journal_reverses_entry_id is None
            else journal_reverses_entry_id
        ),
    )
    event_id = _insert_gst_event(
        conn,
        event_type=event_type,
        source_direction=source_direction,
        adjustment_direction=adjustment_direction,
        projection_box=projection_box,
        amount=amount,
        gst_amount=gst_amount,
        tax_code=tax_code,
        policy_version=policy_version,
        tax_slice_count=tax_slice_count,
        effective_date=effective_date,
        source_record_type=source_record_type,
        source_record_id=source_record_id,
        lifecycle_operation_type=operation_type,
        lifecycle_operation_id=void_journal_id if operation_type is not None else None,
        reversal_of_event_id=original_id,
    )
    for slice_code, slice_amount, slice_gst in (
        ((tax_code, amount, gst_amount),) if slices is None else slices
    ):
        _insert_gst_slice(conn, event_id, slice_code, slice_amount, slice_gst)
    if finalize:
        _finalize_gst_event(conn, event_id)
    return event_id, void_journal_id


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


def _allocation_history_engine(tmp_path, name="allocation-history.db"):
    engine = _engine(tmp_path / name)
    run_company_migrations(engine)
    with engine.begin() as conn:
        _seed_ap_credit_note(conn)
        conn.execute(
            text(
                "INSERT INTO bank_accounts (id, name, opening_balance, is_active) "
                "VALUES (1, 'Synthetic allocation bank', 0, 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO bank_transactions "
                "(id, bank_account_id, direction, amount, occurred_at, gst_amount, "
                "tax_code, unapplied_amount) "
                "VALUES (1, 1, 'out', 20, '2026-10-01', 0, 'none', 0), "
                "(2, 1, 'out', 1, '2026-10-01', 0, 'none', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (1, 1, 1, 10, 1)"
            )
        )
    return engine


def _start_legacy_baseline(
    conn,
    *,
    component=True,
    tax_code="standard",
    allocation_gross=None,
    component_gross=10,
    component_gst=1,
):
    if allocation_gross is not None:
        conn.execute(
            text("UPDATE invoice_payment_allocations SET amount=:amount WHERE id=1"),
            {"amount": allocation_gross},
        )
    event_id = conn.execute(
        text(
            "INSERT INTO allocation_history_events "
            "(operation, bank_transaction_id) VALUES ('legacy_history_unknown', 1) "
            "RETURNING id"
        )
    ).scalar_one()
    component_ids = []
    if component:
        component_ids.append(
            conn.execute(
                text(
                    "INSERT INTO invoice_payment_tax_components "
                    "(allocation_id, tax_code, gross_amount, gst_amount) "
                    "VALUES (1, :tax_code, :gross, :gst) RETURNING id"
                ),
                {"tax_code": tax_code, "gross": component_gross, "gst": component_gst},
            ).scalar_one()
        )

    allocation = conn.execute(
        text(
            "SELECT a.id, a.bank_transaction_id, a.invoice_id, "
            "i.direction, CAST(a.amount AS TEXT), allocation_baseline_cents(a.amount), "
            "CAST(a.gst_amount AS TEXT), allocation_baseline_cents(a.gst_amount), "
            "CAST(a.created_at AS TEXT) "
            "FROM invoice_payment_allocations a JOIN invoices i ON i.id=a.invoice_id "
            "WHERE a.id=1"
        )
    ).one()
    source_kind = "invoice" if allocation.direction == "AR" else "bill"
    gross_cents = allocation[5]
    gst_cents = allocation[7]
    binding_id = conn.execute(
        text(
            "INSERT INTO allocation_history_baseline_bindings "
            "(baseline_event_id, bank_transaction_id, allocation_row_id, source_kind, "
            "invoice_or_bill_id, allocated_gross_cents, allocated_gross_source, "
            "allocated_gst_cents, allocated_gst_source, allocation_created_at) "
            "VALUES (:event_id, :bank_id, :allocation_id, :source_kind, :document_id, "
            ":gross_cents, :gross_source, :gst_cents, :gst_source, :created_at) "
            "RETURNING id"
        ),
        {
            "event_id": event_id,
            "bank_id": allocation.bank_transaction_id,
            "allocation_id": allocation.id,
            "source_kind": source_kind,
            "document_id": allocation.invoice_id,
            "gross_cents": gross_cents,
            "gross_source": allocation[4],
            "gst_cents": gst_cents,
            "gst_source": allocation[6],
            "created_at": allocation[8],
        },
    ).scalar_one()
    for component_id in component_ids:
        component = conn.execute(
            text(
                "SELECT id, allocation_id, tax_code, CAST(gross_amount AS TEXT), "
                "allocation_baseline_cents(gross_amount), CAST(gst_amount AS TEXT), "
                "allocation_baseline_cents(gst_amount) "
                "FROM invoice_payment_tax_components WHERE id=:id"
            ),
            {"id": component_id},
        ).one()
        component_gross_cents = component[4]
        component_gst_cents = component[6]
        conn.execute(
            text(
                "INSERT INTO allocation_history_baseline_tax_bindings "
                "(allocation_binding_id, source_component_row_id, tax_code, "
                "component_gross_cents, component_gross_source, component_gst_cents, "
                "component_gst_source) VALUES (:binding_id, :row_id, :tax_code, "
                ":gross_cents, :gross_source, :gst_cents, :gst_source)"
            ),
            {
                "binding_id": binding_id,
                "row_id": component.id,
                "tax_code": component.tax_code,
                "gross_cents": component_gross_cents,
                "gross_source": component[3],
                "gst_cents": component_gst_cents,
                "gst_source": component[5],
            },
        )
    return event_id


def _add_second_ap_invoice(conn):
    conn.execute(
        text(
            "INSERT INTO invoices (id, direction, contact_id, invoice_number, issue_date, "
            "currency, subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
            "source, created_at, updated_at) VALUES "
            "(2, 'AP', 1, 'INV-AP-SYNTHETIC-2', '2026-09-03', 'AUD', "
            "1, 0, 1, 1, 'draft', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )


def _finalize_legacy_baseline(
    conn,
    event_id,
    *,
    status="complete",
    reasons=(),
    payment_date="2026-10-01",
    integrity_hash=None,
):
    observed_at = "2026-10-02 09:00:00"
    bank_transaction_id = conn.execute(
        text("SELECT bank_transaction_id FROM allocation_history_events WHERE id=:id"),
        {"id": event_id},
    ).scalar_one()
    allocations = conn.execute(
        text(
            "SELECT id, baseline_event_id, bank_transaction_id, allocation_row_id, "
            "source_kind, invoice_or_bill_id, allocated_gross_cents, "
            "allocated_gross_source, allocated_gst_cents, allocated_gst_source, "
            "allocation_created_at FROM allocation_history_baseline_bindings "
            "WHERE baseline_event_id=:id"
        ),
        {"id": event_id},
    ).all()
    components = conn.execute(
        text(
            "SELECT tb.id, tb.allocation_binding_id, tb.source_component_row_id, "
            "tb.tax_code, tb.component_gross_cents, tb.component_gross_source, "
            "tb.component_gst_cents, tb.component_gst_source "
            "FROM allocation_history_baseline_tax_bindings tb "
            "JOIN allocation_history_baseline_bindings b ON b.id=tb.allocation_binding_id "
            "WHERE b.baseline_event_id=:id"
        ),
        {"id": event_id},
    ).all()
    gross_total = sum(row.allocated_gross_cents or 0 for row in allocations)
    gst_total = sum(row.component_gst_cents or 0 for row in components)
    allocation_values = [
        ["allocation", *tuple(row)] for row in allocations
    ]
    component_values = [["component", *tuple(row)] for row in components]
    reason_json = json.dumps(list(reasons), separators=(",", ":"))
    meta = [
        "meta", event_id, bank_transaction_id, payment_date, observed_at,
        len(allocations), len(components), gross_total, gst_total, status, reason_json,
    ]
    digest = integrity_hash or allocation_baseline_integrity_hash(
        [meta, *allocation_values, *component_values]
    )
    return conn.execute(
        text(
            "INSERT INTO allocation_history_baseline_finalizations "
            "(baseline_event_id, bank_transaction_id, observed_payment_date, observed_at, "
            "observed_allocation_count, observed_component_count, "
            "gross_allocation_total_cents, gst_total_cents, integrity_hash, status, "
            "incomplete_reason_codes) VALUES (:event_id, :bank_id, :payment_date, "
            ":observed_at, :allocation_count, :component_count, :gross_total, "
            ":gst_total, :digest, :status, :reasons)"
        ),
        {
            "event_id": event_id,
            "bank_id": bank_transaction_id,
            "payment_date": payment_date,
            "observed_at": observed_at,
            "allocation_count": len(allocations),
            "component_count": len(components),
            "gross_total": gross_total,
            "gst_total": gst_total,
            "digest": digest,
            "status": status,
            "reasons": reason_json,
        },
    )


def _insert_allocation_snapshot(
    conn, *, gross_cents=1000, gst_cents=100, components=None
):
    snapshot_id = conn.execute(
        text(
            "INSERT INTO allocation_history_snapshots "
            "(source_kind, invoice_id, gross_cents, gst_cents, created_at) "
            "VALUES ('bill', 1, :gross_cents, :gst_cents, '2026-10-02 09:00:00') "
            "RETURNING id"
        ),
        {"gross_cents": gross_cents, "gst_cents": gst_cents},
    ).scalar_one()
    for tax_code, component_gross, component_gst in (
        [("standard", gross_cents, gst_cents)] if components is None else components
    ):
        conn.execute(
            text(
                "INSERT INTO allocation_history_tax_components "
                "(snapshot_id, tax_code, gross_cents, gst_cents) "
                "VALUES (:snapshot_id, :tax_code, :gross_cents, :gst_cents)"
            ),
            {
                "snapshot_id": snapshot_id,
                "tax_code": tax_code,
                "gross_cents": component_gross,
                "gst_cents": component_gst,
            },
        )
    if components is None:
        _finalize_allocation_snapshot(conn, snapshot_id, 1, gross_cents, gst_cents)
    return snapshot_id


def _finalize_allocation_snapshot(conn, snapshot_id, component_count, gross_cents, gst_cents):
    return conn.execute(
        text(
            "INSERT INTO allocation_history_finalizations "
            "(snapshot_id, component_count, gross_cents, gst_cents, finalized_at) "
            "VALUES (:snapshot_id, :component_count, :gross_cents, :gst_cents, "
            "'2026-10-02 09:01:00')"
        ),
        {
            "snapshot_id": snapshot_id,
            "component_count": component_count,
            "gross_cents": gross_cents,
            "gst_cents": gst_cents,
        },
    )


def _insert_allocation_history_event(
    conn,
    snapshot_id,
    *,
    operation="allocation_correction",
    version=1,
    expected_prior_version=0,
    linked_event_id=None,
    linked_allocation_id=1,
    idempotency_key="allocation-correction-1",
    effective_date="2026-10-02",
    recorded_at="2026-10-02 09:02:00",
    evidence_reference="synthetic://allocation-evidence",
    evidence_sha256=None,
    reason="Synthetic correction reason",
    approval_id=None,
    bank_transaction_id=1,
    allocation_id=1,
):
    return conn.execute(
        text(
            "INSERT INTO allocation_history_events "
            "(operation, bank_transaction_id, allocation_id, snapshot_id, version, "
            "effective_date, recorded_at, idempotency_key, expected_prior_version, "
            "actor_placeholder, reason, evidence_reference, evidence_sha256, "
            "linked_event_id, linked_allocation_id, approval_id) "
            "VALUES (:operation, :bank_transaction_id, :allocation_id, :snapshot_id, "
            ":version, :effective_date, :recorded_at, :idempotency_key, :expected_prior_version, "
            "'synthetic-actor', :reason, :evidence_reference, :evidence_sha256, "
            ":linked_event_id, :linked_allocation_id, :approval_id) RETURNING id"
        ),
        {
            "operation": operation,
            "bank_transaction_id": bank_transaction_id,
            "allocation_id": allocation_id,
            "snapshot_id": snapshot_id,
            "version": version,
            "effective_date": effective_date,
            "recorded_at": recorded_at,
            "idempotency_key": idempotency_key,
            "expected_prior_version": expected_prior_version,
            "reason": reason,
            "evidence_reference": evidence_reference,
            "evidence_sha256": evidence_sha256,
            "linked_event_id": linked_event_id,
            "linked_allocation_id": linked_allocation_id,
            "approval_id": approval_id,
        },
    ).scalar_one()


def test_allocation_history_evidence_legacy_unknown_and_versions_fail_closed(tmp_path):
    engine = _allocation_history_engine(tmp_path)
    with engine.begin() as conn:
        snapshot_id = _insert_allocation_snapshot(conn)
        for evidence_reference, evidence_sha256, reason in (
            (None, None, "Synthetic correction reason"),
            (" \t ", None, "Synthetic correction reason"),
            ("synthetic://evidence", "not-a-sha256", "Synthetic correction reason"),
            ("synthetic://evidence", None, " \t "),
        ):
            with pytest.raises(Exception):
                _insert_allocation_history_event(
                    conn,
                    snapshot_id,
                    idempotency_key=f"invalid-{evidence_reference}-{evidence_sha256}-{reason}",
                    evidence_reference=evidence_reference,
                    evidence_sha256=evidence_sha256,
                    reason=reason,
                )

        with pytest.raises(Exception, match="Invalid allocation history event"):
            _insert_allocation_history_event(
                conn,
                snapshot_id,
                idempotency_key="invalid-recorded-at",
                recorded_at="not a timestamp",
            )

        first_id = _insert_allocation_history_event(
            conn, snapshot_id, evidence_sha256="a" * 64
        )
        with pytest.raises(Exception, match="Invalid allocation history event|append-only"):
            _insert_allocation_history_event(
                conn,
                snapshot_id,
                version=2,
                expected_prior_version=1,
                idempotency_key="stale-allocation-version",
            )
        with pytest.raises(Exception):
            _insert_allocation_history_event(conn, snapshot_id)
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO allocation_history_events "
                    "(id, operation, bank_transaction_id) "
                    "VALUES (1, 'legacy_history_unknown', 1)"
                )
            )

        # Unknown legacy state has no invented allocation, date, actor, reason,
        # evidence, idempotency key, or version, and may occur only once per txn.
        conn.execute(
            text(
                "INSERT INTO allocation_history_events (operation, bank_transaction_id) "
                "VALUES ('legacy_history_unknown', 2)"
            )
        )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO allocation_history_events "
                    "(operation, bank_transaction_id, evidence_reference) "
                    "VALUES ('legacy_history_unknown', 1, 'synthetic://invented')"
                )
            )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO allocation_history_events "
                    "(operation, bank_transaction_id) "
                    "VALUES ('legacy_history_unknown', 2)"
                )
            )
        unknown = conn.execute(
            text(
                "SELECT allocation_id, version, effective_date, recorded_at, "
                "idempotency_key, expected_prior_version, actor_placeholder, reason, "
                "evidence_reference, evidence_sha256, snapshot_id "
                "FROM allocation_history_events WHERE operation='legacy_history_unknown'"
            )
        ).one()
        assert unknown == (None,) * 11
        assert first_id == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM bas_period_lodgment_markers")
        ).scalar_one() == 0
    engine.dispose()


def test_allocation_snapshots_require_exact_integer_cent_finalization(tmp_path):
    engine = _allocation_history_engine(tmp_path)
    with engine.begin() as conn:
        snapshot_id = conn.execute(
            text(
                "INSERT INTO allocation_history_snapshots "
                "(source_kind, invoice_id, gross_cents, gst_cents, created_at) "
                "VALUES ('bill', 1, 1000, 100, '2026-10-02 09:00:00') RETURNING id"
            )
        ).scalar_one()
        with pytest.raises(Exception, match="does not reconcile"):
              _finalize_allocation_snapshot(conn, snapshot_id, 1, 1000, 1000)
        conn.execute(
            text(
                "INSERT INTO allocation_history_tax_components "
                "(snapshot_id, tax_code, gross_cents, gst_cents) "
                "VALUES (:id, 'standard', 400, 40)"
            ),
            {"id": snapshot_id},
        )
        with pytest.raises(Exception, match="does not reconcile"):
            _finalize_allocation_snapshot(conn, snapshot_id, 1, 1000, 100)
        conn.execute(
            text(
                "INSERT INTO allocation_history_tax_components "
                "(snapshot_id, tax_code, gross_cents, gst_cents) "
                "VALUES (:id, 'gst_free', 600, 60)"
            ),
            {"id": snapshot_id},
        )
        _finalize_allocation_snapshot(conn, snapshot_id, 2, 1000, 100)
        with pytest.raises(Exception, match="append-only"):
            _finalize_allocation_snapshot(conn, snapshot_id, 2, 1000, 100)
        with pytest.raises(Exception, match="finalized"):
            conn.execute(
                text(
                    "INSERT INTO allocation_history_tax_components "
                    "(snapshot_id, tax_code, gross_cents, gst_cents) "
                    "VALUES (:id, 'capital', 1, 0)"
                ),
                {"id": snapshot_id},
            )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO allocation_history_snapshots "
                    "(id, source_kind, invoice_id, gross_cents, gst_cents, created_at) "
                    "VALUES (:id, 'bill', 1, 1000, 100, '2026-10-02 09:00:00')"
                ),
                {"id": snapshot_id},
            )
        component_id = conn.execute(
            text(
                "SELECT id FROM allocation_history_tax_components "
                "WHERE snapshot_id=:id ORDER BY id LIMIT 1"
            ),
            {"id": snapshot_id},
        ).scalar_one()
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO allocation_history_tax_components "
                    "(id, snapshot_id, tax_code, gross_cents, gst_cents) "
                    "VALUES (:id, :snapshot_id, 'standard', 400, 40)"
                ),
                {"id": component_id, "snapshot_id": snapshot_id},
            )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO allocation_history_finalizations "
                    "(snapshot_id, component_count, gross_cents, gst_cents, finalized_at) "
                    "VALUES (:id, 2, 1000, 100, '2026-10-02 09:01:00')"
                ),
                {"id": snapshot_id},
            )
        incomplete_snapshot = conn.execute(
            text(
                "INSERT INTO allocation_history_snapshots "
                "(source_kind, invoice_id, gross_cents, gst_cents, created_at) "
                "VALUES ('bill', 1, 1000, 100, '2026-10-02 10:00:00') RETURNING id"
            )
        ).scalar_one()
        with pytest.raises(Exception, match="Invalid allocation history event"):
            _insert_allocation_history_event(
                conn,
                incomplete_snapshot,
                idempotency_key="incomplete-snapshot-cannot-be-used",
            )

        for table, key in (
            ("allocation_history_snapshots", "id"),
            ("allocation_history_tax_components", "id"),
            ("allocation_history_finalizations", "snapshot_id"),
        ):
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(f"UPDATE {table} SET {key}={key}"))
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(f"DELETE FROM {table}"))
    engine.dispose()


def test_legacy_allocation_baseline_complete_is_cash_neutral(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-complete.db")
    with engine.begin() as conn:
        cash_before = conn.execute(
            text("SELECT amount, occurred_at FROM bank_transactions WHERE id=1")
        ).one()
        paid_before = conn.execute(
            text("SELECT paid_amount FROM invoices WHERE id=1")
        ).scalar_one()
        event_id = _start_legacy_baseline(conn)
        _finalize_legacy_baseline(conn, event_id)
        finalized = conn.execute(
            text(
                "SELECT status, observed_allocation_count, observed_component_count, "
                "gross_allocation_total_cents, gst_total_cents, integrity_hash, "
                "incomplete_reason_codes FROM allocation_history_baseline_finalizations "
                "WHERE baseline_event_id=:id"
            ),
            {"id": event_id},
        ).one()
        assert finalized[:5] == ("complete", 1, 1, 1000, 100)
        assert len(finalized.integrity_hash) == 64
        assert finalized.incomplete_reason_codes == "[]"
        assert conn.execute(
            text("SELECT amount, occurred_at FROM bank_transactions WHERE id=1")
        ).one() == cash_before
        assert conn.execute(
            text("SELECT paid_amount FROM invoices WHERE id=1")
        ).scalar_one() == paid_before == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM credit_note_applications")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM credit_note_refunds")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM gst_adjustment_events")
        ).scalar_one() == 0
        with pytest.raises(Exception, match="Bank transaction has allocation history"):
            conn.execute(text("DELETE FROM bank_transactions WHERE id=1"))
        with pytest.raises(Exception, match="identity is protected"):
            conn.execute(
                text("UPDATE bank_transactions SET occurred_at='2026-10-02' WHERE id=1")
            )
        with pytest.raises(Exception, match="Bank transaction has allocation history"):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO bank_transactions "
                    "(id, bank_account_id, direction, amount, occurred_at, gst_amount, "
                    "tax_code, unapplied_amount) "
                    "VALUES (1, 1, 'out', 99, '2026-10-02', 0, 'none', 0)"
                )
            )
    engine.dispose()


def test_legacy_baseline_float_trigger_is_repaired_idempotently(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-trigger-repair.db")
    trigger_name = "trg_allocation_history_baseline_finalizations_domain"
    legacy_trigger_sql = f'''CREATE TRIGGER "{trigger_name}"
        BEFORE INSERT ON "allocation_history_baseline_finalizations"
        WHEN (SELECT amount*100=ROUND(amount*100)
              FROM invoice_payment_allocations WHERE id=1)
        BEGIN SELECT RAISE(ABORT,
            'Legacy allocation baseline does not match observed rows'); END'''
    with engine.begin() as conn:
        conn.execute(text(f'DROP TRIGGER "{trigger_name}"'))
        conn.exec_driver_sql(legacy_trigger_sql)

    run_company_migrations(engine)
    with engine.connect() as conn:
        repaired_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": trigger_name},
        ).scalar_one()
        assert "allocation_baseline_cents" in repaired_sql
        assert "amount*100" not in repaired_sql
        assert "Legacy allocation baseline does not match observed rows" in repaired_sql

    run_company_migrations(engine)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": trigger_name},
        ).scalar_one() == repaired_sql

    quoted_literal_variant = repaired_sql.replace(
        "Legacy allocation baseline does not match observed rows",
        "legacy allocation baseline does not match observed rows",
    )
    assert quoted_literal_variant != repaired_sql
    with engine.begin() as conn:
        conn.execute(text(f'DROP TRIGGER "{trigger_name}"'))
        conn.exec_driver_sql(quoted_literal_variant)
    run_company_migrations(engine)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
            {"name": trigger_name},
        ).scalar_one() == repaired_sql
    engine.dispose()


def test_legacy_allocation_baseline_missing_components_is_incomplete(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-missing-component.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn, component=False)
        _finalize_legacy_baseline(
            conn,
            event_id,
            status="incomplete",
            reasons=("missing_tax_components",),
        )
        assert conn.execute(
            text(
                "SELECT status, incomplete_reason_codes, gross_allocation_total_cents, "
                "gst_total_cents FROM allocation_history_baseline_finalizations "
                "WHERE baseline_event_id=:id"
            ),
            {"id": event_id},
        ).one() == ("incomplete", '["missing_tax_components"]', 1000, 0)
    engine.dispose()


def test_legacy_allocation_baseline_invalid_amounts_finalize_incomplete(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-invalid-amount.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(
            conn, allocation_gross=10.005, component_gross=10.005
        )
        _finalize_legacy_baseline(
            conn,
            event_id,
            status="incomplete",
            reasons=("invalid_amount", "unreconciled_totals"),
        )
        assert conn.execute(
            text(
                "SELECT status, incomplete_reason_codes FROM "
                "allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
            ),
            {"id": event_id},
        ).one() == (
            "incomplete",
            '["invalid_amount","unreconciled_totals"]',
        )
        assert conn.execute(
            text(
                "SELECT allocated_gross_cents, allocated_gross_source "
                "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
            ),
            {"id": event_id},
        ).one() == (None, "10.005")
    engine.dispose()


def test_legacy_baseline_preserves_negative_amount_as_incomplete(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-negative-amount.db")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA ignore_check_constraints=ON"))
        event_id = _start_legacy_baseline(
            conn, allocation_gross=-1, component_gross=10, component_gst=1
        )
        _finalize_legacy_baseline(
            conn,
            event_id,
            status="incomplete",
            reasons=("invalid_amount", "unreconciled_totals"),
        )
        assert conn.execute(
            text(
                "SELECT status, gross_allocation_total_cents, incomplete_reason_codes "
                "FROM allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
            ),
            {"id": event_id},
        ).one() == (
            "incomplete",
            0,
            '["invalid_amount","unreconciled_totals"]',
        )
    engine.dispose()


def test_legacy_baseline_preserves_malformed_payment_date_as_incomplete(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-invalid-date.db")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE bank_transactions SET occurred_at='unknown-date' WHERE id=1")
        )
        event_id = _start_legacy_baseline(conn)
        _finalize_legacy_baseline(
            conn,
            event_id,
            status="incomplete",
            reasons=("insufficient_source_data",),
            payment_date="unknown-date",
        )
        assert conn.execute(
            text(
                "SELECT observed_payment_date, status, incomplete_reason_codes "
                "FROM allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
            ),
            {"id": event_id},
        ).one() == (
            "unknown-date",
            "incomplete",
            '["insufficient_source_data"]',
        )
    engine.dispose()


def test_legacy_allocation_baseline_unsupported_tax_evidence_is_incomplete(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-unsupported-tax.db")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA ignore_check_constraints=ON"))
        conn.execute(
            text("UPDATE invoice_payment_tax_components SET tax_code='unknown' WHERE id=1")
        )
        event_id = _start_legacy_baseline(conn, tax_code="unknown")
        _finalize_legacy_baseline(
            conn,
            event_id,
            status="incomplete",
            reasons=("unsupported_tax_evidence",),
        )
        assert conn.execute(
            text(
                "SELECT status, incomplete_reason_codes FROM "
                "allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
            ),
            {"id": event_id},
        ).one() == ("incomplete", '["unsupported_tax_evidence"]')
    engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    (
        "allocation_insert",
        "allocation_update",
        "allocation_delete",
        "allocation_replace",
        "allocation_reparent_transaction",
        "allocation_reparent_document",
        "allocation_clear",
        "component_insert",
        "component_update",
        "component_delete",
        "component_replace",
        "component_reparent",
        "component_clear",
    ),
)
def test_finalized_legacy_baseline_rejects_live_row_mutations(tmp_path, mutation):
    engine = _allocation_history_engine(tmp_path, f"legacy-baseline-guard-{mutation}.db")
    with engine.begin() as conn:
        _add_second_ap_invoice(conn)
        conn.execute(
            text(
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (2, 2, 1, 1, 0)"
            )
        )
        event_id = _start_legacy_baseline(conn)
        _finalize_legacy_baseline(conn, event_id)
        mutations = {
            "allocation_insert": (
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (3, 1, 2, 1, 0)"
            ),
            "allocation_update": "UPDATE invoice_payment_allocations SET amount=11 WHERE id=1",
            "allocation_delete": "DELETE FROM invoice_payment_allocations WHERE id=1",
            "allocation_replace": (
                "INSERT OR REPLACE INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (1, 1, 1, 10, 1)"
            ),
            "allocation_reparent_transaction": (
                "UPDATE invoice_payment_allocations SET bank_transaction_id=2 WHERE id=1"
            ),
            "allocation_reparent_document": (
                "UPDATE invoice_payment_allocations SET invoice_id=2 WHERE id=1"
            ),
            "allocation_clear": (
                "DELETE FROM invoice_payment_allocations WHERE bank_transaction_id=1"
            ),
            "component_insert": (
                "INSERT INTO invoice_payment_tax_components "
                "(allocation_id, tax_code, gross_amount, gst_amount) "
                "VALUES (1, 'gst_free', 1, 0)"
            ),
            "component_update": (
                "UPDATE invoice_payment_tax_components SET gross_amount=9 WHERE id=1"
            ),
            "component_delete": "DELETE FROM invoice_payment_tax_components WHERE id=1",
            "component_replace": (
                "INSERT OR REPLACE INTO invoice_payment_tax_components "
                "(id, allocation_id, tax_code, gross_amount, gst_amount) "
                "VALUES (1, 1, 'standard', 10, 1)"
            ),
            "component_reparent": (
                "UPDATE invoice_payment_tax_components SET allocation_id=2 WHERE id=1"
            ),
            "component_clear": (
                "DELETE FROM invoice_payment_tax_components WHERE allocation_id=1"
            ),
        }
        with pytest.raises(Exception, match="Finalized legacy allocation baseline is immutable"):
            conn.execute(text(mutations[mutation]))
    engine.dispose()


def test_unrelated_transaction_allocations_remain_mutable_after_baseline(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-unrelated-rows.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn)
        _finalize_legacy_baseline(conn, event_id)
        conn.execute(
            text(
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (2, 2, 1, 1, 0)"
            )
        )
        conn.execute(
            text("UPDATE invoice_payment_allocations SET amount=2 WHERE id=2")
        )
        conn.execute(
            text("DELETE FROM invoice_payment_allocations WHERE id=2")
        )
        assert conn.execute(
            text("SELECT COUNT(*) FROM invoice_payment_allocations WHERE id=2")
        ).scalar_one() == 0
    engine.dispose()


def test_incomplete_legacy_baseline_also_freezes_observed_allocations(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-incomplete-immutable.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn, component=False)
        _finalize_legacy_baseline(
            conn,
            event_id,
            status="incomplete",
            reasons=("missing_tax_components",),
        )
        with pytest.raises(
            Exception,
            match="Finalized legacy allocation baseline is immutable",
        ):
            conn.execute(
                text("UPDATE invoice_payment_allocations SET amount=11 WHERE id=1")
            )
        with pytest.raises(
            Exception,
            match="Finalized legacy allocation baseline is immutable",
        ):
            conn.execute(
                text("DELETE FROM invoice_payment_allocations WHERE id=1")
            )
    engine.dispose()


def test_legacy_baseline_migration_does_not_backfill_existing_rows(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-no-backfill.db")
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM invoice_payment_allocations")
        ).scalar_one() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM allocation_history_baseline_bindings")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM allocation_history_baseline_finalizations")
        ).scalar_one() == 0
    run_company_migrations(engine)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM allocation_history_baseline_bindings")
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM allocation_history_baseline_finalizations")
        ).scalar_one() == 0
    engine.dispose()


def test_legacy_baseline_bindings_and_finalization_survive_bank_rebuild(
    tmp_path, monkeypatch
):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-bank-rebuild.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn)
        _finalize_legacy_baseline(conn, event_id)
        before = conn.execute(
            text(
                "SELECT b.id, b.baseline_event_id, b.bank_transaction_id, "
                "b.allocation_row_id, b.invoice_or_bill_id, b.allocated_gross_cents, "
                "tb.id, tb.source_component_row_id, tb.tax_code, "
                "tb.component_gross_cents, tb.component_gst_cents, f.status, f.integrity_hash "
                "FROM allocation_history_baseline_bindings b "
                "JOIN allocation_history_baseline_tax_bindings tb "
                  "ON tb.allocation_binding_id=b.id "
                "JOIN allocation_history_baseline_finalizations f "
                  "ON f.baseline_event_id=b.baseline_event_id"
            )
        ).one()
    monkeypatch.setattr(migrations, "_bank_needs_rebuild", lambda _conn: True)
    rebuilt = run_company_migrations(engine)
    assert "rebuild:bank_transactions" in rebuilt
    with engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT b.id, b.baseline_event_id, b.bank_transaction_id, "
                "b.allocation_row_id, b.invoice_or_bill_id, b.allocated_gross_cents, "
                "tb.id, tb.source_component_row_id, tb.tax_code, "
                "tb.component_gross_cents, tb.component_gst_cents, f.status, f.integrity_hash "
                "FROM allocation_history_baseline_bindings b "
                "JOIN allocation_history_baseline_tax_bindings tb "
                  "ON tb.allocation_binding_id=b.id "
                "JOIN allocation_history_baseline_finalizations f "
                  "ON f.baseline_event_id=b.baseline_event_id"
            )
        ).one()
        assert after == before
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()


def test_legacy_baseline_supports_service_begin_immediate(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-immediate.db")
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
        conn.rollback()
    engine.dispose()


def test_legacy_baseline_bindings_and_finalizations_are_append_only(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-append-only.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn)
        _finalize_legacy_baseline(conn, event_id)
        for table, key in (
            ("allocation_history_baseline_bindings", "id"),
            ("allocation_history_baseline_tax_bindings", "id"),
            ("allocation_history_baseline_finalizations", "baseline_event_id"),
        ):
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(f"UPDATE {table} SET {key}={key}"))
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(f"DELETE FROM {table}"))
        with pytest.raises(Exception, match="append-only|Invalid or finalized"):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO allocation_history_baseline_bindings "
                    "(id, baseline_event_id, bank_transaction_id, allocation_row_id) "
                    "SELECT id, baseline_event_id, bank_transaction_id, allocation_row_id "
                    "FROM allocation_history_baseline_bindings LIMIT 1"
                )
            )
        with pytest.raises(Exception, match="append-only"):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO allocation_history_baseline_finalizations "
                    "(baseline_event_id, bank_transaction_id, observed_payment_date, "
                    "observed_at, observed_allocation_count, observed_component_count, "
                    "gross_allocation_total_cents, gst_total_cents, integrity_hash, status, "
                    "incomplete_reason_codes) SELECT baseline_event_id, bank_transaction_id, "
                    "observed_payment_date, observed_at, observed_allocation_count, "
                    "observed_component_count, gross_allocation_total_cents, gst_total_cents, "
                    "integrity_hash, status, incomplete_reason_codes "
                    "FROM allocation_history_baseline_finalizations"
                )
            )
    engine.dispose()


@pytest.mark.parametrize(
    "table",
    ("invoice_payment_allocations", "invoice_payment_tax_components"),
)
def test_finalized_baseline_rejects_rowid_update_aliases(tmp_path, table):
    engine = _allocation_history_engine(tmp_path, f"legacy-baseline-rowid-{table}.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn)
        _finalize_legacy_baseline(conn, event_id)
        with pytest.raises(
            Exception,
            match="Finalized legacy allocation baseline is immutable",
        ):
            conn.execute(text(f"UPDATE {table} SET rowid=id WHERE id=1"))
    engine.dispose()


@pytest.mark.parametrize(
    "failure_case",
    (
        "changed_allocation",
        "changed_component",
        "unbound_allocation",
        "unbound_component",
        "wrong_payment_date",
        "wrong_hash",
    ),
)
def test_legacy_baseline_finalization_rechecks_observed_state(tmp_path, failure_case):
    engine = _allocation_history_engine(tmp_path, f"legacy-baseline-drift-{failure_case}.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn)
        if failure_case == "changed_allocation":
            conn.execute(
                text("UPDATE invoice_payment_allocations SET amount=11 WHERE id=1")
            )
        elif failure_case == "changed_component":
            conn.execute(
                text("UPDATE invoice_payment_tax_components SET gst_amount=0.5 WHERE id=1")
            )
        elif failure_case == "unbound_allocation":
            _add_second_ap_invoice(conn)
            conn.execute(
                text(
                    "INSERT INTO invoice_payment_allocations "
                    "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                    "VALUES (3, 1, 2, 1, 0)"
                )
            )
        elif failure_case == "unbound_component":
            conn.execute(
                text(
                    "INSERT INTO invoice_payment_tax_components "
                    "(allocation_id, tax_code, gross_amount, gst_amount) "
                    "VALUES (1, 'gst_free', 1, 0)"
                )
            )
        elif failure_case == "wrong_hash":
            with pytest.raises(Exception, match="does not match observed rows"):
                _finalize_legacy_baseline(conn, event_id, integrity_hash="0" * 64)
            engine.dispose()
            return

        payment_date = "2026-10-02" if failure_case == "wrong_payment_date" else "2026-10-01"
        with pytest.raises(Exception, match="does not match observed rows"):
            _finalize_legacy_baseline(
                conn,
                event_id,
                payment_date=payment_date,
                status="incomplete" if failure_case == "unbound_component" else "complete",
                reasons=("missing_tax_components",) if failure_case == "unbound_component" else (),
            )
    engine.dispose()


def test_legacy_baseline_duplicate_and_wrong_ownership_bindings_fail_closed(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-identities.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn)
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO allocation_history_baseline_bindings "
                    "(baseline_event_id, bank_transaction_id, allocation_row_id) "
                    "VALUES (:event_id, 1, 1)"
                ),
                {"event_id": event_id},
            )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO allocation_history_baseline_tax_bindings "
                    "(allocation_binding_id, source_component_row_id) "
                    "SELECT allocation_binding_id, source_component_row_id "
                    "FROM allocation_history_baseline_tax_bindings LIMIT 1"
                )
            )
        with pytest.raises(Exception, match="Invalid or finalized legacy allocation binding"):
            conn.execute(
                text(
                    "INSERT INTO allocation_history_baseline_bindings "
                    "(baseline_event_id, bank_transaction_id, allocation_row_id) "
                    "VALUES (:event_id, 2, 2)"
                ),
                {"event_id": event_id},
            )
        _add_second_ap_invoice(conn)
        conn.execute(
            text(
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (3, 1, 2, 1, 0)"
            )
        )
        allocation = conn.execute(
            text(
                "SELECT a.amount, a.gst_amount, a.created_at FROM "
                "invoice_payment_allocations a WHERE a.id=3"
            )
        ).one()
        conn.execute(
            text(
                "INSERT INTO allocation_history_baseline_bindings "
                "(baseline_event_id, bank_transaction_id, allocation_row_id, source_kind, "
                "invoice_or_bill_id, allocated_gross_cents, allocated_gross_source, "
                "allocated_gst_cents, allocated_gst_source, allocation_created_at) "
                "VALUES (:event_id, 1, 3, 'bill', 999, 100, CAST(:amount AS TEXT), "
                "0, CAST(:gst AS TEXT), CAST(:created AS TEXT))"
            ),
            {
                "event_id": event_id,
                "amount": allocation.amount,
                "gst": allocation.gst_amount,
                "created": allocation.created_at,
            },
        )
        with pytest.raises(Exception, match="does not match observed rows"):
            _finalize_legacy_baseline(conn, event_id, status="incomplete", reasons=("missing_tax_components",))
    engine.dispose()


def test_legacy_baseline_rejects_component_bound_to_wrong_allocation(tmp_path):
    engine = _allocation_history_engine(tmp_path, "legacy-baseline-component-owner.db")
    with engine.begin() as conn:
        event_id = _start_legacy_baseline(conn)
        _add_second_ap_invoice(conn)
        conn.execute(
            text(
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (3, 1, 2, 1, 0)"
            )
        )
        second_binding_id = conn.execute(
            text(
                "INSERT INTO allocation_history_baseline_bindings "
                "(baseline_event_id, bank_transaction_id, allocation_row_id, source_kind, "
                "invoice_or_bill_id, allocated_gross_cents, allocated_gross_source, "
                "allocated_gst_cents, allocated_gst_source, allocation_created_at) "
                "SELECT :event_id, 1, id, 'bill', invoice_id, 100, CAST(amount AS TEXT), "
                "0, CAST(gst_amount AS TEXT), CAST(created_at AS TEXT) "
                "FROM invoice_payment_allocations WHERE id=3 RETURNING id"
            ),
            {"event_id": event_id},
        ).scalar_one()
        component_id = conn.execute(
            text(
                "INSERT INTO invoice_payment_tax_components "
                "(allocation_id, tax_code, gross_amount, gst_amount) "
                "VALUES (3, 'standard', 1, 0) RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO allocation_history_baseline_tax_bindings "
                "(allocation_binding_id, source_component_row_id, tax_code, "
                "component_gross_cents, component_gross_source, component_gst_cents, "
                "component_gst_source) VALUES (1, :component_id, 'standard', 100, '1', 0, '0')"
            ),
            {"component_id": component_id},
        )
        assert second_binding_id != 1
        with pytest.raises(Exception, match="does not match observed rows"):
            _finalize_legacy_baseline(
                conn,
                event_id,
                status="incomplete",
                reasons=("missing_tax_components",),
            )
    engine.dispose()


def test_allocation_corrections_reversals_and_period_approvals(tmp_path):
    engine = _allocation_history_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bas_period_lodgment_markers "
                "(period_id, period_start, period_end, lodged_status, internally_asserted_at, "
                "actor_placeholder, evidence_reference, evidence_scope) "
                "VALUES ('synthetic-2026-10', '2026-10-01', '2026-10-02', 1, "
                "'2026-11-01 09:00:00', 'synthetic-actor', 'synthetic://internal-lodgment-assertion', "
                "'internal_assertion_not_ato_lodgment_evidence')"
            )
        )
        with pytest.raises(Exception, match="Invalid internal BAS period marker"):
            conn.execute(
                text(
                    "INSERT INTO bas_period_lodgment_markers "
                    "(period_id, period_start, period_end, lodged_status, internally_asserted_at, "
                    "actor_placeholder, evidence_reference, evidence_scope) "
                    "VALUES ('blank-evidence', '2026-10-01', '2026-10-31', 1, "
                    "'2026-11-01', 'synthetic', ' \t ', "
                    "'internal_assertion_not_ato_lodgment_evidence')"
                )
            )
        snapshot_id = _insert_allocation_snapshot(conn)
        with pytest.raises(Exception, match="Invalid allocation history event"):
            _insert_allocation_history_event(
                conn, snapshot_id, idempotency_key="needs-period-approval"
            )

        approval_id = conn.execute(
            text(
                "INSERT INTO allocation_period_correction_approvals "
                "(period_marker_id, allocation_id, approved_idempotency_key, actor_placeholder, "
                "reason, evidence_reference, recorded_at) "
                "VALUES (1, 1, 'needs-period-approval', 'synthetic-approver', "
                "'Synthetic approval reason', 'synthetic://approval', '2026-10-02 08:00:00') "
                "RETURNING id"
            )
        ).scalar_one()
        first_id = _insert_allocation_history_event(
            conn, snapshot_id, idempotency_key="needs-period-approval", approval_id=approval_id
        )
        duplicate_key_snapshot = _insert_allocation_snapshot(conn)
        with pytest.raises(Exception):
            _insert_allocation_history_event(
                conn,
                duplicate_key_snapshot,
                version=2,
                expected_prior_version=1,
                linked_event_id=first_id,
                linked_allocation_id=None,
                idempotency_key="needs-period-approval",
                effective_date="2026-10-03",
            )
        second_snapshot = _insert_allocation_snapshot(conn)
        second_id = _insert_allocation_history_event(
            conn,
            second_snapshot,
            version=2,
            expected_prior_version=1,
            linked_event_id=first_id,
            linked_allocation_id=None,
            idempotency_key="allocation-correction-2",
            effective_date="2026-10-03",
        )
        with pytest.raises(Exception, match="Invalid allocation history event|components do not match"):
            oversized_snapshot = _insert_allocation_snapshot(
                conn, gross_cents=2000, gst_cents=200
            )
            _insert_allocation_history_event(
                conn,
                oversized_snapshot,
                operation="allocation_reversal",
                version=3,
                expected_prior_version=2,
                linked_event_id=second_id,
                linked_allocation_id=None,
                idempotency_key="over-reversal",
                effective_date="2026-10-04",
            )

        mismatched_components_snapshot = _insert_allocation_snapshot(
            conn,
            components=[("standard", 900, 90), ("capital", 100, 10)],
        )
        with pytest.raises(Exception, match="components do not match"):
            _insert_allocation_history_event(
                conn,
                mismatched_components_snapshot,
                operation="allocation_reversal",
                version=3,
                expected_prior_version=2,
                linked_event_id=second_id,
                linked_allocation_id=None,
                idempotency_key="mismatched-reversal-components",
                effective_date="2026-10-04",
            )

        reversal_snapshot = _insert_allocation_snapshot(conn)
        reversal_id = _insert_allocation_history_event(
            conn,
            reversal_snapshot,
            operation="allocation_reversal",
            version=3,
            expected_prior_version=2,
            linked_event_id=second_id,
            linked_allocation_id=None,
            idempotency_key="allocation-reversal-1",
            effective_date="2026-10-05",
        )
        duplicate_reversal_snapshot = _insert_allocation_snapshot(conn)
        with pytest.raises(Exception, match="Invalid allocation history event|append-only"):
            _insert_allocation_history_event(
                conn,
                duplicate_reversal_snapshot,
                operation="allocation_reversal",
                version=4,
                expected_prior_version=3,
                linked_event_id=reversal_id,
                linked_allocation_id=None,
                idempotency_key="allocation-reversal-duplicate",
                effective_date="2026-10-06",
            )
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("UPDATE bas_period_lodgment_markers SET period_end=period_end"))
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("DELETE FROM allocation_period_correction_approvals"))
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO bas_period_lodgment_markers "
                    "(id, period_id, period_start, period_end, lodged_status, "
                    "internally_asserted_at, actor_placeholder, evidence_reference, evidence_scope) "
                    "VALUES (1, 'synthetic-2026-10', '2026-10-01', '2026-10-02', 1, "
                    "'2026-11-01 09:00:00', 'synthetic-actor', "
                    "'synthetic://internal-lodgment-assertion', "
                    "'internal_assertion_not_ato_lodgment_evidence')"
                )
            )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO allocation_period_correction_approvals "
                    "(id, period_marker_id, allocation_id, approved_idempotency_key, "
                    "actor_placeholder, reason, evidence_reference, recorded_at) "
                    "VALUES (1, 1, 1, 'needs-period-approval', 'synthetic-approver', "
                    "'Synthetic approval reason', 'synthetic://approval', '2026-10-02 08:00:00')"
                )
            )
    engine.dispose()


def test_allocation_reversal_rejects_missing_and_cross_transaction_targets(tmp_path):
    engine = _allocation_history_engine(tmp_path, "allocation-cross-transaction.db")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoice_payment_allocations "
                "(id, bank_transaction_id, invoice_id, amount, gst_amount) "
                "VALUES (2, 2, 1, 10, 1)"
            )
        )
        conn.execute(text("UPDATE bank_transactions SET amount=20 WHERE id=2"))
        first_snapshot = _insert_allocation_snapshot(conn)
        first_event_id = _insert_allocation_history_event(
            conn, first_snapshot, idempotency_key="txn-one-version-one"
        )
        second_snapshot = _insert_allocation_snapshot(
            conn, gross_cents=1000, gst_cents=100
        )
        second_event_id = _insert_allocation_history_event(
            conn,
            second_snapshot,
            bank_transaction_id=2,
            allocation_id=2,
            linked_allocation_id=2,
            idempotency_key="txn-two-version-one",
        )
        reversal_snapshot = _insert_allocation_snapshot(
            conn, gross_cents=1000, gst_cents=100
        )
        with pytest.raises(Exception, match="Invalid allocation history event"):
            _insert_allocation_history_event(
                conn,
                reversal_snapshot,
                operation="allocation_reversal",
                version=2,
                expected_prior_version=1,
                linked_event_id=first_event_id,
                linked_allocation_id=None,
                bank_transaction_id=2,
                allocation_id=2,
                idempotency_key="cross-transaction-reversal",
                effective_date="2026-10-03",
            )
        with pytest.raises(Exception, match="Invalid allocation history event"):
            _insert_allocation_history_event(
                conn,
                reversal_snapshot,
                operation="allocation_reversal",
                version=2,
                expected_prior_version=1,
                linked_event_id=999,
                linked_allocation_id=None,
                bank_transaction_id=2,
                allocation_id=2,
                idempotency_key="missing-reversal-target",
                effective_date="2026-10-03",
            )
        assert second_event_id != first_event_id
    engine.dispose()


def test_allocation_history_protects_cash_identity_and_keeps_other_accounting_separate(tmp_path):
    engine = _allocation_history_engine(tmp_path)
    with engine.begin() as conn:
        snapshot_id = _insert_allocation_snapshot(conn)
        _insert_allocation_history_event(conn, snapshot_id)
        cash_before = conn.execute(
            text("SELECT amount, occurred_at FROM bank_transactions WHERE id=1")
        ).one()
        paid_before = conn.execute(
            text("SELECT paid_amount FROM invoices WHERE id=1")
        ).scalar_one()
        with pytest.raises(Exception, match="Bank transaction has allocation history"):
            conn.execute(text("DELETE FROM bank_transactions WHERE id=1"))
        with pytest.raises(Exception, match="identity is protected"):
            conn.execute(
                text("UPDATE bank_transactions SET occurred_at='2026-10-02' WHERE id=1")
            )
        with pytest.raises(Exception, match="identity is protected"):
            conn.execute(text("UPDATE bank_transactions SET id=10 WHERE id=1"))
        with pytest.raises(Exception, match="Bank transaction has allocation history"):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO bank_transactions "
                    "(id, bank_account_id, direction, amount, occurred_at, gst_amount, "
                    "tax_code, unapplied_amount) "
                    "VALUES (1, 1, 'out', 99, '2026-10-09', 0, 'none', 0)"
                )
            )
        conn.execute(text("DELETE FROM bank_transactions WHERE id=2"))
        conn.execute(
            text(
                "INSERT INTO bank_transactions "
                "(id, bank_account_id, direction, amount, occurred_at, gst_amount, "
                "tax_code, unapplied_amount) "
                "VALUES (2, 1, 'out', 1, '2026-10-01', 0, 'none', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT OR REPLACE INTO bank_transactions "
                "(id, bank_account_id, direction, amount, occurred_at, gst_amount, "
                "tax_code, unapplied_amount) "
                "VALUES (2, 1, 'out', 2, '2026-10-01', 0, 'none', 0)"
            )
        )
        _insert_journal_entry(conn, "credit_note_ap", 42)
        journal_id = conn.execute(
            text(
                "SELECT id FROM journal_entries WHERE source_type='credit_note_ap' "
                "AND source_id=42"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO credit_note_applications "
                "(credit_note_id, invoice_id, amount, application_date, status) "
                "VALUES (42, 1, 1, '2026-10-01', 'active')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO credit_note_refunds "
                "(credit_note_id, bank_transaction_id, journal_entry_id, amount, refund_date, status) "
                "VALUES (42, 2, :journal_id, 1, '2026-10-01', 'active')"
            ),
            {"journal_id": journal_id},
        )
        assert conn.execute(
            text("SELECT amount, occurred_at FROM bank_transactions WHERE id=1")
        ).one() == cash_before
        assert conn.execute(
            text("SELECT paid_amount FROM invoices WHERE id=1")
        ).scalar_one() == paid_before == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM invoice_payment_allocations")
        ).scalar_one() == 1
        for table in ("allocation_history_events", "allocation_history_snapshots"):
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(f"UPDATE {table} SET id=id"))
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(f"DELETE FROM {table}"))
    engine.dispose()


def test_gst_adjustment_schema_guards_and_reversal_index(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-schema.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        conn.execute(text('DROP INDEX "uq_gst_adjustment_events_reversal_once"'))
        conn.execute(
            text(
                "CREATE INDEX uq_gst_adjustment_events_reversal_once "
                "ON gst_adjustment_events (policy_version)"
            )
        )
        conn.execute(
            text('DROP TRIGGER "trg_gst_adjustment_events_no_update"')
        )
        conn.execute(
            text(
                "CREATE TRIGGER trg_gst_adjustment_events_no_update "
                "BEFORE UPDATE ON gst_adjustment_events "
                "BEGIN SELECT CASE WHEN 0 THEN "
                "RAISE(ABORT, 'GST adjustment records are append-only') "
                "END; END"
            )
        )
        domain_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_gst_adjustment_events_domain'"
            )
        ).scalar_one()
        spoofed_domain_sql = domain_sql.replace(
            "'standard', 'gst_free', 'input_taxed', 'capital', 'none', 'mixed')",
            "'standard', 'gst_free', 'input_taxed', 'capital', 'none', 'Mixed')",
        )
        assert spoofed_domain_sql != domain_sql
        conn.execute(text("DROP TRIGGER trg_gst_adjustment_events_domain"))
        conn.exec_driver_sql(spoofed_domain_sql)
        spoofed_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_gst_adjustment_events_no_update'"
            )
        ).scalar_one()
        assert "GST adjustment records are append-only" in spoofed_sql

    applied = run_company_migrations(engine)
    assert "index:uq_gst_adjustment_events_reversal_once" in applied
    assert "guards:gst_adjustment_append_only" in applied
    assert "guards:gst_adjustment_append_only" not in run_company_migrations(engine)
    with engine.connect() as conn:
        repaired_domain_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_gst_adjustment_events_domain'"
            )
        ).scalar_one()
        assert "'none', 'mixed')" in repaired_domain_sql
        assert "'none', 'Mixed')" not in repaired_domain_sql

    with engine.begin() as conn:
        with pytest.raises(Exception, match="Invalid GST adjustment event"):
            _insert_gst_event(conn, tax_code="Mixed")

        conn.execute(text("DROP TRIGGER trg_gst_adjustment_events_domain"))
        conn.execute(
            text(
                "CREATE TRIGGER trg_gst_adjustment_events_domain "
                "BEFORE INSERT ON gst_adjustment_events WHEN 0 "
                "BEGIN SELECT RAISE(ABORT, 'legacy permissive event guard'); END"
            )
        )
    assert "guards:gst_adjustment_append_only" in run_company_migrations(engine)
    with engine.begin() as conn:
        with pytest.raises(Exception, match="Invalid GST adjustment event"):
            _insert_gst_event(
                conn, source_record_type=None, source_record_id=None
            )

        # Continue below with repaired canonical triggers and valid provenance.
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_events ("
                "event_type, source_direction, adjustment_direction, projection_box, "
                "amount_cents, gst_amount_cents, tax_code, policy_version, reason, effective_date, "
                "awareness_date, agreement_date, refund_repayment_date, "
                "source_record_type, source_record_id, "
                "adjustment_note_reference, adjustment_note_held_date, "
                "manual_review_status, tax_slice_count"
                ") VALUES ('agreement', 'AR', 'increasing', '1A', 11000, 1000, "
                "'mixed', 'F-02-v1', 'Synthetic agreement', '2026-10-01', "
                "'2026-10-02', '2026-10-03', '2026-10-04', 'credit_note', 42, "
                "'CN-SYNTHETIC', "
                "'2026-10-05', 'not_required', 4)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_events ("
                "event_type, source_direction, adjustment_direction, projection_box, "
                "amount_cents, gst_amount_cents, tax_code, policy_version, reason, effective_date, "
                "source_record_type, source_record_id, manual_review_status, tax_slice_count"
                ") VALUES ('agreement', 'AR', 'increasing', '1A', 11000, 1000, "
                "'mixed', 'F-02-v1', 'Synthetic replacement target', '2026-10-01', "
                "'credit_note', 42, 'pending', 2)"
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
                "VALUES (1, 'gst_free', 500, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (1, 'input_taxed', 300, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (1, 'none', 200, 0)"
            )
        )
        _finalize_gst_event(conn, 1)
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_evidence "
                "(event_id, evidence_type, evidence_reference) "
                "VALUES (1, 'document', 'synthetic://evidence')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reason) VALUES (2, 'pending', 'Synthetic review')"
            )
        )

        for table in (
            "gst_adjustment_events",
            "gst_adjustment_tax_slices",
            "gst_adjustment_finalizations",
            "gst_adjustment_evidence",
            "gst_adjustment_manual_reviews",
        ):
            if table == "gst_adjustment_finalizations":
                update_sql = (
                    "UPDATE gst_adjustment_finalizations "
                    "SET finalized_at=finalized_at WHERE event_id=1"
                )
                delete_sql = "DELETE FROM gst_adjustment_finalizations WHERE event_id=1"
            else:
                update_sql = f"UPDATE {table} SET id=id WHERE id=1"
                delete_sql = f"DELETE FROM {table} WHERE id=1"
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(update_sql))
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(delete_sql))

        replace_statements = (
            "INSERT OR REPLACE INTO gst_adjustment_events "
            "(id, event_type, source_direction, adjustment_direction, projection_box, "
            "amount_cents, gst_amount_cents, tax_code, policy_version, reason, effective_date, "
            "source_record_type, source_record_id, tax_slice_count) "
            "VALUES (1, 'agreement', 'AR', 'decreasing', '1B', 11000, 1000, "
            "'standard', 'F-02-v1', 'Synthetic replacement', '2026-10-06', "
            "'credit_note', 42, 1)",
            "INSERT OR REPLACE INTO gst_adjustment_tax_slices "
            "(id, event_id, tax_code, amount_cents, gst_amount_cents) "
            "VALUES (1, 2, 'standard', 10000, 1000)",
            "INSERT OR REPLACE INTO gst_adjustment_evidence "
            "(id, event_id, evidence_type, evidence_reference) "
            "VALUES (1, 1, 'document', 'synthetic://replacement')",
            "INSERT OR REPLACE INTO gst_adjustment_manual_reviews "
            "(id, event_id, status, reason) "
            "VALUES (1, 2, 'pending', 'Synthetic replacement')",
            "INSERT OR REPLACE INTO gst_adjustment_finalizations "
            "(event_id, tax_slice_count, amount_cents, gst_amount_cents) VALUES (1, 4, 11000, 1000)",
        )
        for statement in replace_statements:
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(statement))

        with pytest.raises(Exception, match="opposite linked event"):
            conn.execute(
                text(
                    "INSERT INTO gst_adjustment_events ("
                    "event_type, source_direction, adjustment_direction, projection_box, "
                    "amount_cents, gst_amount_cents, tax_code, policy_version, reason, effective_date, "
                    "source_record_type, source_record_id, reversal_of_event_id"
                    ") VALUES ('reversal', 'AR', 'increasing', '1A', 11000, 1000, "
                    "'standard', 'F-02-v1', 'Synthetic invalid reversal', "
                    "'2026-10-06', 'credit_note', 42, 1)"
                )
            )

    report = detect_drift(engine, CompanyBase, "gst-adjustment-schema")
    assert report.is_clean, report.format()


def test_gst_adjustment_partial_schema_repairs_existing_and_created_guards(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-partial-schema.db")
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE gst_adjustment_events DROP COLUMN tax_slice_count")
        )
        conn.execute(text("DROP TABLE gst_adjustment_tax_slices"))
        conn.execute(text("DROP TABLE gst_adjustment_finalizations"))
        conn.execute(text("DROP TABLE gst_adjustment_manual_reviews"))

    applied = run_company_migrations(engine)
    assert "guards:gst_adjustment_append_only" in applied

    with engine.connect() as conn:
        event_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(gst_adjustment_events)"))
        }
        assert "tax_slice_count" in event_columns
        existing_triggers = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
    for table in ("gst_adjustment_events", "gst_adjustment_evidence"):
        assert f"trg_{table}_no_update" in existing_triggers
        assert f"trg_{table}_no_delete" in existing_triggers
        assert f"trg_{table}_no_replace_insert" in existing_triggers
    assert "trg_gst_adjustment_events_domain" in existing_triggers
    assert "trg_gst_adjustment_evidence_domain" in existing_triggers
    assert "trg_gst_adjustment_tax_slices_no_update" not in existing_triggers
    assert "trg_gst_adjustment_finalizations_no_update" not in existing_triggers
    assert "trg_gst_adjustment_manual_reviews_no_update" not in existing_triggers

    with engine.begin() as conn:
        event_id = _insert_gst_event(conn, reason="Existing event guard")
        evidence_id = conn.execute(
            text(
                "INSERT INTO gst_adjustment_evidence "
                "(event_id, evidence_type, evidence_reference) "
                "VALUES (:event_id, 'document', 'synthetic://partial') RETURNING id"
            ),
            {"event_id": event_id},
        ).scalar_one()
        for statement in (
            f"UPDATE gst_adjustment_events SET id=id WHERE id={event_id}",
            f"DELETE FROM gst_adjustment_events WHERE id={event_id}",
            f"UPDATE gst_adjustment_evidence SET id=id WHERE id={evidence_id}",
            f"DELETE FROM gst_adjustment_evidence WHERE id={evidence_id}",
        ):
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(statement))

    CompanyBase.metadata.create_all(engine)
    second = run_company_migrations(engine)
    assert "guards:gst_adjustment_append_only" in second
    with engine.connect() as conn:
        all_triggers = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
    for table in (
        "gst_adjustment_events",
        "gst_adjustment_tax_slices",
        "gst_adjustment_finalizations",
        "gst_adjustment_evidence",
        "gst_adjustment_manual_reviews",
    ):
        assert f"trg_{table}_no_update" in all_triggers
        assert f"trg_{table}_no_delete" in all_triggers
        assert f"trg_{table}_no_replace_insert" in all_triggers
    assert "trg_gst_adjustment_tax_slices_domain" in all_triggers
    assert "trg_gst_adjustment_manual_reviews_domain" in all_triggers
    assert "trg_gst_adjustment_finalizations_validate" in all_triggers

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_tax_slices "
                "(event_id, tax_code, amount_cents, gst_amount_cents) "
                "VALUES (1, 'standard', 1000, 100)"
            )
        )
        _finalize_gst_event(conn, 1)
        pending_event_id = _insert_gst_event(conn, manual_review_status="pending")
        review_id = conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reason) VALUES (:event_id, 'pending', 'Synthetic pending') "
                "RETURNING id"
            ),
            {"event_id": pending_event_id},
        ).scalar_one()
        for statement in (
            "UPDATE gst_adjustment_tax_slices SET id=id WHERE id=1",
            "DELETE FROM gst_adjustment_tax_slices WHERE id=1",
            "UPDATE gst_adjustment_finalizations SET amount_cents=amount_cents WHERE event_id=1",
            "DELETE FROM gst_adjustment_finalizations WHERE event_id=1",
            f"UPDATE gst_adjustment_manual_reviews SET id=id WHERE id={review_id}",
            f"DELETE FROM gst_adjustment_manual_reviews WHERE id={review_id}",
        ):
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text(statement))

    assert detect_drift(engine, CompanyBase, "gst-adjustment-partial").is_clean

    assert "guards:gst_adjustment_append_only" not in run_company_migrations(engine)


def test_gst_adjustment_event_evidence_and_review_domain_constraints(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-domain.db")
    run_company_migrations(engine)

    invalid_events = (
        {"event_type": "   "},
        {"event_type": "not_supported"},
        {"source_direction": ""},
        {"adjustment_direction": "sideways"},
        {"projection_box": "1C"},
        {"tax_code": "   "},
        {"tax_code": "unknown"},
        {"policy_version": " "},
        {"policy_version": "F-02-v2"},
        {"reason": "\t"},
        {"amount": "10.001"},
        {"gst_amount": "1.001"},
        {"source_record_type": " ", "source_record_id": 5},
        {"source_record_type": "credit_note", "source_record_id": 0},
        {"source_record_type": "invoice", "source_record_id": 1},
        {"source_record_type": "credit_note", "source_record_id": ""},
        {"source_record_type": "credit_note", "source_record_id": None},
        {"source_record_type": None, "source_record_id": 5},
        {"adjustment_note_reference": "CN-1"},
        {"adjustment_note_held_date": "2026-10-02"},
        {"adjustment_note_reference": " ", "adjustment_note_held_date": "2026-10-02"},
        {"effective_date": "not-a-date"},
        {"awareness_date": "2026-09-30"},
        {"agreement_date": "2026-09-30"},
        {"refund_repayment_date": "2026-09-30"},
        {
            "adjustment_note_reference": "CN-1",
            "adjustment_note_held_date": "2026-09-30",
        },
        {"manual_review_status": "waiting"},
        {"tax_code": "gst_free", "gst_amount": 1},
        {"tax_code": "mixed", "tax_slice_count": 1},
        {"tax_slice_count": 0},
    )
    with engine.begin() as conn:
        for overrides in invalid_events:
            try:
                _insert_gst_event(conn, **overrides)
            except Exception:
                continue
            pytest.fail(f"invalid GST event values were accepted: {overrides!r}")

        event_id = _insert_gst_event(conn, manual_review_status="pending")
        mixed_event_id = _insert_gst_event(
            conn, tax_code="mixed", tax_slice_count=2
        )
        invalid_slices = (
            {"tax_code": " ", "amount": 1, "gst_amount": 0},
            {"tax_code": "unknown", "amount": 1, "gst_amount": 0},
            {"tax_code": "standard", "amount": "1.001", "gst_amount": 0},
            {"tax_code": "standard", "amount": 1, "gst_amount": "0.001"},
            {"tax_code": "gst_free", "amount": 1, "gst_amount": 1},
            {"tax_code": "standard", "amount": 0, "gst_amount": 0},
        )
        for values in invalid_slices:
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO gst_adjustment_tax_slices "
                        "(event_id, tax_code, amount_cents, gst_amount_cents) "
                        "VALUES (:event_id, :tax_code, :amount, :gst_amount)"
                    ),
                    {"event_id": mixed_event_id, **values},
                )

        invalid_evidence = (
            {"evidence_type": " ", "evidence_reference": "synthetic://x"},
            {"evidence_type": "spreadsheet", "evidence_reference": "synthetic://x"},
            {"evidence_type": "document", "evidence_reference": "  "},
            {
                "evidence_type": "document",
                "evidence_reference": "synthetic://x",
                "content_sha256": "a" * 63,
            },
            {
                "evidence_type": "document",
                "evidence_reference": "synthetic://x",
                "content_sha256": "g" * 64,
            },
        )
        for values in invalid_evidence:
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO gst_adjustment_evidence "
                        "(event_id, evidence_type, evidence_reference, content_sha256) "
                        "VALUES (:event_id, :evidence_type, :evidence_reference, :content_sha256)"
                    ),
                    {"event_id": event_id, "content_sha256": None, **values},
                )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_evidence "
                "(event_id, evidence_type, evidence_reference, content_sha256) "
                "VALUES (:event_id, 'document', 'synthetic://valid-hash', :digest)"
            ),
            {"event_id": event_id, "digest": "A" * 64},
        )

        invalid_reviews = (
            {"status": "", "reason": "reviewed"},
            {"status": "unknown", "reason": "reviewed"},
            {"status": "pending", "reason": "  "},
            {"status": "pending", "reason": None},
            {"status": "pending", "reason": "reviewed", "reviewer": " "},
            {"status": "pending", "reason": "reviewed", "reviewed_at": "2026-10-02"},
            {"status": "approved", "reason": "reviewed"},
            {"status": "approved", "reason": "reviewed", "reviewed_at": ""},
            {"status": "approved", "reason": "reviewed", "reviewed_at": "not-a-date"},
            {
                "status": "approved",
                "reason": "reviewed",
                "reviewed_at": "2026-02-30 10:10:10",
            },
            {
                "status": "approved",
                "reason": "reviewed",
                "reviewed_at": "2026-1-2 1:2:3",
            },
            {
                "status": "approved",
                "reason": "reviewed",
                "reviewed_at": "2026-10-02T10:10:10Z",
            },
        )
        for values in invalid_reviews:
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO gst_adjustment_manual_reviews "
                        "(event_id, status, reason, reviewer, reviewed_at) "
                        "VALUES (:event_id, :status, :reason, :reviewer, :reviewed_at)"
                    ),
                    {
                        "event_id": event_id,
                        "reviewer": None,
                        "reviewed_at": None,
                        **values,
                    },
                )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reason, reviewer, reviewed_at) "
                "VALUES (:event_id, 'approved', 'Synthetic approval', 'Reviewer', "
                "'2026-10-02 10:10:10')"
            ),
            {"event_id": event_id},
        )


def test_gst_adjustment_money_boundary_is_exact_and_two_decimal(tmp_path):
    accepted = (
        ("1", 100),
        ("1.2", 120),
        ("1.20", 120),
        ("999999.99", 99_999_999),
        ("9999999999999.99", 999_999_999_999_999),
    )
    for value, expected_cents in accepted:
        cents = gst_adjustment_money_to_cents(Decimal(value))
        assert cents == expected_cents
        assert _company_models.gst_adjustment_cents_to_money(cents) == Decimal(value).quantize(
            Decimal("0.01")
        )

    rejected = (
        "1.001",
        "1.005",
        "1.009",
        "0.001",
        "10.999",
        "1e2",
        "1E+2",
        "1e-2",
        "NaN",
        "Infinity",
        "-Infinity",
        "-1.00",
        "10000000000000.00",
    )
    for value in rejected:
        with pytest.raises(ValueError):
            gst_adjustment_money_to_cents(value)
    with pytest.raises(ValueError):
        gst_adjustment_money_to_cents(1.2)

    engine = _engine(tmp_path / "gst-adjustment-money-cents.db")
    run_company_migrations(engine)
    with engine.begin() as conn:
        for value, expected_cents in accepted:
            event_id = _insert_gst_event(
                conn,
                amount=value,
                gst_amount="0.00",
                tax_code="gst_free",
            )
            stored_cents = conn.execute(
                text("SELECT amount_cents FROM gst_adjustment_events WHERE id=:id"),
                {"id": event_id},
            ).scalar_one()
            assert stored_cents == expected_cents
            assert conn.execute(
                text(
                    "SELECT typeof(amount_cents) FROM gst_adjustment_events WHERE id=:id"
                ),
                {"id": event_id},
            ).scalar_one() == "integer"

        invalid_cent_amounts = (0, 1.001, -1, 1_000_000_000_000_000)
        for invalid_amount_cents in invalid_cent_amounts:
            with pytest.raises(Exception, match="Invalid GST adjustment event"):
                conn.execute(
                    text(
                        "INSERT INTO gst_adjustment_events ("
                        "event_type, source_direction, adjustment_direction, projection_box, "
                        "amount_cents, gst_amount_cents, tax_code, policy_version, reason, "
                        "effective_date, tax_slice_count"
                        ") VALUES ('agreement', 'AR', 'increasing', '1A', :amount, 0, "
                        "'gst_free', 'F-02-v1', 'Synthetic invalid cents', '2026-10-01', 1)"
                    ),
                    {"amount": invalid_amount_cents},
                )
    engine.dispose()


def test_gst_adjustment_provenance_is_mandatory_in_sql_and_orm(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-provenance.db")
    run_company_migrations(engine)
    invalid_provenance = (
        {"source_record_type": None, "source_record_id": None},
        {"source_record_type": "credit_note", "source_record_id": None},
        {"source_record_type": None, "source_record_id": 7},
        {"source_record_type": " ", "source_record_id": 7},
        {"source_record_type": "credit_note", "source_record_id": " "},
        {"source_record_type": "invoice", "source_record_id": 7},
    )

    with engine.begin() as conn:
        for provenance in invalid_provenance:
            with pytest.raises(Exception, match="Invalid GST adjustment event"):
                _insert_gst_event(conn, **provenance)

        accepted_event_id = _insert_gst_event(
            conn, source_record_type="credit_note", source_record_id=101
        )
        assert accepted_event_id > 0

        original_id = _insert_gst_event(
            conn, source_record_type="credit_note", source_record_id=102
        )
        _insert_gst_slice(conn, original_id, "standard", "10.00", "1.00")
        _finalize_gst_event(conn, original_id)

        with pytest.raises(Exception):
            _insert_gst_event(
                conn,
                event_type="reversal",
                adjustment_direction="decreasing",
                projection_box="1B",
                reversal_of_event_id=original_id,
                source_record_type=None,
                source_record_id=None,
            )

        conn.execute(text("PRAGMA ignore_check_constraints=ON"))
        conn.execute(text("DROP TRIGGER trg_gst_adjustment_events_domain"))
        source_less_legacy_id = _insert_gst_event(
            conn, source_record_type=None, source_record_id=None
        )
        conn.execute(text("PRAGMA ignore_check_constraints=OFF"))
        _insert_gst_slice(conn, source_less_legacy_id, "standard", "10.00", "1.00")
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, source_less_legacy_id)

    with engine.begin() as conn:
        matching_original_id = _insert_gst_event(
            conn, source_record_type="credit_note", source_record_id=104
        )
        _insert_gst_slice(conn, matching_original_id, "standard", "10.00", "1.00")
        _finalize_gst_event(conn, matching_original_id)

    for source_type, source_id in (("invoice", 104), ("credit_note", 105)):
        with Session(engine) as session:
            session.add(
                _orm_gst_event(
                    event_type="reversal",
                    adjustment_direction="decreasing",
                    projection_box="1B",
                    source_record_type=source_type,
                    source_record_id=source_id,
                    reversal_of_event_id=matching_original_id,
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()

    with Session(engine) as session:
        matching_reversal = _orm_gst_event(
            event_type="reversal",
            adjustment_direction="decreasing",
            projection_box="1B",
            source_record_type="credit_note",
            source_record_id=104,
            reversal_of_event_id=matching_original_id,
        )
        session.add(matching_reversal)
        session.flush()
        assert matching_reversal.id is not None

    valid_orm_event = _orm_gst_event(
        source_record_type="credit_note", source_record_id=103
    )
    with Session(engine) as session:
        session.add(valid_orm_event)
        session.flush()
        assert valid_orm_event.id is not None

    for provenance in invalid_provenance:
        with Session(engine) as session:
            session.add(_orm_gst_event(**provenance))
            with pytest.raises(IntegrityError):
                session.flush()

    engine.dispose()


def test_gst_adjustment_event_magnitudes_are_positive_for_sql_and_orm(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-positive-magnitudes.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        original_id = _insert_gst_event(conn)
        _insert_gst_slice(conn, original_id, "standard", "10.00", "1.00")
        _finalize_gst_event(conn, original_id)

        zero_amount_events = (
            _valid_gst_event(amount="0.00", gst_amount="0.00", tax_code="gst_free"),
            _valid_gst_event(
                event_type="reversal",
                adjustment_direction="decreasing",
                projection_box="1B",
                amount="0.00",
                gst_amount="0.00",
                tax_code="gst_free",
                reversal_of_event_id=original_id,
            ),
        )
        for values in zero_amount_events:
            with pytest.raises(IntegrityError):
                conn.execute(_INSERT_GST_EVENT, values)

    for values in zero_amount_events:
        orm_values = dict(values)
        for field in (
            "effective_date",
            "awareness_date",
            "agreement_date",
            "refund_repayment_date",
            "adjustment_note_held_date",
        ):
            if orm_values[field] is not None:
                orm_values[field] = date.fromisoformat(orm_values[field])
        with Session(engine) as session:
            session.add(_company_models.GSTAdjustmentEvent(**orm_values))
            with pytest.raises(IntegrityError):
                session.flush()

    engine.dispose()


def test_gst_adjustment_zero_gst_requires_valid_classification_and_reconciliation(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-zero-gst-classifications.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        for tax_code in ("gst_free", "input_taxed", "none"):
            event_id = _insert_gst_event(
                conn, amount="10.00", gst_amount="0.00", tax_code=tax_code
            )
            _insert_gst_slice(conn, event_id, tax_code, "10.00", "0.00")
            assert _finalize_gst_event(conn, event_id) == event_id

        mixed_event_id = _insert_gst_event(
            conn,
            amount="20.00",
            gst_amount="0.00",
            tax_code="mixed",
            tax_slice_count=2,
        )
        _insert_gst_slice(conn, mixed_event_id, "gst_free", "10.00", "0.00")
        _insert_gst_slice(conn, mixed_event_id, "none", "10.00", "0.00")
        assert _finalize_gst_event(conn, mixed_event_id) == mixed_event_id

        unreconciled_mixed_id = _insert_gst_event(
            conn,
            amount="20.00",
            gst_amount="0.00",
            tax_code="mixed",
            tax_slice_count=2,
        )
        _insert_gst_slice(conn, unreconciled_mixed_id, "gst_free", "10.00", "0.00")
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, unreconciled_mixed_id)

    engine.dispose()


def test_gst_adjustment_strict_calendar_dates_and_review_timestamps(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-strict-dates.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER trg_gst_adjustment_events_domain"))
        conn.execute(
            text(
                "CREATE TRIGGER trg_gst_adjustment_events_domain "
                "BEFORE INSERT ON gst_adjustment_events "
                "WHEN julianday(NEW.effective_date) IS NULL "
                "BEGIN SELECT RAISE(ABORT, 'legacy date check'); END"
            )
        )
    assert "guards:gst_adjustment_append_only" in run_company_migrations(engine)

    impossible_dates = (
        "2026-02-29",
        "2026-02-30",
        "2026-04-31",
        "2026-13-01",
        "2026-00-10",
    )
    date_fields = (
        "effective_date",
        "awareness_date",
        "agreement_date",
        "refund_repayment_date",
        "adjustment_note_held_date",
    )
    with engine.begin() as conn:
        for field in date_fields:
            for invalid_date in (*impossible_dates, ""):
                values = {"effective_date": "2025-01-01", field: invalid_date}
                if field == "adjustment_note_held_date":
                    values["adjustment_note_reference"] = "CN-SYNTHETIC"
                with pytest.raises(Exception, match="Invalid GST adjustment event"):
                    _insert_gst_event(conn, **values)

        leap_day_values = {
            "effective_date": "2028-02-29",
            "awareness_date": "2028-02-29",
            "agreement_date": "2028-02-29",
            "refund_repayment_date": "2028-02-29",
            "adjustment_note_reference": "CN-LEAP",
            "adjustment_note_held_date": "2028-02-29",
            "manual_review_status": "pending",
        }
        leap_event_id = _insert_gst_event(conn, **leap_day_values)

        impossible_timestamps = (
            "2026-02-29 10:10:10",
            "2026-02-30 10:10:10",
            "2026-04-31 10:10:10",
            "2026-13-01 10:10:10",
            "2026-00-10 10:10:10",
            "2026-10-02 24:00:00",
            "2026-10-02 23:60:00",
            "2026-10-02 23:59:60",
            "",
        )
        for reviewed_at in impossible_timestamps:
            with pytest.raises(Exception, match="Invalid GST adjustment manual review"):
                conn.execute(
                    text(
                        "INSERT INTO gst_adjustment_manual_reviews "
                        "(event_id, status, reason, reviewed_at) "
                        "VALUES (:event_id, 'approved', 'Synthetic timestamp test', :reviewed_at)"
                    ),
                    {"event_id": leap_event_id, "reviewed_at": reviewed_at},
                )
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reason, reviewed_at) "
                "VALUES (:event_id, 'approved', 'Valid leap-day timestamp', "
                "'2028-02-29 23:59:59')"
            ),
            {"event_id": leap_event_id},
        )


def test_gst_adjustment_tax_slices_reconcile_exactly_to_parent(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-slice-reconciliation.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        gross_mismatch = _insert_gst_event(
            conn, tax_code="mixed", amount=110, gst_amount=10, tax_slice_count=2
        )
        gst_mismatch = _insert_gst_event(
            conn, tax_code="mixed", amount=110, gst_amount=10, tax_slice_count=2
        )
        for event_id, gst_amount, final_amount in (
            (gross_mismatch, 10, 9),
            (gst_mismatch, 9, 10),
        ):
            _insert_gst_slice(conn, event_id, "standard", "100.00", f"{gst_amount}.00")
            _insert_gst_slice(conn, event_id, "gst_free", f"{final_amount}.00", "0.00")
            with pytest.raises(Exception, match="incomplete or unreconciled"):
                _finalize_gst_event(conn, event_id)


def test_gst_adjustment_finalization_requires_complete_reconciled_slices(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-finalization.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        zero_slice_event = _insert_gst_event(conn)
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, zero_slice_event)

        too_few_event = _insert_gst_event(
            conn, tax_code="mixed", amount=110, gst_amount=10, tax_slice_count=2
        )
        _insert_gst_slice(conn, too_few_event, "standard", "100.00", "10.00")
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, too_few_event)

        too_many_event = _insert_gst_event(
            conn, tax_code="mixed", amount=110, gst_amount=10, tax_slice_count=2
        )
        for tax_code, amount, gst_amount in (
            ("standard", 100, 10),
            ("gst_free", 10, 0),
        ):
            _insert_gst_slice(
                conn, too_many_event, tax_code, f"{amount}.00", f"{gst_amount}.00"
            )
        with pytest.raises(Exception, match="Invalid or unreconciled"):
            conn.execute(
                text(
                    "INSERT INTO gst_adjustment_tax_slices "
                    "(event_id, tax_code, amount_cents, gst_amount_cents) "
                    "VALUES (:event_id, 'capital', 1, 0)"
                ),
                {"event_id": too_many_event},
            )

        mismatched_event = _insert_gst_event(
            conn, tax_code="mixed", amount=110, gst_amount=10, tax_slice_count=2
        )
        for tax_code, amount, gst_amount in (
            ("standard", 100, 9),
            ("gst_free", 10, 0),
        ):
            _insert_gst_slice(
                conn, mismatched_event, tax_code, f"{amount}.00", f"{gst_amount}.00"
            )
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, mismatched_event)

        complete_event = _insert_gst_event(
            conn, tax_code="mixed", amount=110, gst_amount=10, tax_slice_count=2
        )
        for tax_code, amount, gst_amount in (
            ("standard", 100, 10),
            ("gst_free", 10, 0),
        ):
            _insert_gst_slice(
                conn, complete_event, tax_code, f"{amount}.00", f"{gst_amount}.00"
            )

        assert _finalize_gst_event(conn, complete_event) == complete_event
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM gst_adjustment_finalizations "
                "WHERE event_id=:event_id"
            ),
            {"event_id": complete_event},
        ).scalar_one() == 1
        with pytest.raises(Exception):
            _finalize_gst_event(conn, complete_event)


def test_gst_adjustment_finalization_requires_eligible_review_state(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-review-finalization.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        for event_status in ("not_required", "pending", "approved", "rejected"):
            for review_status in (None, "pending", "approved", "rejected"):
                event_id = _insert_gst_event(conn, manual_review_status=event_status)
                _insert_gst_slice(conn, event_id, "standard", "10.00", "1.00")
                should_accept_review = event_status == "pending" and review_status is not None
                if review_status is not None:
                    reviewed_at = (
                        None if review_status == "pending" else "2026-10-02 10:00:00"
                    )
                    statement = text(
                        "INSERT INTO gst_adjustment_manual_reviews "
                        "(event_id, status, reviewed_at, reason) "
                        "VALUES (:event_id, :status, :reviewed_at, 'Synthetic decision')"
                    )
                    if should_accept_review:
                        conn.execute(
                            statement,
                            {
                                "event_id": event_id,
                                "status": review_status,
                                "reviewed_at": reviewed_at,
                            },
                        )
                    else:
                        with pytest.raises(
                            Exception, match="Invalid GST adjustment manual review"
                        ):
                            conn.execute(
                                statement,
                                {
                                    "event_id": event_id,
                                    "status": review_status,
                                    "reviewed_at": reviewed_at,
                                },
                            )

                should_finalize = (
                    event_status == "not_required"
                    or (event_status == "pending" and review_status == "approved")
                )
                if should_finalize:
                    assert _finalize_gst_event(conn, event_id) == event_id
                else:
                    with pytest.raises(Exception, match="incomplete or unreconciled"):
                        _finalize_gst_event(conn, event_id)

        history_event_id = _insert_gst_event(conn, manual_review_status="pending")
        _insert_gst_slice(conn, history_event_id, "standard", "10.00", "1.00")
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reason) "
                "VALUES (:event_id, 'pending', 'Synthetic pending state')"
            ),
            {"event_id": history_event_id},
        )
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, history_event_id)
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reviewed_at, reason) "
                "VALUES (:event_id, 'approved', '2026-10-02 10:00:00', "
                "'Synthetic approved state')"
            ),
            {"event_id": history_event_id},
        )
        assert _finalize_gst_event(conn, history_event_id) == history_event_id

        legacy_history_event_id = _insert_gst_event(conn, manual_review_status="pending")
        _insert_gst_slice(conn, legacy_history_event_id, "standard", "10.00", "1.00")
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reviewed_at, reason) "
                "VALUES (:event_id, 'rejected', '2026-10-02 10:00:00', "
                "'Synthetic historical rejection')"
            ),
            {"event_id": legacy_history_event_id},
        )
        conn.execute(text("DROP TRIGGER trg_gst_adjustment_manual_reviews_domain"))
        conn.execute(
            text(
                "INSERT INTO gst_adjustment_manual_reviews "
                "(event_id, status, reviewed_at, reason) "
                "VALUES (:event_id, 'approved', '2026-10-03 10:00:00', "
                "'Synthetic latest approval')"
            ),
            {"event_id": legacy_history_event_id},
        )
        assert _finalize_gst_event(conn, legacy_history_event_id) == legacy_history_event_id

    engine.dispose()


def test_gst_adjustment_reversals_are_full_single_and_linked(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-reversals.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        original_id = _insert_gst_event(
            conn,
            amount=110,
            gst_amount=10,
            tax_code="mixed",
            tax_slice_count=2,
            source_record_type="credit_note",
            source_record_id=42,
        )
        _insert_gst_slice(conn, original_id, "standard", "100.00", "10.00")
        _insert_gst_slice(conn, original_id, "gst_free", "10.00", "0.00")
        _finalize_gst_event(conn, original_id)

        with pytest.raises(Exception, match="full opposite"):
            _insert_gst_event(
                conn,
                event_type="reversal",
                amount=55,
                gst_amount=5,
                tax_code="mixed",
                tax_slice_count=2,
                adjustment_direction="decreasing",
                projection_box="1B",
                source_record_type="credit_note",
                source_record_id=42,
                reversal_of_event_id=original_id,
            )
        with pytest.raises(Exception, match="full opposite"):
            _insert_gst_event(
                conn,
                event_type="reversal",
                amount=110,
                gst_amount=10,
                tax_code="mixed",
                tax_slice_count=2,
                adjustment_direction="decreasing",
                projection_box="1B",
                source_record_type="invoice",
                source_record_id=42,
                reversal_of_event_id=original_id,
            )
        with pytest.raises(Exception, match="full opposite"):
            _insert_gst_event(
                conn,
                event_type="reversal",
                source_direction="AP",
                amount=110,
                gst_amount=10,
                tax_code="mixed",
                tax_slice_count=2,
                adjustment_direction="decreasing",
                projection_box="1B",
                source_record_type="credit_note",
                source_record_id=42,
                reversal_of_event_id=original_id,
            )
        with pytest.raises(Exception, match="full opposite"):
            _insert_gst_event(
                conn,
                event_type="reversal",
                amount=110,
                gst_amount=10,
                tax_code="mixed",
                tax_slice_count=2,
                adjustment_direction="decreasing",
                projection_box="1B",
                source_record_type="credit_note",
                source_record_id=43,
                reversal_of_event_id=original_id,
            )

        reversal_id = _insert_gst_event(
            conn,
            event_type="reversal",
            amount=110,
            gst_amount=10,
            tax_code="mixed",
            tax_slice_count=2,
            adjustment_direction="decreasing",
            projection_box="1B",
            effective_date="2026-10-02",
            source_record_type="credit_note",
            source_record_id=42,
            reversal_of_event_id=original_id,
        )
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, reversal_id)

        _insert_gst_slice(conn, reversal_id, "standard", "100.00", "10.00")
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, reversal_id)

        _insert_gst_slice(conn, reversal_id, "gst_free", "10.00", "0.00")
        _finalize_gst_event(conn, reversal_id)

        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO gst_adjustment_finalizations "
                    "(event_id, tax_slice_count, amount_cents, gst_amount_cents) "
                    "VALUES (:event_id, 2, 11000, 1000)"
                ),
                {"event_id": reversal_id},
            )

        with pytest.raises(Exception):
            _insert_gst_event(
                conn,
                event_type="reversal",
                amount=110,
                gst_amount=10,
                tax_code="mixed",
                tax_slice_count=2,
                adjustment_direction="decreasing",
                projection_box="1B",
                effective_date="2026-10-03",
                source_record_type="credit_note",
                source_record_id=42,
                reversal_of_event_id=original_id,
            )
        with pytest.raises(Exception, match="full opposite"):
            _insert_gst_event(
                conn,
                event_type="reversal",
                amount=110,
                gst_amount=10,
                tax_code="mixed",
                tax_slice_count=2,
                effective_date="2026-10-03",
                reversal_of_event_id=reversal_id,
            )

        original = conn.execute(
            text(
                "SELECT amount_cents, gst_amount_cents, policy_version, reversal_of_event_id "
                "FROM gst_adjustment_events WHERE id=:event_id"
            ),
            {"event_id": original_id},
        ).one()
        assert original == (11000, 1000, "F-02-v1", None)


def test_gst_adjustment_ap_void_same_box_reversal_and_journal_provenance(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-ap-void-same-box.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        _seed_ap_credit_note(conn)
        original_id, posting_journal_id = _insert_ap_original(conn)
        reversal_id, void_journal_id = _insert_ap_void_reversal(
            conn, original_id, posting_journal_id
        )

        assert conn.execute(
            text(
                "SELECT event_type, projection_box, lifecycle_operation_type, "
                "lifecycle_operation_id, reversal_of_event_id "
                "FROM gst_adjustment_events WHERE id IN (:original, :reversal) "
                "ORDER BY id"
            ),
            {"original": original_id, "reversal": reversal_id},
        ).all() == [
            ("agreement", "1A", "credit_note_ap", posting_journal_id, None),
            ("reversal", "1A", "credit_note_void_ap", void_journal_id, original_id),
        ]
        assert conn.execute(
            text("SELECT COUNT(*) FROM gst_adjustment_finalizations")
        ).scalar_one() == 2

        for journal_id in (posting_journal_id, void_journal_id):
            for column, value in (
                ("id", journal_id + 1000),
                ("source_type", "manual"),
                ("source_id", 99),
                ("entry_date", "2026-10-03"),
                (
                    "reverses_entry_id",
                    void_journal_id if journal_id == posting_journal_id else None,
                ),
            ):
                with pytest.raises(Exception, match="provenance is immutable"):
                    conn.execute(
                        text(f"UPDATE journal_entries SET {column}=:value WHERE id=:id"),
                        {"value": value, "id": journal_id},
                    )
            with pytest.raises(Exception, match="provenance is immutable"):
                conn.execute(
                    text("DELETE FROM journal_entries WHERE id=:id"),
                    {"id": journal_id},
                )
            journal = conn.execute(
                text(
                    "SELECT entry_date, source_type, source_id, reverses_entry_id "
                    "FROM journal_entries WHERE id=:id"
                ),
                {"id": journal_id},
            ).one()
            with pytest.raises(Exception, match="provenance is immutable"):
                conn.execute(
                    text(
                        "INSERT OR REPLACE INTO journal_entries "
                        "(id, entry_date, memo, source_type, source_id, reverses_entry_id) "
                        "VALUES (:id, :entry_date, 'Synthetic replacement', :source_type, "
                        ":source_id, :reverses_entry_id)"
                    ),
                    {"id": journal_id, **journal._mapping},
                )
        with pytest.raises(Exception, match="provenance is immutable"):
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO journal_entries "
                    "(entry_date, memo, source_type, source_id) "
                    "VALUES ('2026-10-03', 'Synthetic replacement', "
                    "'credit_note_ap', 42)"
                )
            )

        manual_journal_id = _insert_journal_entry(
            conn, "manual", None, entry_date="2026-10-01"
        )
        conn.execute(
            text(
                "UPDATE journal_entries SET id=id + 1000, source_id=99, "
                "entry_date='2026-10-03' "
                "WHERE id=:id"
            ),
            {"id": manual_journal_id},
        )
        conn.execute(
            text("DELETE FROM journal_entries WHERE id=:id"),
            {"id": manual_journal_id + 1000},
        )

    engine.dispose()


def test_gst_adjustment_journal_provenance_trigger_repair_is_idempotent(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-journal-guard-repair.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        _seed_ap_credit_note(conn)
        _original_id, posting_journal_id = _insert_ap_original(conn)
        conn.execute(
            text(
                "DROP TRIGGER trg_journal_entries_gst_adjustment_no_provenance_update"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER trg_journal_entries_gst_adjustment_no_provenance_update "
                "BEFORE UPDATE OF source_type, source_id, reverses_entry_id, entry_date "
                "ON journal_entries WHEN EXISTS ("
                "SELECT 1 FROM gst_adjustment_events AS event "
                "JOIN gst_adjustment_finalizations AS finalization "
                "ON finalization.event_id=event.id "
                "WHERE event.lifecycle_operation_id=OLD.id "
                "AND event.lifecycle_operation_type=OLD.source_type "
                "AND event.source_record_id=OLD.source_id) "
                "AND (OLD.source_type IS NOT NEW.source_type "
                "OR OLD.source_id IS NOT NEW.source_id "
                "OR OLD.reverses_entry_id IS NOT NEW.reverses_entry_id "
                "OR OLD.entry_date IS NOT NEW.entry_date) "
                "BEGIN SELECT RAISE(ABORT, 'legacy journal provenance guard'); END"
            )
        )

    repaired = run_company_migrations(engine)
    assert "guards:gst_adjustment_append_only" in repaired
    assert "guards:gst_adjustment_append_only" not in run_company_migrations(engine)
    with engine.begin() as conn:
        with pytest.raises(Exception, match="provenance is immutable"):
            conn.execute(
                text("UPDATE journal_entries SET id=id + 1000 WHERE id=:id"),
                {"id": posting_journal_id},
            )
        with pytest.raises(Exception, match="provenance is immutable"):
            conn.execute(
                text("UPDATE journal_entries SET source_id=99 WHERE id=:id"),
                {"id": posting_journal_id},
            )

    engine.dispose()


def test_gst_adjustment_lifecycle_operation_domain_and_journal_binding(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-lifecycle-operation-domain.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        invalid_operations = (
            {"lifecycle_operation_type": "credit_note_ap", "lifecycle_operation_id": None},
            {"lifecycle_operation_type": None, "lifecycle_operation_id": 1},
            {"lifecycle_operation_type": "manual", "lifecycle_operation_id": 1},
            {"lifecycle_operation_type": "credit_note_void_ap", "lifecycle_operation_id": 0},
            {"lifecycle_operation_type": "credit_note_void_ap", "lifecycle_operation_id": -1},
            {"lifecycle_operation_type": "credit_note_void_ap", "lifecycle_operation_id": 1.5},
        )
        for operation in invalid_operations:
            with pytest.raises(Exception, match="Invalid GST adjustment event"):
                _insert_gst_event(conn, **operation)

        legacy_original = _insert_gst_event(
            conn,
            source_direction="AP",
            projection_box="1A",
            lifecycle_operation_type=None,
            lifecycle_operation_id=None,
        )
        _insert_gst_slice(conn, legacy_original, "standard", "10.00", "1.00")
        _finalize_gst_event(conn, legacy_original)

        for index, invalid_journal in enumerate(
            (
                {"journal_source_type": "manual"},
                {"journal_source_id": 902},
                {"journal_entry_date": "2026-10-02"},
            ),
            start=100,
        ):
            with pytest.raises(Exception, match="incomplete or unreconciled"):
                with conn.begin_nested():
                    _insert_ap_original(
                        conn, source_record_id=index, **invalid_journal
                    )

        duplicate_source_journal_id = _insert_journal_entry(conn, "credit_note_ap", 103)
        with pytest.raises(Exception, match="UNIQUE"):
            _insert_journal_entry(conn, "credit_note_ap", 103)

        original_id, posting_journal_id = _insert_ap_original(conn)
        unrelated_journal_id = _insert_journal_entry(
            conn, "manual", None, entry_date="2026-10-01"
        )
        wrong_void_cases = (
            {"journal_source_type": "manual"},
            {"journal_source_id": 904},
            {"journal_reverses_entry_id": unrelated_journal_id},
            {"journal_date": "2026-10-03"},
            {"operation_type": "credit_note_ap"},
        )
        for invalid_void in wrong_void_cases:
            with pytest.raises(Exception):
                with conn.begin_nested():
                    _insert_ap_void_reversal(
                        conn,
                        original_id,
                        posting_journal_id,
                        projection_box="1B",
                        **invalid_void,
                    )

        with pytest.raises(Exception, match="full opposite"):
            _insert_ap_void_reversal(
                conn,
                legacy_original,
                1,
                operation_type=None,
                projection_box="1A",
            )

    engine.dispose()


@pytest.mark.parametrize(
    "case",
    (
        "legacy",
        "ar",
        "application",
        "refund",
        "ordinary",
        "arbitrary_operation",
        "nonfinalized",
        "already_reversed",
        "reversal_original",
    ),
)
def test_gst_adjustment_same_box_void_requires_eligible_ap_credit_state(tmp_path, case):
    engine = _engine(tmp_path / f"gst-adjustment-ap-void-ineligible-{case}.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        _seed_ap_credit_note(conn)
        reversal_box = "1A"
        if case == "legacy":
            original_id = _insert_gst_event(
                conn,
                source_direction="AP",
                projection_box="1A",
                lifecycle_operation_type=None,
                lifecycle_operation_id=None,
            )
            _insert_gst_slice(conn, original_id, "standard", "10.00", "1.00")
            _finalize_gst_event(conn, original_id)
            original_journal_id = _insert_journal_entry(conn, "manual", None)
        elif case == "ar":
            original_id, original_journal_id = _insert_ap_original(
                conn, source_direction="AR", operation_type=None
            )
        elif case == "nonfinalized":
            original_id, original_journal_id = _insert_ap_original(conn, finalize=False)
        elif case == "reversal_original":
            base_id, original_journal_id = _insert_ap_original(
                conn, operation_type=None
            )
            original_id = _insert_gst_event(
                conn,
                event_type="reversal",
                source_direction="AP",
                adjustment_direction="increasing",
                projection_box="1B",
                source_record_id=42,
                reversal_of_event_id=base_id,
                lifecycle_operation_type=None,
                lifecycle_operation_id=None,
            )
            _insert_gst_slice(conn, original_id, "standard", "10.00", "1.00")
            _finalize_gst_event(conn, original_id)
            reversal_box = "1B"
        else:
            original_id, original_journal_id = _insert_ap_original(
                conn, operation_type=None if case == "ordinary" else "credit_note_ap"
            )

        if case == "application":
            conn.execute(
                text(
                    "INSERT INTO credit_note_applications "
                    "(credit_note_id, invoice_id, amount, application_date, status) "
                    "VALUES (42, 1, 1, '2026-10-01', 'active')"
                )
            )
        elif case == "refund":
            conn.execute(
                text(
                    "INSERT INTO bank_accounts (id, name, opening_balance, is_active) "
                    "VALUES (1, 'Synthetic bank', 0, 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO bank_transactions "
                    "(id, bank_account_id, direction, amount, occurred_at, gst_amount, "
                    "tax_code, unapplied_amount) "
                    "VALUES (1, 1, 'out', 1, '2026-10-01', 0, 'none', 0)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO credit_note_refunds "
                    "(credit_note_id, bank_transaction_id, journal_entry_id, amount, "
                    "refund_date, status) VALUES (42, 1, :journal_id, 1, "
                    "'2026-10-01', 'active')"
                ),
                {"journal_id": original_journal_id},
            )
        elif case == "already_reversed":
            _insert_ap_void_reversal(conn, original_id, original_journal_id)

        operation_type = None if case in {"legacy", "ar", "ordinary"} else "credit_note_void_ap"
        if case == "arbitrary_operation":
            operation_type = "arbitrary"
        with pytest.raises(Exception):
            _insert_ap_void_reversal(
                conn,
                original_id,
                original_journal_id,
                projection_box=reversal_box,
                operation_type=operation_type,
                adjustment_direction=(
                    "decreasing" if case == "reversal_original" else "increasing"
                ),
            )

    engine.dispose()


@pytest.mark.parametrize(
    ("case", "overrides"),
    (
        ("amount", {"amount": "9.00"}),
        ("gst", {"gst_amount": "0.50"}),
        ("tax_code", {"tax_code": "capital"}),
        ("policy", {"policy_version": "F-02-v2"}),
        ("source_type", {"source_record_type": "invoice"}),
        ("source_id", {"source_record_id": 43}),
        ("source_direction", {"source_direction": "AR"}),
        ("adjustment_direction", {"adjustment_direction": "decreasing"}),
        ("effective_date", {"effective_date": "2026-09-30"}),
        ("operation_type", {"operation_type": "credit_note_ap"}),
        ("journal_type", {"journal_source_type": "manual"}),
        ("journal_source_id", {"journal_source_id": 904}),
        ("journal_target", {"journal_reverses_entry_id": "unrelated"}),
        ("journal_date", {"journal_date": "2026-10-03"}),
    ),
)
def test_gst_adjustment_ap_void_rejects_mismatched_reversal_provenance(
    tmp_path, case, overrides
):
    engine = _engine(tmp_path / f"gst-adjustment-ap-void-mismatch-{case}.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        _seed_ap_credit_note(conn)
        original_id, posting_journal_id = _insert_ap_original(conn)
        unrelated_journal_id = _insert_journal_entry(
            conn, "manual", None, entry_date="2026-10-01"
        )
        if overrides.get("journal_reverses_entry_id") == "unrelated":
            overrides = {**overrides, "journal_reverses_entry_id": unrelated_journal_id}

        with pytest.raises(Exception):
            with conn.begin_nested():
                _insert_ap_void_reversal(
                    conn,
                    original_id,
                    posting_journal_id,
                    projection_box="1B",
                    **overrides,
                )

    engine.dispose()


def test_gst_adjustment_ap_void_reversal_requires_exact_complete_slices(tmp_path):
    engine = _engine(tmp_path / "gst-adjustment-ap-void-slices.db")
    run_company_migrations(engine)

    with engine.begin() as conn:
        _seed_ap_credit_note(conn)
        original_id, posting_journal_id = _insert_ap_original(conn)

        with pytest.raises(Exception, match="Invalid or unreconciled"):
            with conn.begin_nested():
                _insert_ap_void_reversal(
                    conn,
                    original_id,
                    posting_journal_id,
                    slices=(("standard", "9.00", "0.90"),),
                )

        reversal_id, _void_journal_id = _insert_ap_void_reversal(
            conn,
            original_id,
            posting_journal_id,
            finalize=False,
            slices=(),
        )
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, reversal_id)

        mixed_original_id, mixed_posting_journal_id = _insert_ap_original(
            conn,
            source_record_id=43,
            amount="10.00",
            gst_amount="0.50",
            tax_code="mixed",
            tax_slice_count=2,
            slices=(("standard", "5.00", "0.50"), ("gst_free", "5.00", "0.00")),
        )
        partial_reversal_id, _mixed_void_journal_id = _insert_ap_void_reversal(
            conn,
            mixed_original_id,
            mixed_posting_journal_id,
            source_record_id=43,
            journal_source_id=43,
            amount="10.00",
            gst_amount="0.50",
            tax_code="mixed",
            tax_slice_count=2,
            finalize=False,
            slices=(("standard", "5.00", "0.50"),),
        )
        with pytest.raises(Exception, match="incomplete or unreconciled"):
            _finalize_gst_event(conn, partial_reversal_id)

    engine.dispose()


def test_gst_adjustment_events_are_company_database_isolated(tmp_path):
    first = _engine(tmp_path / "company-one.db")
    second = _engine(tmp_path / "company-two.db")
    run_company_migrations(first)
    run_company_migrations(second)

    with first.begin() as conn:
        _insert_gst_event(conn, reason="First company synthetic event")
    with second.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM gst_adjustment_events")
        ).scalar_one() == 0


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
