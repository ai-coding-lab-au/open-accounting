from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import CompanyBase
from app.db.company import SQLiteImmediateLockError, begin_sqlite_immediate
from app.db.errors import DataRecoveryRequiredError
from app.db.migrations import allocation_baseline_integrity_hash, run_company_migrations
from app.models import company as _company_models  # noqa: F401
from app.models import outgoing as _outgoing_models  # noqa: F401
from app.models.company import (
    AllocationHistoryBaselineFinalization,
    BankTxnDirection,
    BankTransaction,
)
from app.services import allocation_history
from app.services import bank_accounts, bank_import, invoice_payments
from app.services.allocation_history import (
    AllocationHistoryCaptureError,
    AllocationHistoryConflict,
    capture_legacy_allocation_baseline,
    translate_allocation_history_db_error,
)


def _database(path):
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    CompanyBase.metadata.create_all(engine)
    run_company_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO contacts (id, kind, name, active, created_at) "
            "VALUES (1, 'supplier', 'Synthetic contact', 1, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO invoices (id, direction, contact_id, invoice_number, issue_date, "
            "currency, subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
            "source, created_at, updated_at) VALUES "
            "(1, 'AP', 1, 'SYN-AP', '2026-09-01', 'AUD', 10, 1, 11, 1, 'draft', 10, "
            "'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(2, 'AR', 1, 'SYN-AR', '2026-09-01', 'AUD', 10, 1, 11, 1, 'draft', 10, "
            "'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO bank_accounts (id, name, opening_balance, is_active) "
            "VALUES (1, 'Synthetic bank', 0, 1)"
        ))
        connection.execute(text(
            "INSERT INTO bank_transactions "
            "(id, bank_account_id, direction, amount, occurred_at, gst_amount, tax_code, "
            "unapplied_amount) VALUES "
            "(1, 1, 'out', 10, '2026-10-01', 1, 'standard', 0), "
            "(2, 1, 'in', 10, '2026-10-01', 1, 'standard', 0)"
        ))
        connection.execute(text(
            "INSERT INTO invoice_payment_allocations "
            "(id, bank_transaction_id, invoice_id, amount, gst_amount, created_at) VALUES "
            "(11, 1, 1, 10, 1, '2026-10-01 08:00:00'), "
            "(22, 2, 2, 10, 1, '2026-10-01 08:01:00')"
        ))
        connection.execute(text(
            "INSERT INTO invoice_payment_tax_components "
            "(id, allocation_id, tax_code, gross_amount, gst_amount) VALUES "
            "(111, 11, 'standard', 10, 1), (222, 22, 'standard', 10, 1)"
        ))
    return engine

class _ProjectorReadRows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError("Expected at most one substituted projector row")
        return self._rows[0] if self._rows else None


def _substitute_projector_read(
    monkeypatch,
    session,
    *,
    query_fragment,
    column,
    value,
    mapped=True,
):
    original_execute = session.execute
    matched = False

    def execute_with_substituted_read(statement, *args, **kwargs):
        nonlocal matched
        result = original_execute(statement, *args, **kwargs)
        if query_fragment not in str(statement):
            return result
        matched = True
        if mapped:
            rows = [dict(row) for row in result.mappings().all()]
            for row in rows:
                row[column] = value
            return _ProjectorReadRows(rows)

        keys = tuple(result.keys())
        rows = [dict(zip(keys, row)) for row in result.all()]
        for row in rows:
            row[column] = value
        return _ProjectorReadRows(
            [tuple(row[key] for key in keys) for row in rows]
        )

    monkeypatch.setattr(session, "execute", execute_with_substituted_read)
    return lambda: matched


def _allocation_batch_database(
    path, *, baseline_tax_code="standard", baseline_gst_amount="1"
):
    engine = _database(path)
    with engine.begin() as connection:
        connection.execute(text("UPDATE bank_transactions SET amount=12 WHERE id=2"))
        connection.execute(text(
            "UPDATE invoice_payment_allocations SET gst_amount=:gst WHERE id=22"
        ), {"gst": baseline_gst_amount})
        connection.execute(text(
            "UPDATE invoice_payment_tax_components SET tax_code=:tax_code, "
            "gross_amount=10, gst_amount=:gst WHERE id=222"
        ), {"tax_code": baseline_tax_code, "gst": baseline_gst_amount})
        connection.execute(text(
            "INSERT INTO accounts (id, code, name, type, is_gst, active) "
            "VALUES (2, '5000', 'Synthetic expense', 'EXPENSE', 0, 1), "
            "(3, '2200', 'Synthetic customer deposits', 'LIABILITY', 0, 1), "
            "(4, '2210', 'Synthetic inactive liability', 'LIABILITY', 0, 0), "
            "(5, '1200', 'Synthetic GST paid', 'ASSET', 1, 1), "
            "(6, '2100', 'Synthetic GST collected', 'LIABILITY', 1, 1), "
            "(7, '1100', 'Synthetic receivables control', 'ASSET', 0, 1), "
            "(8, '2000', 'Synthetic payables control', 'LIABILITY', 0, 1), "
            "(9, '2300', 'Synthetic bank ledger', 'LIABILITY', 0, 1), "
            "(10, '5100', 'Synthetic eligible asset', 'ASSET', 0, 1)"
        ))
    with Session(engine) as session:
        baseline = capture_legacy_allocation_baseline(
            session, 2, datetime(2026, 10, 12, 10, 0, 0)
        )
        assert baseline.status == "complete"
        session.commit()
    return engine, baseline.baseline_event_id


def _exact_bank_cash_writer_database(
    path, *, bank_amount, allocation_amount, gst_amount, tax_code
):
    engine = _database(path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE bank_transactions SET amount=:bank_amount, gst_amount=:gst_amount, "
                "tax_code=:tax_code WHERE id=2"
            ),
            {
                "bank_amount": bank_amount,
                "gst_amount": gst_amount,
                "tax_code": tax_code,
            },
        )
        connection.execute(
            text(
                "UPDATE invoice_payment_allocations SET amount=:allocation_amount, "
                "gst_amount=:gst_amount WHERE id=22"
            ),
            {"allocation_amount": allocation_amount, "gst_amount": gst_amount},
        )
        connection.execute(
            text(
                "UPDATE invoice_payment_tax_components SET tax_code=:tax_code, "
                "gross_amount=:allocation_amount, gst_amount=:gst_amount WHERE id=222"
            ),
            {
                "tax_code": tax_code,
                "allocation_amount": allocation_amount,
                "gst_amount": gst_amount,
            },
        )
        connection.execute(
            text(
                "INSERT INTO accounts (id, code, name, type, is_gst, active) "
                "VALUES (3, '2200', 'Synthetic customer deposits', 'LIABILITY', 0, 1)"
            )
        )
    with Session(engine) as session:
        baseline = capture_legacy_allocation_baseline(
            session, 2, datetime(2026, 10, 12, 9)
        )
        assert baseline.status == "complete"
        session.commit()
    return engine, baseline.baseline_event_id


def _standard_writer_request(baseline_id, *, idempotency_key="replay-key"):
    from app.services.allocation_batches import AllocationLineSpec

    return {
        "bank_transaction_id": 2,
        "effective_date": "2026-10-01",
        "recorded_at": "2026-10-12 10:00:00",
        "idempotency_key": idempotency_key,
        "reason": "Synthetic exact cash conversion test",
        "evidence_reference": "synthetic://exact-cash",
        "actor_placeholder": "Synthetic operator",
        "expected_prior_version": 0,
        "lines": [AllocationLineSpec("invoice", 2, 1000, 100, (("standard", 1000, 100),))],
        "residual_cents": 100,
        "residual_account_id": 3,
        "baseline_finalization_id": baseline_id,
        "books_locked_through": None,
    }


@pytest.mark.parametrize(
    ("bank_amount", "allocation_amount", "gst_amount", "tax_code", "residual_cents", "expected_cash_cents"),
    (
        ("11.00", "10.00", "1.00", "standard", 100, 1100),
        ("0.29", "0.29", "0", "none", 0, 29),
        ("1", "1", "0", "none", 0, 100),
        ("1.2", "1.2", "0", "none", 0, 120),
        ("1.20", "1.20", "0", "none", 0, 120),
        ("999999.99", "999999.99", "0", "none", 0, 99999999),
    ),
)
def test_batch_writer_converts_bank_cash_to_exact_cents(
    tmp_path,
    bank_amount,
    allocation_amount,
    gst_amount,
    tax_code,
    residual_cents,
    expected_cash_cents,
):
    from inspect import getsource
    from unittest.mock import patch

    from app.services import allocation_batches
    from app.services.allocation_batches import (
        AllocationLineSpec,
        _batch_payload,
        _hash_payload,
        _payload_rows,
        parse_exact_money,
        write_allocation_correction,
    )

    assert "int(bank_transaction.amount)" not in getsource(allocation_batches)
    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / f"cash-{expected_cash_cents}.db",
        bank_amount=bank_amount,
        allocation_amount=allocation_amount,
        gst_amount=gst_amount,
        tax_code=tax_code,
    )
    allocation_gross_cents = parse_exact_money(allocation_amount)
    allocation_gst_cents = parse_exact_money(gst_amount)
    line = AllocationLineSpec(
        "invoice",
        2,
        allocation_gross_cents,
        allocation_gst_cents,
        ((tax_code, allocation_gross_cents, allocation_gst_cents),),
    )
    writer_args = {
        "bank_transaction_id": 2,
        "effective_date": "2026-10-01",
        "recorded_at": "2026-10-12 10:00:00",
        "idempotency_key": f"exact-cash-{expected_cash_cents}",
        "reason": "Synthetic exact cash conversion test",
        "evidence_reference": "synthetic://exact-cash",
        "actor_placeholder": "Synthetic operator",
        "expected_prior_version": 0,
        "lines": [line],
        "residual_cents": residual_cents,
        "residual_account_id": 3 if residual_cents else None,
        "baseline_finalization_id": baseline_id,
        "books_locked_through": None,
    }
    request_payload = _batch_payload(
        operation="allocation_correction",
        bank_transaction_id=2,
        expected_prior_version=0,
        effective_date=writer_args["effective_date"],
        recorded_at=writer_args["recorded_at"],
        idempotency_key=writer_args["idempotency_key"],
        reason=writer_args["reason"],
        evidence_reference=writer_args["evidence_reference"],
        evidence_sha256=None,
        actor_placeholder=writer_args["actor_placeholder"],
        linked_prior_batch_id=None,
        internal_correction_approval_id=None,
        residual_cents=residual_cents,
        residual_account_id=writer_args["residual_account_id"],
        baseline_finalization_id=baseline_id,
    )
    request_hash = _hash_payload(_payload_rows(request_payload, [line]))
    with Session(engine) as session:
        result = write_allocation_correction(session, **writer_args)
        assert result.bank_cash_cents == expected_cash_cents
        assert result.manual_review_status == "not_required"
        finalized = session.execute(
            text(
                "SELECT b.canonical_payload_hash, f.canonical_integrity_hash, "
                "f.bank_cash_cents, f.total_allocated_cents, f.residual_cents, "
                "f.component_count "
                "FROM allocation_batches b JOIN allocation_batch_finalizations f "
                "ON f.batch_id=b.id WHERE b.id=:batch_id"
            ),
            {"batch_id": result.batch_id},
        ).one()
        assert finalized.canonical_payload_hash == finalized.canonical_integrity_hash
        trigger_hash = session.execute(
            text(
                "SELECT allocation_baseline_sha256(payload) FROM ("
                "SELECT json_object('kind','batch','bank_transaction_id',b.bank_transaction_id,"
                "'operation',b.operation,'transaction_version',b.transaction_version,"
                "'expected_prior_version',b.expected_prior_version,'effective_date',b.effective_date,"
                "'recorded_at',b.recorded_at,'idempotency_key',b.idempotency_key,'reason',b.reason,"
                "'evidence_reference',b.evidence_reference,'evidence_sha256',b.evidence_sha256,"
                "'actor_placeholder',b.actor_placeholder,'linked_prior_batch_id',b.linked_prior_batch_id,"
                "'internal_correction_approval_id',b.internal_correction_approval_id,"
                "'manual_review_status',b.manual_review_status,'residual_amount_cents',b.residual_amount_cents,"
                "'residual_account_id',b.residual_account_id,'baseline_finalization_id',"
                "b.baseline_finalization_id) AS payload FROM allocation_batches b WHERE b.id=:batch_id "
                "UNION ALL SELECT json_object('kind','line','line_order',l.line_order,"
                "'source_kind',l.source_kind,'invoice_or_bill_id',l.invoice_or_bill_id,"
                "'gross_cents',l.gross_cents,'gst_cents',l.gst_cents) "
                "FROM allocation_batch_lines l WHERE l.batch_id=:batch_id "
                "UNION ALL SELECT json_object('kind','component','line_order',l.line_order,"
                "'tax_code',c.tax_code,'gross_cents',c.gross_cents,'gst_cents',c.gst_cents) "
                "FROM allocation_batch_lines l JOIN allocation_batch_tax_components c "
                "ON c.allocation_batch_line_id=l.id WHERE l.batch_id=:batch_id)"
            ),
            {"batch_id": result.batch_id},
        ).scalar_one()
        assert finalized.canonical_integrity_hash == trigger_hash
        assert request_hash != finalized.canonical_integrity_hash
        assert finalized.bank_cash_cents == expected_cash_cents
        assert finalized.total_allocated_cents + finalized.residual_cents == expected_cash_cents
        assert finalized.component_count == 1
        assert session.execute(
            text("SELECT idempotency_key FROM allocation_batches WHERE id=:batch_id"),
            {"batch_id": result.batch_id},
        ).scalar_one() == writer_args["idempotency_key"]
        session.commit()

    with Session(engine) as session:
        cache_before_replay = session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one()
        artifact_counts_before_replay = session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches WHERE bank_transaction_id=2), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one()
    with Session(engine) as session:
        assert session.execute(
            text("SELECT id FROM allocation_batches WHERE idempotency_key=:key"),
            {"key": writer_args["idempotency_key"]},
        ).scalar_one() == result.batch_id
    with Session(engine) as session:
        from app.services import allocation_batches

        owner_rows = []
        original_lookup = allocation_batches._existing_idempotency_batch

        def lookup_owner(db, key):
            owner = original_lookup(db, key)
            owner_rows.append(owner)
            return owner

        with patch.object(allocation_batches, "_existing_idempotency_batch", side_effect=lookup_owner):
            with patch.object(
                allocation_batches,
                "_validate_priors",
                side_effect=AssertionError("exact replay must bypass stale-version validation"),
            ):
                replay = write_allocation_correction(session, **writer_args)
        assert len(owner_rows) == 1
        assert owner_rows[0]["id"] == result.batch_id
        assert replay.batch_id == result.batch_id
        assert replay.operation == result.operation
        assert replay.transaction_version == result.transaction_version
        assert replay.expected_prior_version == result.expected_prior_version
        assert replay.bank_transaction_id == result.bank_transaction_id == 2
        assert replay.bank_cash_cents == expected_cash_cents
        assert replay.manual_review_status == result.manual_review_status == "not_required"
        assert replay.baseline_finalization_id == result.baseline_finalization_id == baseline_id
        assert replay.finalization_integrity_hash == result.finalization_integrity_hash
        assert session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches WHERE bank_transaction_id=2), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == artifact_counts_before_replay
        assert session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one() == cache_before_replay
        session.rollback()
    engine.dispose()


def test_batch_writer_rejects_subcent_stored_bank_cash_before_writes(tmp_path):
    from app.services.allocation_batches import (
        AllocationLineSpec,
        InvalidExactMoneyInputError,
        write_allocation_correction,
    )

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "cash-subcent.db",
        bank_amount="11.005",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    line = AllocationLineSpec("invoice", 2, 1000, 100, (("standard", 1000, 100),))
    with Session(engine) as session:
        before_cache = session.execute(
            text("SELECT paid_amount, paid_date, status FROM invoices WHERE id=2")
        ).one()
        before_allocations = session.execute(
            text(
                "SELECT id, bank_transaction_id, invoice_id, amount, gst_amount, created_at "
                "FROM invoice_payment_allocations "
                "WHERE bank_transaction_id=2 ORDER BY id"
            )
        ).all()
        before_components = session.execute(
            text(
                "SELECT c.id, c.allocation_id, c.tax_code, c.gross_amount, c.gst_amount "
                "FROM invoice_payment_tax_components c "
                "JOIN invoice_payment_allocations a ON a.id=c.allocation_id "
                "WHERE a.bank_transaction_id=2 ORDER BY c.id"
            )
        ).all()
        before_transaction = session.execute(
            text("SELECT amount, direction, occurred_at FROM bank_transactions WHERE id=2")
        ).one()
    with Session(engine) as session:
        with pytest.raises(InvalidExactMoneyInputError, match="exact to the cent"):
            write_allocation_correction(
                session,
                bank_transaction_id=2,
                effective_date="2026-10-01",
                recorded_at="2026-10-12 10:00:00",
                idempotency_key="invalid-subcent-cash",
                reason="Synthetic invalid stored cash test",
                evidence_reference="synthetic://invalid-cash",
                actor_placeholder="Synthetic operator",
                expected_prior_version=0,
                lines=[line],
                residual_cents=100,
                residual_account_id=3,
                baseline_finalization_id=baseline_id,
                books_locked_through=None,
            )
        assert not session.in_transaction()
        session.commit()

    with Session(engine) as session:
        assert session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == (0, 0, 0, 0)
        assert session.execute(
            text("SELECT paid_amount, paid_date, status FROM invoices WHERE id=2")
        ).one() == before_cache
        assert session.execute(
            text(
                "SELECT id, bank_transaction_id, invoice_id, amount, gst_amount, created_at "
                "FROM invoice_payment_allocations "
                "WHERE bank_transaction_id=2 ORDER BY id"
            )
        ).all() == before_allocations
        assert session.execute(
            text(
                "SELECT c.id, c.allocation_id, c.tax_code, c.gross_amount, c.gst_amount "
                "FROM invoice_payment_tax_components c "
                "JOIN invoice_payment_allocations a ON a.id=c.allocation_id "
                "WHERE a.bank_transaction_id=2 ORDER BY c.id"
            )
        ).all() == before_components
        assert session.execute(
            text("SELECT amount, direction, occurred_at FROM bank_transactions WHERE id=2")
        ).one() == before_transaction
        assert session.execute(
            text("SELECT CAST(amount AS TEXT) FROM bank_transactions WHERE id=2")
        ).scalar_one() == "11.005"
    engine.dispose()


def _batch_payload_rows(
    *, baseline_id, bank_transaction_id, operation, version, expected_version, effective_date,
    recorded_at, idempotency_key, manual_review_status, residual_cents,
    residual_account_id, linked_prior_batch_id, internal_approval_id, line,
):
    rows = [{
        "kind": "batch",
        "bank_transaction_id": bank_transaction_id,
        "operation": operation,
        "transaction_version": version,
        "expected_prior_version": expected_version,
        "effective_date": effective_date,
        "recorded_at": recorded_at,
        "idempotency_key": idempotency_key,
        "reason": "Synthetic transaction-wide allocation change",
        "evidence_reference": "synthetic://allocation-batch",
        "evidence_sha256": None,
        "actor_placeholder": "Synthetic operator",
        "linked_prior_batch_id": linked_prior_batch_id,
        "internal_correction_approval_id": internal_approval_id,
        "manual_review_status": manual_review_status,
        "residual_amount_cents": residual_cents,
        "residual_account_id": residual_account_id,
        "baseline_finalization_id": baseline_id,
    }]
    if line is not None:
        rows.append({
            "kind": "line",
            "line_order": 0,
            "source_kind": line["source_kind"],
            "invoice_or_bill_id": line["invoice_or_bill_id"],
            "gross_cents": line["gross_cents"],
            "gst_cents": line["gst_cents"],
        })
        for component in line["components"]:
            rows.append({
                "kind": "component",
                "line_order": 0,
                "tax_code": component["tax_code"],
                "gross_cents": component["gross_cents"],
                "gst_cents": component["gst_cents"],
            })
    return rows


def _insert_allocation_batch(
    connection, baseline_id, *, bank_transaction_id=2,
    operation="allocation_correction", version=1,
    expected_version=0, effective_date="2026-10-01",
    recorded_at="2026-10-12 11:00:00", idempotency_key="synthetic-batch-1",
    manual_review_status="not_required", residual_cents=200,
    residual_account_id=3, linked_prior_batch_id=None, internal_approval_id=None,
    line=None,
):
    rows = _batch_payload_rows(
        baseline_id=baseline_id,
        bank_transaction_id=bank_transaction_id,
        operation=operation,
        version=version,
        expected_version=expected_version,
        effective_date=effective_date,
        recorded_at=recorded_at,
        idempotency_key=idempotency_key,
        manual_review_status=manual_review_status,
        residual_cents=residual_cents,
        residual_account_id=residual_account_id,
        linked_prior_batch_id=linked_prior_batch_id,
        internal_approval_id=internal_approval_id,
        line=line,
    )
    values = {
        "bank_transaction_id": bank_transaction_id,
        "operation": operation,
        "transaction_version": version,
        "expected_prior_version": expected_version,
        "effective_date": effective_date,
        "recorded_at": recorded_at,
        "idempotency_key": idempotency_key,
        "canonical_payload_hash": allocation_baseline_integrity_hash(rows),
        "reason": "Synthetic transaction-wide allocation change",
        "evidence_reference": "synthetic://allocation-batch",
        "evidence_sha256": None,
        "actor_placeholder": "Synthetic operator",
        "linked_prior_batch_id": linked_prior_batch_id,
        "internal_correction_approval_id": internal_approval_id,
        "manual_review_status": manual_review_status,
        "residual_amount_cents": residual_cents,
        "residual_account_id": residual_account_id,
        "baseline_finalization_id": baseline_id,
    }
    return connection.execute(text(
        "INSERT INTO allocation_batches (bank_transaction_id, operation, "
        "transaction_version, expected_prior_version, effective_date, recorded_at, "
        "idempotency_key, canonical_payload_hash, reason, evidence_reference, "
        "evidence_sha256, actor_placeholder, linked_prior_batch_id, "
        "internal_correction_approval_id, manual_review_status, "
        "residual_amount_cents, residual_account_id, baseline_finalization_id) "
        "VALUES (:bank_transaction_id, :operation, :transaction_version, "
        ":expected_prior_version, :effective_date, :recorded_at, :idempotency_key, "
        ":canonical_payload_hash, :reason, :evidence_reference, :evidence_sha256, "
        ":actor_placeholder, :linked_prior_batch_id, :internal_correction_approval_id, "
        ":manual_review_status, :residual_amount_cents, :residual_account_id, "
        ":baseline_finalization_id) RETURNING id"
    ), values).scalar_one()


def _insert_allocation_batch_line(connection, batch_id, line):
    line_id = connection.execute(text(
        "INSERT INTO allocation_batch_lines "
        "(batch_id, line_order, source_kind, invoice_or_bill_id, gross_cents, gst_cents) "
        "VALUES (:batch_id, 0, :source_kind, :invoice_id, :gross_cents, :gst_cents) "
        "RETURNING id"
    ), {
        "batch_id": batch_id,
        "source_kind": line["source_kind"],
        "invoice_id": line["invoice_or_bill_id"],
        "gross_cents": line["gross_cents"],
        "gst_cents": line["gst_cents"],
    }).scalar_one()
    for component in line["components"]:
        connection.execute(text(
            "INSERT INTO allocation_batch_tax_components "
            "(allocation_batch_line_id, tax_code, gross_cents, gst_cents) "
            "VALUES (:line_id, :tax_code, :gross_cents, :gst_cents)"
        ), {"line_id": line_id, **component})
    return line_id


def _standard_batch_line(
    *, source_kind="invoice", invoice_id=2, gross_cents=1000,
    gst_cents=100, tax_code="standard",
):
    return {
        "source_kind": source_kind,
        "invoice_or_bill_id": invoice_id,
        "gross_cents": gross_cents,
        "gst_cents": gst_cents,
        "components": [{
            "tax_code": tax_code,
            "gross_cents": gross_cents,
            "gst_cents": gst_cents,
        }],
    }


def _finalize_allocation_batch(connection, batch_id, line, *, total=1000, gst=100,
                               residual=200, cash=1200):
    rows = connection.execute(text(
        "SELECT kind, payload FROM ("
        "SELECT 0 sort_key, json_object('kind','batch',"
        "'bank_transaction_id',b.bank_transaction_id,'operation',b.operation,"
        "'transaction_version',b.transaction_version,'expected_prior_version',b.expected_prior_version,"
        "'effective_date',b.effective_date,'recorded_at',b.recorded_at,"
        "'idempotency_key',b.idempotency_key,'reason',b.reason,"
        "'evidence_reference',b.evidence_reference,'evidence_sha256',b.evidence_sha256,"
        "'actor_placeholder',b.actor_placeholder,'linked_prior_batch_id',b.linked_prior_batch_id,"
        "'internal_correction_approval_id',b.internal_correction_approval_id,"
        "'manual_review_status',b.manual_review_status,"
        "'residual_amount_cents',b.residual_amount_cents,'residual_account_id',b.residual_account_id,"
        "'baseline_finalization_id',b.baseline_finalization_id) payload, 'batch' kind "
        "FROM allocation_batches b WHERE b.id=:batch_id "
        "UNION ALL SELECT 1, json_object('kind','line','line_order',l.line_order,"
        "'source_kind',l.source_kind,'invoice_or_bill_id',l.invoice_or_bill_id,"
        "'gross_cents',l.gross_cents,'gst_cents',l.gst_cents), 'line' "
        "FROM allocation_batch_lines l WHERE l.batch_id=:batch_id "
        "UNION ALL SELECT 2, json_object('kind','component','line_order',l.line_order,"
        "'tax_code',c.tax_code,'gross_cents',c.gross_cents,'gst_cents',c.gst_cents), 'component' "
        "FROM allocation_batch_lines l JOIN allocation_batch_tax_components c "
        "ON c.allocation_batch_line_id=l.id WHERE l.batch_id=:batch_id) "
        "ORDER BY sort_key"
    ), {"batch_id": batch_id}).all()
    canonical_hash = allocation_baseline_integrity_hash(
        [json.loads(row.payload) for row in rows]
    )
    connection.execute(text(
        "INSERT INTO allocation_batch_finalizations "
        "(batch_id, allocation_line_count, component_count, total_allocated_cents, "
        "total_gst_cents, residual_cents, bank_cash_cents, canonical_integrity_hash, finalized_at) "
        "VALUES (:batch_id, :line_count, :component_count, :total, :gst, :residual, :cash, "
        ":integrity_hash, '2026-10-12 11:01:00')"
    ), {
        "batch_id": batch_id,
        "line_count": 1 if line is not None else 0,
        "component_count": len(line["components"]) if line else 0,
        "total": total,
        "gst": gst,
        "residual": residual,
        "cash": cash,
        "integrity_hash": canonical_hash,
    })


def _insert_finalized_allocation_batch(
    connection,
    baseline_id,
    *,
    bank_transaction_id=2,
    operation="allocation_correction",
    version=1,
    expected_version=0,
    effective_date="2026-10-01",
    recorded_at="2026-10-12 11:00:00",
    idempotency_key="synthetic-finalized-batch",
    manual_review_status="not_required",
    residual_cents=200,
    residual_account_id=3,
    linked_prior_batch_id=None,
    line=None,
    total=1000,
    gst=100,
    cash=1200,
):
    batch_id = _insert_allocation_batch(
        connection,
        baseline_id,
        bank_transaction_id=bank_transaction_id,
        operation=operation,
        version=version,
        expected_version=expected_version,
        effective_date=effective_date,
        recorded_at=recorded_at,
        idempotency_key=idempotency_key,
        manual_review_status=manual_review_status,
        residual_cents=residual_cents,
        residual_account_id=residual_account_id,
        linked_prior_batch_id=linked_prior_batch_id,
        line=line,
    )
    if line is not None:
        _insert_allocation_batch_line(connection, batch_id, line)
    _finalize_allocation_batch(
        connection,
        batch_id,
        line,
        total=total,
        gst=gst,
        residual=residual_cents,
        cash=cash,
    )
    return batch_id


def _insert_correction_history(connection, *, transaction_id, allocation_id, invoice_id):
    source_kind = "bill" if invoice_id == 1 else "invoice"
    snapshot_id = connection.execute(
        text(
            "INSERT INTO allocation_history_snapshots "
            "(source_kind, invoice_id, gross_cents, gst_cents, created_at) "
            "VALUES (:source_kind, :invoice_id, 1000, 100, '2026-10-02 09:00:00') "
            "RETURNING id"
        ),
        {"source_kind": source_kind, "invoice_id": invoice_id},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO allocation_history_tax_components "
            "(snapshot_id, tax_code, gross_cents, gst_cents) "
            "VALUES (:snapshot_id, 'standard', 1000, 100)"
        ),
        {"snapshot_id": snapshot_id},
    )
    connection.execute(
        text(
            "INSERT INTO allocation_history_finalizations "
            "(snapshot_id, component_count, gross_cents, gst_cents, finalized_at) "
            "VALUES (:snapshot_id, 1, 1000, 100, '2026-10-02 09:01:00')"
        ),
        {"snapshot_id": snapshot_id},
    )
    connection.execute(
        text(
            "INSERT INTO allocation_history_events "
            "(operation, bank_transaction_id, allocation_id, snapshot_id, version, "
            "effective_date, recorded_at, idempotency_key, expected_prior_version, "
            "actor_placeholder, reason, evidence_reference, linked_allocation_id) "
            "VALUES ('allocation_correction', :transaction_id, :allocation_id, "
            ":snapshot_id, 1, '2026-10-02', '2026-10-02 09:02:00', "
            "'synthetic-backfill-correction', 0, 'Synthetic operator', "
            "'Synthetic prior correction', 'synthetic://allocation-evidence', "
            ":allocation_id)"
        ),
        {
            "transaction_id": transaction_id,
            "allocation_id": allocation_id,
            "snapshot_id": snapshot_id,
        },
    )


def _backfill_database(path, *, history_kind=None):
    engine = _database(path)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO invoice_lines "
            "(id, invoice_id, description, quantity, unit_price, gst_rate, "
            "line_subtotal, line_gst, line_total, tax_code) VALUES "
            "(1, 1, 'Synthetic AP line', 1, 10, 0.1, 10, 1, 11, 'standard'), "
            "(2, 2, 'Synthetic AR line', 1, 10, 0.1, 10, 1, 11, 'standard')"
        ))
        connection.execute(text(
            "UPDATE invoices SET status='authorised', paid_amount=0"
        ))
    if history_kind == "baseline":
        with Session(engine) as session:
            capture_legacy_allocation_baseline(
                session, 2, datetime(2026, 10, 12)
            )
            session.commit()
    elif history_kind == "correction":
        with engine.begin() as connection:
            _insert_correction_history(
                connection, transaction_id=2, allocation_id=22, invoice_id=2
            )
    with engine.begin() as connection:
        if history_kind is None:
            connection.execute(text("DELETE FROM invoice_payment_tax_components"))
        else:
            connection.execute(text(
                "DELETE FROM invoice_payment_tax_components WHERE allocation_id=11"
            ))
        connection.execute(text(
            "UPDATE bank_transactions SET gst_amount=0, tax_code='none'"
        ))
    return engine


def _snapshot(engine):
    tables = (
        "bank_transactions",
        "invoice_payment_allocations",
        "invoice_payment_tax_components",
        "invoices",
        "credit_notes",
        "credit_note_applications",
        "credit_note_refunds",
        "gst_adjustment_events",
        "journal_entries",
    )
    with engine.connect() as connection:
        return {
            table: connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).all()
            for table in tables
        }


@pytest.mark.parametrize(
    ("transaction_id", "allocation_id", "component_id", "status_direction"),
    [(1, 11, 111, "AP"), (2, 22, 222, "AR")],
)
def test_captures_complete_ap_and_ar_baselines(
    tmp_path, transaction_id, allocation_id, component_id, status_direction
):
    engine = _database(tmp_path / f"{status_direction}.db")
    before = _snapshot(engine)
    observed_at = datetime(2026, 10, 12, 9, 30, 15, 123456)
    if status_direction == "AR":
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(session, transaction_id, observed_at)
        assert result.status == "complete"
        assert result.incomplete_reasons == ()
        assert result.history_boundary_date == date(2026, 10, 1)
        assert result.observation_timestamp == observed_at
        assert result.allocation_count == 1
        assert result.component_count == 1
        assert result.gross_total_cents == 1000
        assert result.gst_total_cents == 100
        assert result.pre_baseline_history_complete is False

        binding = session.execute(text(
            "SELECT allocation_row_id, source_kind, invoice_or_bill_id, "
            "allocated_gross_cents, allocated_gross_source, allocated_gst_cents, "
            "allocated_gst_source, allocation_created_at "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert tuple(binding) == (
            allocation_id,
            "bill" if status_direction == "AP" else "invoice",
            1 if status_direction == "AP" else 2,
            1000,
            "10",
            100,
            "1",
            "2026-10-01 08:00:00" if status_direction == "AP" else "2026-10-01 08:01:00",
        )
        component = session.execute(text(
            "SELECT source_component_row_id, tax_code, component_gross_cents, "
            "component_gross_source, component_gst_cents, component_gst_source "
            "FROM allocation_history_baseline_tax_bindings tb "
            "JOIN allocation_history_baseline_bindings b ON b.id=tb.allocation_binding_id "
            "WHERE b.baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert tuple(component) == (component_id, "standard", 1000, "10", 100, "1")
        persisted_hash = session.execute(text(
            "SELECT integrity_hash FROM allocation_history_baseline_finalizations "
            "WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).scalar_one()
        assert persisted_hash == result.integrity_hash
        assert session.get(AllocationHistoryBaselineFinalization, result.baseline_event_id)
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("missing_components", ("missing_tax_components",)),
        ("unsupported_tax", ("unsupported_tax_evidence",)),
        ("subcent", ("invalid_amount", "unreconciled_totals")),
        ("invalid", ("invalid_amount", "unreconciled_totals")),
        ("unreconciled", ("unreconciled_totals",)),
        ("incompatible_invoice", ("insufficient_source_data",)),
        ("incompatible_transaction", ("insufficient_source_data",)),
        ("malformed_payment_date", ("insufficient_source_data",)),
    ],
)
def test_incomplete_legacy_evidence_is_captured_without_rounding(tmp_path, setup, expected):
    engine = _database(tmp_path / f"{setup}.db")
    with engine.begin() as connection:
        if setup == "missing_components":
            connection.execute(text(
                "DELETE FROM invoice_payment_tax_components WHERE allocation_id=11"
            ))
        elif setup == "unsupported_tax":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(text(
                "UPDATE invoice_payment_tax_components SET tax_code='unsupported' "
                "WHERE allocation_id=11"
            ))
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        elif setup == "subcent":
            connection.execute(text(
                "UPDATE invoice_payment_allocations SET amount=10.001 WHERE id=11"
            ))
        elif setup == "invalid":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(text(
                "UPDATE invoice_payment_allocations SET amount=-10 WHERE id=11"
            ))
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        elif setup == "unreconciled":
            connection.execute(text(
                "UPDATE invoice_payment_tax_components SET gross_amount=9 "
                "WHERE allocation_id=11"
            ))
        elif setup == "incompatible_invoice":
            connection.execute(text("UPDATE invoices SET direction='XX' WHERE id=1"))
        elif setup == "incompatible_transaction":
            connection.execute(text("UPDATE bank_transactions SET direction='OUT' WHERE id=1"))
        elif setup == "malformed_payment_date":
            connection.execute(text(
                "UPDATE bank_transactions SET occurred_at='not-a-date' WHERE id=1"
            ))

    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 9)
        )
        assert result.status == "incomplete"
        assert result.incomplete_reasons == expected
        if setup in {"incompatible_invoice", "incompatible_transaction"}:
            assert result.direction_evidence == (
                ("OUT" if setup == "incompatible_transaction" else "out", "XX" if setup == "incompatible_invoice" else "AP"),
            )
        stored_reasons = session.execute(text(
            "SELECT incomplete_reason_codes FROM allocation_history_baseline_finalizations "
            "WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).scalar_one()
        assert tuple(json.loads(stored_reasons)) == result.incomplete_reasons
        stored_hash = session.execute(text(
            "SELECT integrity_hash FROM allocation_history_baseline_finalizations "
            "WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).scalar_one()
        assert result.integrity_hash == stored_hash
        if setup == "malformed_payment_date":
            assert result.history_boundary_date is None
        allocation_source = session.execute(text(
            "SELECT allocated_gross_source, allocated_gross_cents "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        if setup == "subcent":
            assert allocation_source == ("10.001", None)
        elif setup == "invalid":
            assert allocation_source == ("-10", None)
        elif setup in {"incompatible_invoice", "incompatible_transaction"}:
            source_kind = session.execute(text(
                "SELECT source_kind FROM allocation_history_baseline_bindings "
                "WHERE baseline_event_id=:id"
            ), {"id": result.baseline_event_id}).scalar_one()
            if setup == "incompatible_invoice":
                assert source_kind is None
        session.rollback()
    engine.dispose()


_VALID_MONEY_CASES = (
    ("0.29", 29),
    ("0.57", 57),
    ("1", 100),
    ("1.2", 120),
    ("1.20", 120),
    ("999999.99", 99_999_999),
)


@pytest.mark.parametrize(("raw_amount", "expected_cents"), _VALID_MONEY_CASES)
def test_exact_cent_service_and_trigger_agreement(tmp_path, raw_amount, expected_cents):
    engine = _database(tmp_path / f"valid-{raw_amount.replace('.', '-')}.db")
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE invoice_payment_allocations "
            "SET amount=:amount, gst_amount=:amount WHERE id=11"
        ), {"amount": raw_amount})
        connection.execute(text(
            "UPDATE invoice_payment_tax_components "
            "SET gross_amount=:amount, gst_amount=:amount WHERE id=111"
        ), {"amount": raw_amount})
        source_values = connection.execute(text(
            "SELECT CAST(a.amount AS TEXT), CAST(a.gst_amount AS TEXT), "
            "CAST(c.gross_amount AS TEXT), CAST(c.gst_amount AS TEXT) "
            "FROM invoice_payment_allocations a "
            "JOIN invoice_payment_tax_components c ON c.allocation_id=a.id WHERE a.id=11"
        )).one()
        assert tuple(connection.execute(text(
            "SELECT allocation_baseline_cents(:gross), allocation_baseline_cents(:gst), "
            "allocation_baseline_cents(:component_gross), "
            "allocation_baseline_cents(:component_gst)"
        ), {
            "gross": source_values[0],
            "gst": source_values[1],
            "component_gross": source_values[2],
            "component_gst": source_values[3],
        }).one()) == (expected_cents,) * 4

    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 9)
        )
        assert result.status == "complete"
        assert result.incomplete_reasons == ()
        assert result.gross_total_cents == expected_cents
        assert result.gst_total_cents == expected_cents
        allocation = session.execute(text(
            "SELECT allocated_gross_cents, allocated_gross_source, "
            "allocated_gst_cents, allocated_gst_source "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        component = session.execute(text(
            "SELECT component_gross_cents, component_gross_source, "
            "component_gst_cents, component_gst_source "
            "FROM allocation_history_baseline_tax_bindings tb "
            "JOIN allocation_history_baseline_bindings b ON b.id=tb.allocation_binding_id "
            "WHERE b.baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert allocation == (expected_cents, source_values[0], expected_cents, source_values[1])
        assert component == (expected_cents, source_values[2], expected_cents, source_values[3])
        finalization = session.execute(text(
            "SELECT incomplete_reason_codes, integrity_hash "
            "FROM allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert json.loads(finalization.incomplete_reason_codes) == []
        assert finalization.integrity_hash == result.integrity_hash
        session.rollback()
    engine.dispose()


def test_allocation_batch_finalization_reconciles_complete_state_and_is_append_only(tmp_path):
    engine, baseline_id = _allocation_batch_database(tmp_path / "batch-valid.db")
    line = {
        "source_kind": "invoice",
        "invoice_or_bill_id": 2,
        "gross_cents": 1000,
        "gst_cents": 100,
        "components": [{
            "tax_code": "standard", "gross_cents": 1000, "gst_cents": 100,
        }],
    }
    with engine.begin() as connection:
        batch_id = _insert_allocation_batch(connection, baseline_id, line=line)
        _insert_allocation_batch_line(connection, batch_id, line)
        _finalize_allocation_batch(connection, batch_id, line)

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT allocation_line_count, component_count, total_allocated_cents, "
            "total_gst_cents, residual_cents, bank_cash_cents "
            "FROM allocation_batch_finalizations WHERE batch_id=:id"
        ), {"id": batch_id}).one() == (1, 1, 1000, 100, 200, 1200)
        assert connection.execute(text(
            "SELECT amount FROM bank_transactions WHERE id=2"
        )).scalar_one() == 12
        assert connection.execute(text(
            "SELECT paid_amount FROM invoices WHERE id=2"
        )).scalar_one() == 10

    immutable_statements = (
        "UPDATE allocation_batches SET reason='changed' WHERE id=:id",
        "DELETE FROM allocation_batches WHERE id=:id",
        "INSERT OR REPLACE INTO allocation_batches SELECT * FROM allocation_batches WHERE id=:id",
        "UPDATE allocation_batch_lines SET gross_cents=999 WHERE batch_id=:id",
        "DELETE FROM allocation_batch_lines WHERE batch_id=:id",
        "INSERT OR REPLACE INTO allocation_batch_lines SELECT * FROM allocation_batch_lines WHERE batch_id=:id",
        "UPDATE allocation_batch_tax_components SET gross_cents=999 "
        "WHERE allocation_batch_line_id=(SELECT id FROM allocation_batch_lines WHERE batch_id=:id)",
        "DELETE FROM allocation_batch_tax_components WHERE allocation_batch_line_id="
        "(SELECT id FROM allocation_batch_lines WHERE batch_id=:id)",
        "INSERT OR REPLACE INTO allocation_batch_tax_components "
        "SELECT * FROM allocation_batch_tax_components WHERE allocation_batch_line_id="
        "(SELECT id FROM allocation_batch_lines WHERE batch_id=:id)",
        "UPDATE allocation_batch_finalizations SET total_allocated_cents=999 WHERE batch_id=:id",
        "DELETE FROM allocation_batch_finalizations WHERE batch_id=:id",
        "INSERT OR REPLACE INTO allocation_batch_finalizations "
        "SELECT * FROM allocation_batch_finalizations WHERE batch_id=:id",
    )
    for statement in immutable_statements:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(statement), {"id": batch_id})
    engine.dispose()


@pytest.mark.parametrize(
    ("review_status", "should_finalize"),
    [
        ("not_required", False),
        ("required", False),
        ("rejected", False),
        ("approved", True),
    ],
)
def test_allocation_batch_changed_bas_vector_requires_approved_review(
    tmp_path, review_status, should_finalize
):
    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"batch-review-{review_status}.db"
    )
    line = {
        "source_kind": "invoice",
        "invoice_or_bill_id": 2,
        "gross_cents": 900,
        "gst_cents": 90,
        "components": [{
            "tax_code": "standard", "gross_cents": 900, "gst_cents": 90,
        }],
    }
    with engine.begin() as connection:
        batch_id = _insert_allocation_batch(
            connection,
            baseline_id,
            manual_review_status=review_status,
            residual_cents=300,
            line=line,
        )
        _insert_allocation_batch_line(connection, batch_id, line)
    if should_finalize:
        with engine.begin() as connection:
            _finalize_allocation_batch(
                connection, batch_id, line, total=900, gst=90, residual=300
            )
    else:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _finalize_allocation_batch(
                    connection, batch_id, line, total=900, gst=90, residual=300
                )
    engine.dispose()


@pytest.mark.parametrize(
    (
        "baseline_tax_code", "after_tax_code", "after_gross", "review_status",
        "accepted",
    ),
    [
        ("none", "none", 1000, "not_required", True),
        ("gst_free", "gst_free", 900, "not_required", False),
        ("none", "gst_free", 1000, "not_required", False),
        ("none", "gst_free", 1000, "approved", True),
        ("gst_free", "none", 1000, "not_required", False),
    ],
)
def test_allocation_batch_bas_vector_tracks_zero_gst_and_none_components(
    tmp_path, baseline_tax_code, after_tax_code, after_gross, review_status, accepted
):
    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"batch-vector-{baseline_tax_code}-{after_tax_code}-{review_status}.db",
        baseline_tax_code=baseline_tax_code,
        baseline_gst_amount="0",
    )
    line = _standard_batch_line(
        gross_cents=after_gross,
        gst_cents=0,
        tax_code=after_tax_code,
    )
    residual = 1200 - after_gross
    with engine.begin() as connection:
        batch_id = _insert_allocation_batch(
            connection,
            baseline_id,
            idempotency_key=f"vector-{review_status}",
            manual_review_status=review_status,
            residual_cents=residual,
            line=line,
        )
        _insert_allocation_batch_line(connection, batch_id, line)
    if accepted:
        with engine.begin() as connection:
            _finalize_allocation_batch(
                connection,
                batch_id,
                line,
                total=after_gross,
                gst=0,
                residual=residual,
            )
    else:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _finalize_allocation_batch(
                    connection,
                    batch_id,
                    line,
                    total=after_gross,
                    gst=0,
                    residual=residual,
                )
    engine.dispose()


def test_allocation_batch_rejects_component_cash_version_and_residual_errors(tmp_path):
    engine, baseline_id = _allocation_batch_database(tmp_path / "batch-invalid.db")
    with engine.begin() as connection:
        invalid_headers = (
            {"version": 2, "expected_version": 0},
            {"residual_cents": 200, "residual_account_id": None},
            {"residual_cents": 200, "residual_account_id": 7},
            {"residual_cents": 0, "residual_account_id": 2},
        )
        for index, overrides in enumerate(invalid_headers):
            with pytest.raises(IntegrityError):
                _insert_allocation_batch(
                    connection,
                    baseline_id,
                    idempotency_key=f"invalid-batch-{index}",
                    **overrides,
                )

    line = {
        "source_kind": "invoice",
        "invoice_or_bill_id": 2,
        "gross_cents": 1000,
        "gst_cents": 100,
        "components": [{
            "tax_code": "standard", "gross_cents": 900, "gst_cents": 90,
        }],
    }
    with engine.begin() as connection:
        batch_id = _insert_allocation_batch(connection, baseline_id, line=line)
        _insert_allocation_batch_line(connection, batch_id, line)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _finalize_allocation_batch(connection, batch_id, line)
    engine.dispose()


@pytest.mark.parametrize(
    ("case", "direction", "residual_cents", "account_id", "bank_ledger", "accepted"),
    [
        ("zero-null", "in", 0, None, False, True),
        ("zero-account", "in", 0, 3, False, False),
        ("positive-null", "in", 200, None, False, False),
        ("missing-account", "in", 200, 999, False, False),
        ("inactive-account", "in", 200, 4, False, False),
        ("gst-paid-control", "in", 200, 5, False, False),
        ("gst-collected-control", "in", 200, 6, False, False),
        ("ar-control", "in", 200, 7, False, False),
        ("ap-control", "in", 200, 8, False, False),
        ("bank-ledger-control", "in", 200, 9, True, False),
        ("money-in-asset", "in", 200, 2, False, False),
        ("money-in-liability", "in", 200, 3, False, True),
        ("money-out-liability", "out", 200, 3, False, False),
        ("money-out-asset", "out", 200, 10, False, True),
        ("unsupported-direction", "odd", 200, 3, False, False),
    ],
)
def test_allocation_batch_residual_account_database_policy(
    tmp_path, case, direction, residual_cents, account_id, bank_ledger, accepted
):
    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"batch-residual-{case}.db"
    )
    with engine.begin() as connection:
        if direction != "in":
            connection.execute(text(
                "UPDATE bank_transactions SET direction=:direction WHERE id=2"
            ), {"direction": direction})
        if bank_ledger:
            connection.execute(text(
                "UPDATE bank_accounts SET ledger_account_id=9 WHERE id=1"
            ))

    def insert_batch(connection):
        return _insert_allocation_batch(
            connection,
            baseline_id,
            idempotency_key=f"residual-{case}",
            residual_cents=residual_cents,
            residual_account_id=account_id,
        )

    if accepted:
        with engine.begin() as connection:
            batch_id = insert_batch(connection)
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT residual_amount_cents, residual_account_id "
                "FROM allocation_batches WHERE id=:id"
            ), {"id": batch_id}).one() == (residual_cents, account_id)
    else:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                insert_batch(connection)
    engine.dispose()


@pytest.mark.parametrize(
    ("residual_cents", "account_id", "accepted"),
    [(0, None, True), (0, 3, False), (200, None, False), (200, 2, False), (200, 3, True)],
)
def test_allocation_batch_residual_account_orm_policy(
    tmp_path, residual_cents, account_id, accepted
):
    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"batch-residual-orm-{residual_cents}-{account_id}.db"
    )
    batch = _company_models.AllocationBatch(
        bank_transaction_id=2,
        operation="allocation_correction",
        transaction_version=1,
        expected_prior_version=0,
        effective_date=date(2026, 10, 1),
        recorded_at=datetime(2026, 10, 12, 11),
        idempotency_key=f"orm-residual-{residual_cents}-{account_id}",
        canonical_payload_hash="a" * 64,
        reason="Synthetic ORM residual test",
        evidence_reference="synthetic://orm-residual",
        evidence_sha256=None,
        actor_placeholder="Synthetic operator",
        linked_prior_batch_id=None,
        internal_correction_approval_id=None,
        manual_review_status="not_required",
        residual_amount_cents=residual_cents,
        residual_account_id=account_id,
        baseline_finalization_id=baseline_id,
    )
    with Session(engine) as session:
        session.add(batch)
        if accepted:
            session.commit()
        else:
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
    engine.dispose()


def test_allocation_batch_versions_idempotency_and_prior_links_fail_closed(tmp_path):
    engine, baseline_id = _allocation_batch_database(tmp_path / "batch-versions.db")
    line = _standard_batch_line()
    with engine.begin() as connection:
        first_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            idempotency_key="version-one",
            line=line,
        )

    with Session(engine) as session:
        other_baseline = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 10)
        )
        assert other_baseline.status == "complete"
        other_baseline_id = other_baseline.baseline_event_id
        session.commit()
    other_line = _standard_batch_line(source_kind="bill", invoice_id=1)
    with engine.begin() as connection:
        other_transaction_batch_id = _insert_finalized_allocation_batch(
            connection,
            other_baseline_id,
            bank_transaction_id=1,
            idempotency_key="other-transaction-version-one",
            residual_cents=0,
            residual_account_id=None,
            line=other_line,
            cash=1000,
        )

    invalid_versions = (
        {
            "version": 3,
            "expected_version": 1,
            "linked_prior_batch_id": first_id,
            "idempotency_key": "wrong-version-step",
        },
        {
            "version": 3,
            "expected_version": 2,
            "linked_prior_batch_id": first_id,
            "idempotency_key": "skipped-version",
        },
        {
            "version": 2,
            "expected_version": 0,
            "idempotency_key": "stale-version",
        },
        {
            "version": 1,
            "expected_version": 0,
            "idempotency_key": "duplicate-transaction-version",
        },
        {
            "version": 2,
            "expected_version": 1,
            "linked_prior_batch_id": other_transaction_batch_id,
            "idempotency_key": "cross-transaction-prior",
        },
    )
    for values in invalid_versions:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_allocation_batch(
                    connection,
                    baseline_id,
                    **values,
                )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                version=2,
                expected_version=1,
                linked_prior_batch_id=first_id,
                idempotency_key="version-one",
                line=line,
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                version=2,
                expected_version=1,
                linked_prior_batch_id=first_id,
                idempotency_key="version-one",
                line=_standard_batch_line(gross_cents=900, gst_cents=90),
            )

    with engine.begin() as connection:
        second_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            version=2,
            expected_version=1,
            linked_prior_batch_id=first_id,
            idempotency_key="version-two",
            line=line,
        )
        third_id = _insert_allocation_batch(
            connection,
            baseline_id,
            version=3,
            expected_version=2,
            linked_prior_batch_id=second_id,
            idempotency_key="version-three",
            line=line,
        )
        assert third_id > second_id
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT transaction_version, expected_prior_version "
            "FROM allocation_batches WHERE bank_transaction_id=2 "
            "ORDER BY transaction_version"
        )).all() == [(1, 0), (2, 1), (3, 2)]
        assert connection.execute(text(
            "SELECT bank_transaction_id, transaction_version, expected_prior_version "
            "FROM allocation_batches WHERE id=:id"
        ), {"id": other_transaction_batch_id}).one() == (1, 1, 0)
    engine.dispose()


@pytest.mark.parametrize("boundary", ["effective_date", "recorded_at"])
def test_allocation_batch_requires_complete_baseline_observation_boundary(
    tmp_path, boundary
):
    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"batch-boundary-{boundary}.db"
    )
    overrides = (
        {"effective_date": "2026-09-30"}
        if boundary == "effective_date"
        else {"recorded_at": "2026-10-12 09:59:59"}
    )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                idempotency_key=f"before-{boundary}-boundary",
                **overrides,
            )
    engine.dispose()


def test_allocation_batch_rejects_missing_incomplete_and_cross_transaction_baselines(
    tmp_path
):
    engine, baseline_id = _allocation_batch_database(tmp_path / "batch-baselines.db")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                999,
                idempotency_key="missing-baseline",
            )

    with Session(engine) as session:
        other_baseline = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 10)
        )
        session.commit()
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                other_baseline.baseline_event_id,
                idempotency_key="cross-transaction-baseline",
            )
    engine.dispose()

    incomplete_engine = _database(tmp_path / "batch-incomplete-baseline.db")
    with incomplete_engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts (id, code, name, type, is_gst, active) "
            "VALUES (3, '2200', 'Synthetic customer deposits', 'LIABILITY', 0, 1)"
        ))
        connection.execute(text(
            "DELETE FROM invoice_payment_tax_components WHERE id=222"
        ))
    with Session(incomplete_engine) as session:
        incomplete = capture_legacy_allocation_baseline(
            session, 2, datetime(2026, 10, 12, 10)
        )
        assert incomplete.status == "incomplete"
        session.commit()
    with pytest.raises(IntegrityError):
        with incomplete_engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                incomplete.baseline_event_id,
                idempotency_key="incomplete-baseline",
            )
    incomplete_engine.dispose()


def test_allocation_batch_schema_sequence_allows_direct_sql_states(tmp_path):
    """Direct-SQL trigger coverage; not public writer coverage."""
    engine, baseline_id = _allocation_batch_database(tmp_path / "batch-operations.db")
    line = _standard_batch_line()
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_reallocation",
                effective_date="2026-10-02",
                idempotency_key="reallocation-from-baseline",
                manual_review_status="approved",
                line=line,
            )
    with engine.begin() as connection:
        correction_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            idempotency_key="operation-correction-one",
            line=line,
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                version=2,
                expected_version=1,
                linked_prior_batch_id=correction_id,
                effective_date="2026-10-02",
                idempotency_key="correction-wrong-date",
                line=line,
            )

    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT transaction_version, expected_prior_version "
            "FROM allocation_batches WHERE id=:id"
        ), {"id": correction_id}).one() == (1, 0)
        second_correction_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            version=2,
            expected_version=1,
            linked_prior_batch_id=correction_id,
            idempotency_key="operation-correction-two",
            line=line,
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_reallocation",
                version=3,
                expected_version=2,
                linked_prior_batch_id=second_correction_id,
                effective_date="2026-10-03",
                idempotency_key="reallocation-after-correction",
                manual_review_status="approved",
                line=line,
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_reversal",
                version=3,
                expected_version=2,
                linked_prior_batch_id=second_correction_id,
                effective_date="2026-10-01",
                idempotency_key="reversal-not-later",
                manual_review_status="approved",
                residual_cents=1200,
                line=None,
            )

    excessive_line = _standard_batch_line(gross_cents=1100, gst_cents=110)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            reversal_id = _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_reversal",
                version=3,
                expected_version=2,
                linked_prior_batch_id=second_correction_id,
                effective_date="2026-10-02",
                idempotency_key="reversal-exceeds-prior",
                manual_review_status="approved",
                residual_cents=100,
                line=excessive_line,
            )
            _insert_allocation_batch_line(connection, reversal_id, excessive_line)
            _finalize_allocation_batch(
                connection,
                reversal_id,
                excessive_line,
                total=1100,
                gst=110,
                residual=100,
            )

    with engine.begin() as connection:
        reversal_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            operation="allocation_reversal",
            version=3,
            expected_version=2,
            linked_prior_batch_id=second_correction_id,
            effective_date="2026-10-02",
            idempotency_key="operation-reversal-three",
            manual_review_status="approved",
            residual_cents=1200,
            residual_account_id=3,
            line=None,
            total=0,
            gst=0,
            cash=1200,
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_correction",
                version=4,
                expected_version=3,
                linked_prior_batch_id=reversal_id,
                idempotency_key="correction-after-reversal",
                line=line,
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_reallocation",
                version=4,
                expected_version=3,
                linked_prior_batch_id=reversal_id,
                effective_date="2026-10-02",
                idempotency_key="reallocation-not-later",
                manual_review_status="approved",
                line=line,
            )

    with engine.begin() as connection:
        reallocation_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            operation="allocation_reallocation",
            version=4,
            expected_version=3,
            linked_prior_batch_id=reversal_id,
            effective_date="2026-10-03",
            idempotency_key="operation-reallocation-four",
            manual_review_status="approved",
            line=line,
        )

    for operation, linked_id, key in (
        ("allocation_correction", reallocation_id, "correction-after-reallocation"),
        ("allocation_reallocation", reallocation_id, "reallocation-after-reallocation"),
    ):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_allocation_batch(
                    connection,
                    baseline_id,
                    operation=operation,
                    version=5,
                    expected_version=4,
                    linked_prior_batch_id=linked_id,
                    effective_date="2026-10-04" if operation == "allocation_reallocation" else "2026-10-01",
                    idempotency_key=key,
                    manual_review_status="approved",
                    line=line,
                )
    engine.dispose()


@pytest.mark.parametrize(
    ("link_reversal", "finalized", "effective_date"),
    [(False, True, "2026-10-03"), (True, False, "2026-10-03"), (True, True, "2026-10-02")],
)
def test_allocation_batch_schema_reallocation_requires_later_finalized_reversal(
    tmp_path, link_reversal, finalized, effective_date
):
    """Direct-SQL schema trigger coverage, not a public writer workflow."""
    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"batch-reallocation-gate-{link_reversal}-{finalized}-{effective_date}.db"
    )
    with engine.begin() as connection:
        reversal_id = _insert_allocation_batch(
            connection,
            baseline_id,
            operation="allocation_reversal",
            effective_date="2026-10-02",
            idempotency_key="reallocation-gate-reversal",
            manual_review_status="approved",
            residual_cents=1200,
            line=None,
        )
        if finalized:
            _finalize_allocation_batch(
                connection,
                reversal_id,
                None,
                total=0,
                gst=0,
                residual=1200,
                cash=1200,
            )

    should_accept = link_reversal and finalized and effective_date > "2026-10-02"
    line = _standard_batch_line()
    if should_accept:
        with engine.begin() as connection:
            batch_id = _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_reallocation",
                version=2,
                expected_version=1,
                linked_prior_batch_id=reversal_id,
                effective_date=effective_date,
                idempotency_key="eligible-reallocation",
                manual_review_status="approved",
                line=line,
            )
            _insert_allocation_batch_line(connection, batch_id, line)
    else:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_allocation_batch(
                    connection,
                    baseline_id,
                    operation="allocation_reallocation",
                    version=2,
                    expected_version=1,
                    linked_prior_batch_id=reversal_id if link_reversal else None,
                    effective_date=effective_date,
                    idempotency_key="ineligible-reallocation",
                    manual_review_status="approved",
                    line=line,
                )
    engine.dispose()


def test_allocation_batch_migrations_do_not_backfill_baselines_or_financial_rows(
    tmp_path
):
    engine = _database(tmp_path / "batch-no-backfill.db")
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE invoices SET contact_name_snapshot='Synthetic contact'"
        ))
        connection.execute(text(
            "INSERT INTO journal_entries (entry_date, memo, source_type, source_id) "
            "VALUES ('2026-09-01', 'Synthetic AP invoice provenance', 'invoice_ap', 1), "
            "('2026-09-01', 'Synthetic AR invoice provenance', 'invoice_ar', 2)"
        ))
    before = _snapshot(engine)
    with engine.connect() as connection:
        bank_state = connection.execute(text(
            "SELECT id, bank_account_id, amount, direction, occurred_at, gst_amount, tax_code "
            "FROM bank_transactions ORDER BY id"
        )).all()
        invoice_state = connection.execute(text(
            "SELECT id, paid_amount, status FROM invoices ORDER BY id"
        )).all()
        allocation_state = connection.execute(text(
            "SELECT * FROM invoice_payment_allocations ORDER BY id"
        )).all()
        component_state = connection.execute(text(
            "SELECT * FROM invoice_payment_tax_components ORDER BY id"
        )).all()
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT id FROM invoice_payment_allocations ORDER BY id"
        )).scalars().all() == [11, 22]
        assert connection.execute(text(
            "SELECT id FROM invoice_payment_tax_components ORDER BY id"
        )).scalars().all() == [111, 222]
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_batches"
        )).scalar_one() == 0

    first_run = run_company_migrations(engine)
    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT id, bank_account_id, amount, direction, occurred_at, gst_amount, tax_code "
            "FROM bank_transactions ORDER BY id"
        )).all() == bank_state
        assert connection.execute(text(
            "SELECT id, paid_amount, status FROM invoices ORDER BY id"
        )).all() == invoice_state
        assert connection.execute(text(
            "SELECT * FROM invoice_payment_allocations ORDER BY id"
        )).all() == allocation_state
        assert connection.execute(text(
            "SELECT * FROM invoice_payment_tax_components ORDER BY id"
        )).all() == component_state
        assert connection.execute(text(
            "SELECT id FROM invoice_payment_allocations ORDER BY id"
        )).scalars().all() == [11, 22]
        assert connection.execute(text(
            "SELECT id FROM invoice_payment_tax_components ORDER BY id"
        )).scalars().all() == [111, 222]
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_finalizations"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_batches"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_batch_lines"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_batch_tax_components"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_batch_finalizations"
        )).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM payment_reconciliation_events"
        )).scalar_one() == 0

    second_run = run_company_migrations(engine)
    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT id, bank_account_id, amount, direction, occurred_at, gst_amount, tax_code "
            "FROM bank_transactions ORDER BY id"
        )).all() == bank_state
        assert connection.execute(text(
            "SELECT id, paid_amount, status FROM invoices ORDER BY id"
        )).all() == invoice_state
        assert connection.execute(text(
            "SELECT * FROM invoice_payment_allocations ORDER BY id"
        )).all() == allocation_state
        assert connection.execute(text(
            "SELECT * FROM invoice_payment_tax_components ORDER BY id"
        )).all() == component_state
    assert not any(step.startswith("reconcile:payments:") for step in first_run + second_run)
    assert not any(step.startswith("backfill:") for step in first_run + second_run)
    engine.dispose()


def _insert_batch_lodgment_marker(connection):
    return connection.execute(text(
        "INSERT INTO bas_period_lodgment_markers "
        "(period_id, period_start, period_end, lodged_status, internally_asserted_at, "
        "actor_placeholder, evidence_reference, evidence_scope) "
        "VALUES ('synthetic-period', '2026-10-01', '2026-10-31', 1, "
        "'2026-10-12 10:00:00', 'Synthetic operator', 'synthetic://lodgment', "
        "'internal_assertion_not_ato_lodgment_evidence') RETURNING id"
    )).scalar_one()


def _insert_batch_correction_approval(connection, marker_id, idempotency_key):
    return connection.execute(text(
        "INSERT INTO allocation_period_correction_approvals "
        "(period_marker_id, allocation_id, approved_idempotency_key, actor_placeholder, "
        "reason, evidence_reference, recorded_at) VALUES (:marker_id, 22, :key, "
        "'Synthetic reviewer', 'Synthetic correction approval', "
        "'synthetic://correction-approval', '2026-10-12 10:30:00') RETURNING id"
    ), {"marker_id": marker_id, "key": idempotency_key}).scalar_one()


def test_allocation_batch_schema_lodged_correction_requires_transaction_approval(tmp_path):
    """Direct-SQL schema coverage; the public writer has no approval path."""
    engine, baseline_id = _allocation_batch_database(tmp_path / "batch-correction-marker.db")
    with engine.begin() as connection:
        marker_id = _insert_batch_lodgment_marker(connection)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                idempotency_key="approved-correction",
                line=None,
            )

    line = {
        "source_kind": "invoice",
        "invoice_or_bill_id": 2,
        "gross_cents": 1000,
        "gst_cents": 100,
        "components": [{
            "tax_code": "standard", "gross_cents": 1000, "gst_cents": 100,
        }],
    }
    with engine.begin() as connection:
        approval_id = _insert_batch_correction_approval(
            connection, marker_id, "approved-correction"
        )
        batch_id = _insert_allocation_batch(
            connection,
            baseline_id,
            idempotency_key="approved-correction",
            internal_approval_id=approval_id,
            line=line,
        )
        _insert_allocation_batch_line(connection, batch_id, line)
    engine.dispose()


def test_allocation_batch_schema_reversal_inside_marker_ignores_correction_approval(tmp_path):
    """Direct-SQL schema trigger coverage only."""
    engine, baseline_id = _allocation_batch_database(tmp_path / "batch-reversal-marker.db")
    with engine.begin() as connection:
        marker_id = _insert_batch_lodgment_marker(connection)
        approval_id = _insert_batch_correction_approval(
            connection, marker_id, "reversal-is-not-authorized"
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_allocation_batch(
                connection,
                baseline_id,
                operation="allocation_reversal",
                effective_date="2026-10-02",
                idempotency_key="reversal-is-not-authorized",
                manual_review_status="approved",
                residual_cents=1200,
                internal_approval_id=approval_id,
            )
    engine.dispose()


_INVALID_MONEY_CASES = (
    "0.001",
    "1.001",
    "1.005",
    "1.009",
    "10.999",
    "-0.01",
    "NaN",
    "Infinity",
    "-Infinity",
    "92233720368547758.08",
)


@pytest.mark.parametrize("raw_amount", _INVALID_MONEY_CASES)
def test_invalid_legacy_money_stays_raw_and_is_incomplete(tmp_path, raw_amount):
    engine = _database(tmp_path / f"invalid-{raw_amount.replace('.', '-')}.db")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(text(
            "UPDATE invoice_payment_allocations SET amount=:amount, gst_amount=0 WHERE id=11"
        ), {"amount": raw_amount})
        connection.execute(text(
            "UPDATE invoice_payment_tax_components "
            "SET gross_amount=:amount, gst_amount=0 WHERE id=111"
        ), {"amount": raw_amount})
        source_values = connection.execute(text(
            "SELECT CAST(a.amount AS TEXT), CAST(c.gross_amount AS TEXT) "
            "FROM invoice_payment_allocations a "
            "JOIN invoice_payment_tax_components c ON c.allocation_id=a.id WHERE a.id=11"
        )).one()
        assert connection.execute(text(
            "SELECT allocation_baseline_cents(:raw)"
        ), {"raw": source_values[0]}).scalar_one() is None
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 9)
        )
        assert result.status == "incomplete"
        assert result.incomplete_reasons == ("invalid_amount", "unreconciled_totals")
        allocation = session.execute(text(
            "SELECT allocated_gross_cents, allocated_gross_source "
            "FROM allocation_history_baseline_bindings WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        component = session.execute(text(
            "SELECT component_gross_cents, component_gross_source "
            "FROM allocation_history_baseline_tax_bindings tb "
            "JOIN allocation_history_baseline_bindings b ON b.id=tb.allocation_binding_id "
            "WHERE b.baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert allocation == (None, source_values[0])
        assert component == (None, source_values[1])
        finalization = session.execute(text(
            "SELECT incomplete_reason_codes, integrity_hash "
            "FROM allocation_history_baseline_finalizations WHERE baseline_event_id=:id"
        ), {"id": result.baseline_event_id}).one()
        assert tuple(json.loads(finalization.incomplete_reason_codes)) == result.incomplete_reasons
        assert finalization.integrity_hash == result.integrity_hash
        session.rollback()
    engine.dispose()


def test_missing_transaction_and_zero_allocations_fail_without_artifacts(tmp_path):
    engine = _database(tmp_path / "missing-source.db")
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryCaptureError, match="does not exist"):
            capture_legacy_allocation_baseline(session, 999, datetime(2026, 10, 12))
        session.rollback()

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO bank_transactions "
            "(id, bank_account_id, direction, amount, occurred_at, gst_amount, tax_code, "
            "unapplied_amount) VALUES (3, 1, 'out', 1, '2026-10-02', 0, 'none', 0)"
        ))
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryCaptureError, match="without allocations"):
            capture_legacy_allocation_baseline(session, 3, datetime(2026, 10, 12))
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events WHERE bank_transaction_id=3"
        )).scalar_one() == 0
        session.rollback()
    engine.dispose()


def test_duplicate_and_preexisting_history_fail_closed(tmp_path):
    engine = _database(tmp_path / "duplicate.db")
    with Session(engine) as session:
        capture_legacy_allocation_baseline(session, 1, datetime(2026, 10, 12))
        with pytest.raises(AllocationHistoryCaptureError, match="already has allocation history"):
            capture_legacy_allocation_baseline(session, 1, datetime(2026, 10, 12))
        session.rollback()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO allocation_history_events (operation, bank_transaction_id) "
            "VALUES ('legacy_history_unknown', 2)"
        ))
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryCaptureError, match="already has allocation history"):
            capture_legacy_allocation_baseline(session, 2, datetime(2026, 10, 12))
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_finalizations "
            "WHERE bank_transaction_id=2"
        )).scalar_one() == 0
        session.rollback()
    engine.dispose()


def test_source_mutation_aborts_capture_atomically(tmp_path):
    engine = _database(tmp_path / "mutation.db")
    with Session(engine) as session:
        mutated = False

        def mutate_before_finalization(current_session, _flush_context, _instances):
            nonlocal mutated
            if not mutated and any(
                isinstance(obj, AllocationHistoryBaselineFinalization)
                for obj in current_session.new
            ):
                current_session.connection().exec_driver_sql(
                    "UPDATE invoice_payment_allocations SET amount=9 WHERE id=11"
                )
                mutated = True

        event.listen(session, "before_flush", mutate_before_finalization)
        try:
            with pytest.raises(IntegrityError, match="Legacy allocation baseline"):
                capture_legacy_allocation_baseline(
                    session, 1, datetime(2026, 10, 12)
                )
        finally:
            event.remove(session, "before_flush", mutate_before_finalization)
        assert mutated
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events WHERE bank_transaction_id=1"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_bindings"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_tax_bindings"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_finalizations"
        )).scalar_one() == 0
        assert session.execute(text(
            "SELECT amount FROM invoice_payment_allocations WHERE id=11"
        )).scalar_one() == 10
        session.rollback()
    engine.dispose()


def test_capture_does_not_commit_or_change_operational_rows(tmp_path):
    engine = _database(tmp_path / "preservation.db")
    before = _snapshot(engine)
    with Session(engine) as session:
        result = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12)
        )
        assert session.in_transaction()
        with Session(engine) as observer:
            assert observer.execute(text(
                "SELECT COUNT(*) FROM allocation_history_events "
                "WHERE id=:id"
            ), {"id": result.baseline_event_id}).scalar_one() == 0
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()


def test_two_sessions_cannot_both_capture_one_transaction(tmp_path, monkeypatch):
    engine = _database(tmp_path / "race.db")
    first_acquired = threading.Event()
    first_release = threading.Event()
    second_reached_begin = threading.Event()
    second_acquired = threading.Event()
    session_ids = {}
    begin_calls = []
    original_begin = allocation_history.begin_sqlite_immediate

    def instrumented_begin(session):
        label = session.info["race_label"]
        if session.info.get("race_begin_seen"):
            return original_begin(session)
        begin_calls.append(label)
        if label == "second":
            second_reached_begin.set()
        original_begin(session)
        session.info["race_begin_seen"] = True
        if label == "first":
            first_acquired.set()
            if not first_release.wait(timeout=5):
                raise AssertionError("first session was not released from its immediate lock")
        else:
            second_acquired.set()

    monkeypatch.setattr(allocation_history, "begin_sqlite_immediate", instrumented_begin)

    def capture(label):
        with Session(engine) as session:
            session.info["race_label"] = label
            session_ids[label] = id(session)
            try:
                result = capture_legacy_allocation_baseline(
                    session, 1, datetime(2026, 10, 12)
                )
                session.commit()
                return ("success", result.baseline_event_id)
            except AllocationHistoryCaptureError:
                session.rollback()
                return ("duplicate", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(capture, "first")
        second_future = None
        try:
            assert first_acquired.wait(timeout=5), "first session did not acquire BEGIN IMMEDIATE"
            second_future = pool.submit(capture, "second")
            assert second_reached_begin.wait(timeout=5), "second session did not reach BEGIN IMMEDIATE"
            assert not second_acquired.wait(timeout=0.25), (
                "second session acquired BEGIN IMMEDIATE while first still held it"
            )
        finally:
            first_release.set()
        first_outcome = first_future.result(timeout=10)
        assert second_future is not None, "second capture was not scheduled"
        outcomes = [first_outcome, second_future.result(timeout=10)]
    assert session_ids["first"] != session_ids["second"]
    assert set(begin_calls) == {"first", "second"}
    assert sum(outcome[0] == "success" for outcome in outcomes) == 1
    assert sum(outcome[0] == "duplicate" for outcome in outcomes) == 1
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events "
            "WHERE bank_transaction_id=1"
        )).scalar_one() == 1
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_baseline_finalizations "
            "WHERE bank_transaction_id=1"
        )).scalar_one() == 1
    engine.dispose()


def test_unrelated_transaction_and_operational_values_are_preserved(tmp_path):
    engine = _database(tmp_path / "unrelated.db")
    before = _snapshot(engine)
    with Session(engine) as session:
        capture_legacy_allocation_baseline(session, 1, datetime(2026, 10, 12))
        assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()


@pytest.mark.parametrize("history_kind", ["baseline", "event"])
@pytest.mark.parametrize("specs", [[], [{"invoice_id": 1, "amount": "1.00"}]])
def test_history_blocks_allocation_clear_and_replacement_without_state_change(
    tmp_path, history_kind, specs
):
    engine = _database(tmp_path / f"guard-{history_kind}-{len(specs)}.db")
    with Session(engine) as session:
        if history_kind == "baseline":
            capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            transaction_id = 1
        else:
            session.execute(text(
                "INSERT INTO allocation_history_events "
                "(operation, bank_transaction_id) "
                "VALUES ('legacy_history_unknown', 2)"
            ))
            session.commit()
            transaction_id = 2
    before = _snapshot(engine)

    with Session(engine) as session:
        begin_sqlite_immediate(session)
        transaction = session.get(BankTransaction, transaction_id)
        with pytest.raises(AllocationHistoryConflict) as caught:
            invoice_payments.replace_transaction_allocations(
                session, transaction, specs
            )
        assert caught.value.http_status == 409
        assert "SQLite" not in str(caught.value)
        assert "trigger" not in str(caught.value).lower()

    assert _snapshot(engine) == before
    engine.dispose()


@pytest.mark.parametrize("history_kind", ["baseline", "event"])
def test_historical_none_allocation_request_is_read_only(
    tmp_path, history_kind
):
    engine = _database(tmp_path / f"readonly-{history_kind}.db")
    with Session(engine) as session:
        if history_kind == "baseline":
            capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            transaction_id = 1
        else:
            session.execute(text(
                "INSERT INTO allocation_history_events "
                "(operation, bank_transaction_id) "
                "VALUES ('legacy_history_unknown', 2)"
            ))
            session.commit()
            transaction_id = 2
    before = _snapshot(engine)

    with Session(engine) as session:
        begin_sqlite_immediate(session)
        transaction = session.get(BankTransaction, transaction_id)
        invoice_payments.replace_transaction_allocations(
            session, transaction, None
        )
        session.commit()

    assert _snapshot(engine) == before
    engine.dispose()


def test_only_recognized_history_trigger_errors_translate(tmp_path):
    engine = _database(tmp_path / "history-error-translation.db")
    recognized = IntegrityError(
        "DELETE FROM bank_transactions",
        {},
        sqlite3.IntegrityError("Bank transaction has allocation history"),
    )
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryConflict):
            translate_allocation_history_db_error(session, recognized)

    for message in ("database is locked", "UNIQUE constraint failed: invoices.id"):
        unrelated = IntegrityError(
            "INSERT INTO invoices",
            {},
            sqlite3.IntegrityError(message),
        )
        with Session(engine) as session:
            assert translate_allocation_history_db_error(session, unrelated) is None
    engine.dispose()


def test_backfill_rebuilds_missing_components_without_history(tmp_path):
    engine = _backfill_database(tmp_path / "backfill-no-history.db")
    before = _snapshot(engine)
    with Session(engine) as session:
        assert invoice_payments.backfill_missing_tax_components(session) == 2
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.commit()

    with Session(engine) as session:
        allocations = session.execute(text(
            "SELECT bank_transaction_id, CAST(gst_amount AS TEXT) "
            "FROM invoice_payment_allocations "
            "ORDER BY bank_transaction_id"
        )).all()
        assert allocations == [(1, "0.91"), (2, "0.91")]
        components = session.execute(text(
            "SELECT allocation_id, tax_code, CAST(gross_amount AS TEXT), "
            "CAST(gst_amount AS TEXT) "
            "FROM invoice_payment_tax_components ORDER BY allocation_id"
        )).all()
        assert components == [
            (11, "standard", "10", "0.91"),
            (22, "standard", "10", "0.91"),
        ]
        transactions = session.execute(text(
            "SELECT CAST(gst_amount AS TEXT), tax_code "
            "FROM bank_transactions ORDER BY id"
        )).all()
        assert transactions == [
            ("0.91", "standard"),
            ("0.91", "standard"),
        ]
        invoices = session.execute(text(
            "SELECT status, CAST(paid_amount AS TEXT) FROM invoices ORDER BY id"
        )).all()
        assert invoices == [
            ("partial", "10"),
            ("partial", "10"),
        ]
    engine.dispose()


@pytest.mark.parametrize("history_kind", ["baseline", "correction"])
def test_backfill_history_preflight_rejects_entire_mixed_set(
    tmp_path, history_kind
):
    engine = _backfill_database(
        tmp_path / f"backfill-history-{history_kind}.db",
        history_kind=history_kind,
    )
    before = _snapshot(engine)
    with Session(engine) as session:
        with pytest.raises(
            invoice_payments.PaymentAllocationError,
            match="backfill is blocked because allocation history exists",
        ) as caught:
            invoice_payments.backfill_missing_tax_components(session)
        assert "SQLite" not in str(caught.value)
        assert "trigger" not in str(caught.value).lower()
        assert not session.in_transaction()

    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events"
        )).scalar_one() == 1
    engine.dispose()


def test_startup_wraps_history_backfill_rejection_as_recovery_error(
    tmp_path, monkeypatch
):
    engine = _backfill_database(
        tmp_path / "startup-backfill-history.db", history_kind="baseline"
    )
    before = _snapshot(engine)
    from app.db import company as company_db
    from app.db import migrations as migration_module
    from app.db import schema_sync
    from app.services import chart_of_accounts

    monkeypatch.setattr(
        company_db, "get_company_engine", lambda *_args, **_kwargs: engine
    )
    monkeypatch.setattr(schema_sync, "sync_missing_columns", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(migration_module, "run_company_migrations", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(schema_sync, "detect_drift", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(schema_sync, "require_clean_schema", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chart_of_accounts, "seed_default_coa", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(migration_module, "reconcile_system_accounts", lambda *_args, **_kwargs: [])

    with pytest.raises(DataRecoveryRequiredError) as caught:
        company_db.init_company_db("synthetic-backfill")
    assert str(caught.value) == (
        "Legacy invoice payment allocations could not be reconciled safely: "
        "Legacy tax-component backfill is blocked because allocation history exists. "
        "Restore the pre-migration backup or review the recorded payment "
        "reconciliation events before retrying."
    )
    assert "SQLite" not in str(caught.value)
    assert _snapshot(engine) == before
    engine.dispose()


def test_direct_mutation_services_lock_before_relevant_reads(tmp_path):
    engine = _database(tmp_path / "service-lock-order.db")
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)

    def assert_first_statement_is_lock(action):
        statements.clear()
        action()
        assert statements[0] == "BEGIN IMMEDIATE", statements

    with Session(engine) as session:
        assert_first_statement_is_lock(
            lambda: bank_accounts.record_manual_transaction(
                session,
                idempotency_key="direct-lock-manual",
                payload_hash="payload",
                bank_account_id=1,
                direction=BankTxnDirection.IN,
                amount=Decimal("1.00"),
                occurred_at=date(2026, 10, 1),
                tax_code="none",
                gst_registered=True,
            )
        )

    with Session(engine) as session:
        assert_first_statement_is_lock(
            lambda: bank_import.commit_import(
                session, bank_account_id=1, rows=[], gst_registered=True
            )
        )

    with Session(engine) as session:
        def missing_recategorisation():
            with pytest.raises(bank_accounts.BankAccountNotFound):
                bank_accounts.recategorise_transaction(
                    session, txn_id=999, gst_registered=True
                )

        assert_first_statement_is_lock(missing_recategorisation)

    with Session(engine) as session:
        def missing_deletion():
            with pytest.raises(bank_accounts.BankAccountNotFound):
                bank_accounts.delete_manual_transaction(session, txn_id=999)

        assert_first_statement_is_lock(missing_deletion)

    with Session(engine) as session:
        transaction = BankTransaction(
            id=1,
            bank_account_id=1,
            direction=BankTxnDirection.OUT,
            amount=Decimal("10.00"),
            occurred_at=date(2026, 10, 1),
            invoice_allocations=[],
        )
        statements.clear()
        invoice_payments.replace_transaction_allocations(session, transaction, [])
        assert statements[0] == "BEGIN IMMEDIATE", statements
        session.rollback()

    event.remove(engine, "before_cursor_execute", record_statement)
    engine.dispose()


def test_immediate_lock_ownership_tracks_root_commit_and_rollback(tmp_path):
    engine = _database(tmp_path / "lock-ownership-lifecycle.db")
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    with Session(engine) as session:
        begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 1

        bank_import.commit_import(
            session, bank_account_id=1, rows=[], gst_registered=True
        )
        assert statements.count("BEGIN IMMEDIATE") == 1
        assert session.in_transaction()

        session.commit()
        assert "sqlite_immediate_lock_owner" not in session.info
        session.execute(text("SELECT 1"))
        with pytest.raises(SQLiteImmediateLockError):
            begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 1
        session.rollback()

        begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 2
        session.rollback()
        assert "sqlite_immediate_lock_owner" not in session.info

        session.execute(text("SELECT 1"))
        with pytest.raises(SQLiteImmediateLockError):
            begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 2
        session.rollback()

        begin_sqlite_immediate(session)
        assert statements.count("BEGIN IMMEDIATE") == 3
        session.rollback()

    class _NonSqliteDialect:
        name = "postgresql"

    class _NonSqliteBind:
        dialect = _NonSqliteDialect()

    class _NonSqliteSession:
        info = {}

        def get_bind(self):
            return _NonSqliteBind()

    non_sqlite_session = _NonSqliteSession()
    begin_sqlite_immediate(non_sqlite_session)
    assert non_sqlite_session.info == {}
    event.remove(engine, "before_cursor_execute", record_statement)
    engine.dispose()


def test_direct_service_fails_closed_after_autobegun_read(tmp_path):
    engine = _database(tmp_path / "read-before-lock.db")
    before = _snapshot(engine)
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        assert statements == ["SELECT 1"]
        with pytest.raises(SQLiteImmediateLockError):
            bank_accounts.recategorise_transaction(
                session,
                txn_id=1,
                tax_code="none",
                gst_amount=Decimal("0.00"),
                gst_registered=True,
            )
        assert statements == ["SELECT 1"]
        session.commit()

    assert _snapshot(engine) == before
    event.remove(engine, "before_cursor_execute", record_statement)
    engine.dispose()


def test_direct_mutation_services_leave_commit_to_caller(tmp_path):
    engine = _database(tmp_path / "caller-owned-transaction.db")
    from app.services.bank_accounts import record_manual_transaction

    with Session(engine) as session:
        record_manual_transaction(
            session,
            idempotency_key="caller-owned-manual",
            payload_hash="caller-owned-payload",
            bank_account_id=1,
            direction=BankTxnDirection.IN,
            amount=Decimal("1.00"),
            occurred_at=date(2026, 10, 2),
            tax_code="none",
            gst_registered=True,
        )
        assert session.in_transaction()
        with Session(engine) as observer:
            assert observer.query(BankTransaction).count() == 2
        session.commit()
    with Session(engine) as observer:
        assert observer.query(BankTransaction).count() == 3

    before = _snapshot(engine)
    with Session(engine) as session:
        bank_accounts.recategorise_transaction(
            session,
            txn_id=1,
            account_id=None,
            tax_code="none",
            gst_amount=Decimal("0.00"),
            gst_registered=True,
        )
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before

    with Session(engine) as session:
        bank_accounts.delete_manual_transaction(session, txn_id=2)
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before

    import_row = {
        "occurred_at": date(2026, 10, 2),
        "direction": "in",
        "amount": Decimal("2.00"),
        "dedup_key": "caller-owned-import",
        "account_id": None,
        "tax_code": "none",
        "gst_amount": Decimal("0.00"),
        "memo": "Caller-owned import",
        "counter_party_name": None,
        "invoice_allocations": [],
        "unapplied_account_id": None,
    }
    with Session(engine) as session:
        assert bank_import.commit_import(
            session,
            bank_account_id=1,
            rows=[import_row],
            gst_registered=True,
        ) == {"created": 1, "skipped_duplicates": 0}
        assert session.in_transaction()
        with Session(engine) as observer:
            assert _snapshot(engine) == before
        session.rollback()
    assert _snapshot(engine) == before
    engine.dispose()


def test_failed_direct_mutations_cannot_commit_partial_work(tmp_path):
    engine = _database(tmp_path / "failed-caller-owned-transaction.db")
    before = _snapshot(engine)
    from app.services.bank_accounts import InvoiceSettlementConflict, record_manual_transaction

    with Session(engine) as session:
        with pytest.raises(InvoiceSettlementConflict):
            record_manual_transaction(
                session,
                idempotency_key="failed-caller-owned-manual",
                payload_hash="failed-payload",
                bank_account_id=1,
                direction=BankTxnDirection.IN,
                amount=Decimal("1.00"),
                occurred_at=date(2026, 10, 2),
                invoice_allocations=[{"invoice_id": 2, "amount": "1.00"}],
                tax_code="none",
                gst_registered=True,
            )
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == before

    with Session(engine) as session:
        with pytest.raises(bank_accounts.BankTxnError):
            bank_accounts.recategorise_transaction(
                session,
                txn_id=1,
                tax_code="gst_free",
                gst_registered=True,
            )
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == before

    import_rows = [
        {
            "occurred_at": date(2026, 10, 2),
            "direction": "in",
            "amount": Decimal("2.00"),
            "dedup_key": "partial-import-first",
            "account_id": None,
            "tax_code": "none",
            "gst_amount": Decimal("0.00"),
            "memo": "First import row",
            "counter_party_name": None,
            "invoice_allocations": [],
            "unapplied_account_id": None,
        },
        {
            "occurred_at": date(2026, 10, 3),
            "direction": "out",
            "amount": Decimal("3.00"),
            "dedup_key": "partial-import-second",
            "account_id": 999,
            "tax_code": "none",
            "gst_amount": Decimal("0.00"),
            "memo": "Invalid second import row",
            "counter_party_name": None,
            "invoice_allocations": [],
            "unapplied_account_id": None,
        },
    ]
    with Session(engine) as session:
        with pytest.raises(ValueError, match="account_id does not reference"):
            bank_import.commit_import(
                session,
                bank_account_id=1,
                rows=import_rows,
                gst_registered=True,
            )
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == before

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO allocation_history_events "
            "(operation, bank_transaction_id) "
            "VALUES ('legacy_history_unknown', 2)"
        ))
    delete_before = _snapshot(engine)
    with Session(engine) as session:
        with pytest.raises(AllocationHistoryConflict):
            bank_accounts.delete_manual_transaction(session, txn_id=2)
        assert not session.in_transaction()
        session.commit()
    assert _snapshot(engine) == delete_before
    engine.dispose()


def test_failed_allocation_replacement_with_history_cannot_commit_partial_work(tmp_path):
    engine = _database(tmp_path / "failed-history-replacement.db")
    with engine.begin() as connection:
        _insert_correction_history(
            connection, transaction_id=2, allocation_id=22, invoice_id=2
        )

    tables = (
        "bank_transactions",
        "invoice_payment_allocations",
        "invoice_payment_tax_components",
        "invoices",
        "allocation_history_snapshots",
        "allocation_history_tax_components",
        "allocation_history_finalizations",
        "allocation_history_events",
    )

    def persisted_state(connection):
        return {
            table: connection.execute(
                text(f"SELECT * FROM {table} ORDER BY rowid")
            ).all()
            for table in tables
        }

    with engine.connect() as connection:
        before = persisted_state(connection)

    with Session(engine) as session:
        begin_sqlite_immediate(session)
        transaction = session.get(BankTransaction, 2)
        assert transaction is not None
        with pytest.raises(AllocationHistoryConflict):
            invoice_payments.replace_transaction_allocations(
                session, transaction, []
            )
        assert not session.in_transaction()
        session.commit()

    with Session(engine) as fresh_session:
        assert persisted_state(fresh_session.connection()) == before
    engine.dispose()


def test_baseline_capture_and_direct_recategorisation_serialize(tmp_path, monkeypatch):
    engine = _database(tmp_path / "baseline-mutation-race.db")
    before = _snapshot(engine)
    baseline_locked = threading.Event()
    release_baseline = threading.Event()
    mutation_waiting = threading.Event()
    mutation_locked = threading.Event()
    original_begin = allocation_history.begin_sqlite_immediate

    def coordinated_begin(session):
        if session.info.get("guard_race_lock_seen"):
            original_begin(session)
            return
        label = session.info["guard_race_label"]
        if label == "mutation":
            mutation_waiting.set()
        original_begin(session)
        session.info["guard_race_lock_seen"] = True
        if label == "baseline":
            baseline_locked.set()
            if not release_baseline.wait(timeout=5):
                raise AssertionError("baseline writer was not released")
        else:
            mutation_locked.set()

    monkeypatch.setattr(
        allocation_history, "begin_sqlite_immediate", coordinated_begin
    )
    monkeypatch.setattr(bank_accounts, "begin_sqlite_immediate", coordinated_begin)

    def capture():
        with Session(engine) as session:
            session.info["guard_race_label"] = "baseline"
            result = capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            return result

    def mutate():
        with Session(engine) as session:
            session.info["guard_race_label"] = "mutation"
            try:
                bank_accounts.recategorise_transaction(
                    session,
                    txn_id=1,
                    tax_code="none",
                    gst_amount=Decimal("0.00"),
                    gst_registered=True,
                )
            except AllocationHistoryConflict:
                return "conflict"
            return "changed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        capture_future = pool.submit(capture)
        try:
            assert baseline_locked.wait(timeout=5)
            mutation_future = pool.submit(mutate)
            assert mutation_waiting.wait(timeout=5)
            assert not mutation_locked.wait(timeout=0.25)
        finally:
            release_baseline.set()
        capture_future.result(timeout=10)
        assert mutation_future.result(timeout=10) == "conflict"

    assert _snapshot(engine) == before
    assert mutation_locked.is_set()
    engine.dispose()


@pytest.mark.parametrize("mutation_kind", ["allocation_clear", "import_new", "delete"])
def test_baseline_capture_serializes_with_allocation_import_and_delete(
    tmp_path, monkeypatch, mutation_kind
):
    engine = _database(tmp_path / f"baseline-{mutation_kind}-race.db")
    before = _snapshot(engine)
    baseline_locked = threading.Event()
    release_baseline = threading.Event()
    mutation_waiting = threading.Event()
    mutation_locked = threading.Event()
    original_begin = allocation_history.begin_sqlite_immediate

    def coordinated_begin(session):
        if session.info.get("allocation_race_lock_seen"):
            original_begin(session)
            return
        label = session.info["allocation_race_label"]
        if label == "mutation":
            mutation_waiting.set()
        original_begin(session)
        session.info["allocation_race_lock_seen"] = True
        if label == "baseline":
            baseline_locked.set()
            if not release_baseline.wait(timeout=5):
                raise AssertionError("baseline writer was not released")
        else:
            mutation_locked.set()

    for module in (allocation_history, invoice_payments, bank_accounts, bank_import):
        monkeypatch.setattr(module, "begin_sqlite_immediate", coordinated_begin)

    def capture():
        with Session(engine) as session:
            session.info["allocation_race_label"] = "baseline"
            result = capture_legacy_allocation_baseline(
                session, 1, datetime(2026, 10, 12)
            )
            session.commit()
            return result.baseline_event_id

    def mutate():
        with Session(engine) as session:
            session.info["allocation_race_label"] = "mutation"
            try:
                if mutation_kind == "allocation_clear":
                    transaction = BankTransaction(
                        id=1,
                        bank_account_id=1,
                        direction=BankTxnDirection.OUT,
                        amount=Decimal("10.00"),
                        occurred_at=date(2026, 10, 1),
                        invoice_allocations=[],
                    )
                    invoice_payments.replace_transaction_allocations(
                        session, transaction, []
                    )
                    return "changed"
                if mutation_kind == "delete":
                    bank_accounts.delete_manual_transaction(session, txn_id=1)
                    return "deleted"
                result = bank_import.commit_import(
                    session,
                    bank_account_id=1,
                    rows=[{
                        "occurred_at": date(2026, 10, 2),
                        "direction": "in",
                        "amount": Decimal("3.00"),
                        "dedup_key": "baseline-race-new-import",
                        "account_id": None,
                        "tax_code": "none",
                        "gst_amount": Decimal("0.00"),
                        "memo": "new concurrent import",
                        "counter_party_name": None,
                        "invoice_allocations": [],
                        "unapplied_account_id": None,
                    }],
                    gst_registered=True,
                )
                session.commit()
                return result
            except AllocationHistoryConflict as exc:
                session.commit()
                message = str(exc)
                assert "SQLite" not in message
                assert "trigger" not in message.lower()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        capture_future = pool.submit(capture)
        mutation_future = None
        try:
            assert baseline_locked.wait(timeout=5)
            mutation_future = pool.submit(mutate)
            assert mutation_waiting.wait(timeout=5)
            assert not mutation_locked.wait(timeout=0.25)
        finally:
            release_baseline.set()
        baseline_event_id = capture_future.result(timeout=10)
        assert mutation_future is not None
        mutation_result = mutation_future.result(timeout=10)

    assert baseline_event_id > 0
    after = _snapshot(engine)
    if mutation_kind == "import_new":
        assert mutation_result == {"created": 1, "skipped_duplicates": 0}
        assert len(after["bank_transactions"]) == 3
        assert after["bank_transactions"][:2] == before["bank_transactions"]
        for table in set(before) - {"bank_transactions"}:
            assert after[table] == before[table]
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM bank_transactions "
                "WHERE memo='new concurrent import'"
            )).scalar_one() == 1
    else:
        assert mutation_result == "conflict"
    assert mutation_locked.is_set()
    if mutation_kind != "import_new":
        assert after == before
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM allocation_history_events "
            "WHERE bank_transaction_id=1"
        )).scalar_one() == 1
        assert connection.execute(text(
            "SELECT COUNT(*) FROM bank_transactions"
        )).scalar_one() == (3 if mutation_kind == "import_new" else 2)
    engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    (
        "bank_transaction_id",
        "operation_reversal",
        "operation_reallocation",
        "effective_date",
        "recorded_at",
        "expected_prior_version",
        "reason",
        "evidence_reference",
        "evidence_sha256",
        "actor_placeholder",
        "linked_prior_batch_id",
        "allocation_line",
        "tax_components",
        "residual_cents",
        "residual_account_id",
        "baseline_finalization_id",
    ),
)
def test_batch_writer_conflicting_replay_intent_is_not_stale(tmp_path, mutation):
    from app.services.allocation_batches import (
        AllocationLineSpec,
        IdempotencyConflictError,
        write_allocation_correction,
        write_allocation_reallocation,
        write_allocation_reversal,
    )

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / f"conflict-{mutation}.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    request = _standard_writer_request(baseline_id, idempotency_key=f"conflict-{mutation}")
    with Session(engine) as session:
        write_allocation_correction(session, **request)
        session.commit()

    changed = dict(request)
    writer = write_allocation_correction
    if mutation == "bank_transaction_id":
        changed["bank_transaction_id"] = 1
    elif mutation == "operation_reversal":
        writer = write_allocation_reversal
    elif mutation == "operation_reallocation":
        writer = write_allocation_reallocation
    elif mutation == "effective_date":
        changed["effective_date"] = "2026-10-02"
    elif mutation == "recorded_at":
        changed["recorded_at"] = "2026-10-12T10:00:00.000001"
    elif mutation == "expected_prior_version":
        changed["expected_prior_version"] = 1
    elif mutation == "reason":
        changed["reason"] += " changed"
    elif mutation == "evidence_reference":
        changed["evidence_reference"] += "/changed"
    elif mutation == "evidence_sha256":
        changed["evidence_sha256"] = "a" * 64
    elif mutation == "actor_placeholder":
        changed["actor_placeholder"] += " changed"
    elif mutation == "linked_prior_batch_id":
        changed["linked_prior_batch_id"] = 10
    elif mutation == "allocation_line":
        changed["lines"] = [AllocationLineSpec("invoice", 2, 900, 90, (("standard", 900, 90),))]
    elif mutation == "tax_components":
        changed["lines"] = [AllocationLineSpec(
            "invoice", 2, 1000, 100, (("none", 100, 10), ("standard", 900, 90))
        )]
    elif mutation == "residual_cents":
        changed["residual_cents"] = 99
    elif mutation == "residual_account_id":
        changed["residual_account_id"] = None
    elif mutation == "baseline_finalization_id":
        changed["baseline_finalization_id"] += 1

    with Session(engine) as session:
        with pytest.raises(IdempotencyConflictError):
            writer(session, **changed)
        session.rollback()
    with Session(engine) as session:
        assert session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches WHERE bank_transaction_id=2), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == (1, 1, 1, 1)
    engine.dispose()


def test_batch_writer_new_stale_key_still_raises_stale_version(tmp_path):
    from app.services.allocation_batches import StaleVersionError, write_allocation_correction

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "stale-new-key.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    request = _standard_writer_request(baseline_id, idempotency_key="first-correction")
    with Session(engine) as session:
        write_allocation_correction(session, **request)
        session.commit()

    stale_request = {**request, "idempotency_key": "different-new-key"}
    with Session(engine) as session:
        with pytest.raises(StaleVersionError):
            write_allocation_correction(session, **stale_request)
        session.rollback()
    with Session(engine) as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM allocation_batches WHERE bank_transaction_id=2")
        ).scalar_one() == 1
    engine.dispose()


def test_batch_writer_does_not_replay_unfinalized_idempotency_owner(tmp_path):
    from app.services.allocation_batches import (
        AllocationLineSpec,
        InvalidAllocationStateError,
        write_allocation_correction,
    )

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "unfinalized-replay.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    line = {
        "source_kind": "invoice",
        "invoice_or_bill_id": 2,
        "gross_cents": 1000,
        "gst_cents": 100,
        "components": [{"tax_code": "standard", "gross_cents": 1000, "gst_cents": 100}],
    }
    with engine.begin() as connection:
        batch_id = _insert_allocation_batch(
            connection,
            baseline_id,
            idempotency_key="unfinalized-owner",
            residual_cents=100,
            residual_account_id=3,
            line=line,
        )
        _insert_allocation_batch_line(connection, batch_id, line)

    request = _standard_writer_request(baseline_id, idempotency_key="unfinalized-owner")
    request.update({
        "reason": "Synthetic transaction-wide allocation change",
        "evidence_reference": "synthetic://allocation-batch",
        "recorded_at": "2026-10-12 11:00:00",
        "lines": [AllocationLineSpec("invoice", 2, 1000, 100, (("standard", 1000, 100),))],
    })
    with Session(engine) as session:
        with pytest.raises(InvalidAllocationStateError, match="not finalized"):
            write_allocation_correction(session, **request)
        session.rollback()
    with Session(engine) as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM allocation_batches WHERE idempotency_key='unfinalized-owner'")
        ).scalar_one() == 1
        assert session.execute(
            text("SELECT COUNT(*) FROM allocation_batch_finalizations")
        ).scalar_one() == 0
    engine.dispose()


def test_batch_writer_rejects_inconsistent_finalization_on_replay(tmp_path):
    from unittest.mock import patch

    from app.services.allocation_batches import InvalidAllocationStateError, write_allocation_correction

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "inconsistent-replay.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    request = _standard_writer_request(baseline_id, idempotency_key="inconsistent-owner")
    with Session(engine) as session:
        write_allocation_correction(session, **request)
        session.commit()
    with Session(engine) as session:
        with patch("app.services.allocation_batches._finalization_integrity_hash", return_value="f" * 64):
            with pytest.raises(InvalidAllocationStateError, match="inconsistent"):
                write_allocation_correction(session, **request)
        session.rollback()
    with Session(engine) as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM allocation_batches WHERE idempotency_key='inconsistent-owner'")
        ).scalar_one() == 1
    engine.dispose()


def test_batch_writer_serializes_concurrent_identical_retries(tmp_path, monkeypatch):
    import app.services.allocation_batches as batches
    from app.services.allocation_batches import write_allocation_correction

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "concurrent-replay.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    request = _standard_writer_request(baseline_id, idempotency_key="concurrent-replay")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE invoices SET status='authorised', paid_amount=0, paid_date=NULL WHERE id=2")
        )
    first_acquired = threading.Event()
    second_at_boundary = threading.Event()
    second_acquired = threading.Event()
    release_first = threading.Event()
    begin_calls = []
    insert_calls = []
    original_begin = batches.begin_sqlite_immediate
    original_insert = batches._insert_batch_rows
    original_replay = batches._replay_existing_batch
    replay_calls = []

    def coordinated_begin(session):
        if session.info.get("writer_begin_seen"):
            return original_begin(session)
        session.info["writer_begin_seen"] = True
        label = session.info["writer_race_label"]
        begin_calls.append(label)
        if label == "second":
            second_at_boundary.set()
        original_begin(session)
        if label == "first":
            first_acquired.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first writer did not release its immediate lock")
        else:
            second_acquired.set()

    def count_insert(*args, **kwargs):
        insert_calls.append(True)
        return original_insert(*args, **kwargs)

    def count_replay(*args, **kwargs):
        replay_calls.append(True)
        return original_replay(*args, **kwargs)

    monkeypatch.setattr(batches, "begin_sqlite_immediate", coordinated_begin)
    monkeypatch.setattr(batches, "_insert_batch_rows", count_insert)
    monkeypatch.setattr(batches, "_replay_existing_batch", count_replay)

    def write_same_request(label):
        with Session(engine) as session:
            session.info["writer_race_label"] = label
            result = write_allocation_correction(session, **request)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(write_same_request, "first")
        assert first_acquired.wait(timeout=5), "first writer did not acquire BEGIN IMMEDIATE"
        second_future = pool.submit(write_same_request, "second")
        try:
            assert second_at_boundary.wait(timeout=5), "second writer did not reach BEGIN IMMEDIATE"
            assert not second_acquired.wait(timeout=0.25), (
                "second writer acquired BEGIN IMMEDIATE while first still held it"
            )
        finally:
            release_first.set()
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)
    assert first == second
    assert set(begin_calls) == {"first", "second"}
    assert len(insert_calls) == 1
    assert len(replay_calls) == 1
    with Session(engine) as session:
        assert session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches WHERE bank_transaction_id=2), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == (1, 1, 1, 1)
        assert session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one() == (10, "partial", "2026-10-01")
    engine.dispose()


@pytest.mark.parametrize(
    ("operation", "writer_name"),
    (
        ("allocation_correction", "write_allocation_correction"),
        ("allocation_reversal", "write_allocation_reversal"),
        ("allocation_reallocation", "write_allocation_reallocation"),
    ),
)
def test_allocation_writers_enforce_detached_period_lock_context(
    tmp_path, operation, writer_name
):
    from inspect import signature

    import app.services.allocation_batches as batches
    from app.services.allocation_batches import (
        InvalidAllocationStateError,
        PeriodLockedError,
    )

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / f"period-lock-{operation}.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    writer = getattr(batches, writer_name)
    request = _standard_writer_request(baseline_id, idempotency_key=f"lock-{operation}")
    assert "before_vector" not in signature(writer).parameters
    assert "internal_correction_approval_id" not in signature(writer).parameters
    cache_before = (Decimal("0"), "draft", None)
    with Session(engine) as session:
        cache_before = session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one()
        missing_context = dict(request)
        missing_context.pop("books_locked_through")
        with pytest.raises(InvalidAllocationStateError, match="books_locked_through"):
            writer(session, **missing_context)
        session.commit()

    locked_request = {
        **request,
        "books_locked_through": date(2026, 10, 1),
    }
    if operation == "allocation_correction":
        assert "internal_correction_approval_id" not in signature(writer).parameters
    with Session(engine) as session:
        with pytest.raises(PeriodLockedError):
            writer(session, **locked_request)
        session.commit()

    with Session(engine) as session:
        assert session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == (0, 0, 0, 0)
        assert session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one() == cache_before

    open_request = {
        **request,
        "books_locked_through": date(2026, 9, 30),
    }
    if operation == "allocation_correction":
        with Session(engine) as session:
            result = writer(session, **open_request)
            assert result.manual_review_status == "not_required"
            session.rollback()
    engine.dispose()


def test_legacy_current_projection_never_claims_historical_certainty(tmp_path):
    from app.services.allocation_projection import project_allocation_state

    engine = _database(tmp_path / "legacy-current-certainty.db")
    with Session(engine) as session:
        current = project_allocation_state(
            session, 2, date(2026, 10, 1), date(2026, 10, 12)
        )
        assert current.source == "legacy_current"
        assert current.certainty == "uncertain"
        assert current.uncertainty_reason == "historical_evidence_unavailable"
        assert current.ordered_allocation_lines

    with Session(engine) as session:
        before_effective = project_allocation_state(
            session, 2, date(2026, 9, 30), date(2026, 10, 12)
        )
        assert before_effective.source == "uncertain"
        assert before_effective.certainty == "uncertain"
        assert before_effective.uncertainty_reason == "historical_evidence_unavailable"
        assert before_effective.ordered_allocation_lines == ()

    with Session(engine) as session:
        before_knowledge = project_allocation_state(
            session, 2, date(2026, 10, 1), date(2026, 9, 30)
        )
        assert before_knowledge.source == "uncertain"
        assert before_knowledge.certainty == "uncertain"
        assert before_knowledge.uncertainty_reason == "historical_evidence_unavailable"
        assert before_knowledge.ordered_allocation_lines == ()
    engine.dispose()


def test_batch_cache_projects_removed_and_added_ar_and_ap_documents(tmp_path):
    from app.services.allocation_batches import (
        AllocationLineSpec,
        write_allocation_correction,
    )

    engine = _database(tmp_path / "projected-cache-ar-ap.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE invoices SET status='authorised', paid_amount=0, paid_date=NULL "
                "WHERE id IN (1, 2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO invoices (id, direction, contact_id, invoice_number, issue_date, "
                "currency, subtotal, gst_amount, total, gst_inclusive, status, paid_amount, "
                "source, created_at, updated_at) VALUES "
                "(3, 'AR', 1, 'SYN-AR-NEW', '2026-09-01', 'AUD', 10, 1, 11, 1, "
                "'authorised', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "(4, 'AP', 1, 'SYN-AP-NEW', '2026-09-01', 'AUD', 10, 1, 11, 1, "
                "'authorised', 0, 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    with Session(engine) as session:
        ap_baseline = capture_legacy_allocation_baseline(
            session, 1, datetime(2026, 10, 12, 10)
        )
        ar_baseline = capture_legacy_allocation_baseline(
            session, 2, datetime(2026, 10, 12, 10)
        )
        session.commit()

    with Session(engine) as session:
        original_allocations = session.execute(
            text(
                "SELECT id, bank_transaction_id, invoice_id, amount, gst_amount, created_at "
                "FROM invoice_payment_allocations ORDER BY id"
            )
        ).all()
        original_components = session.execute(
            text(
                "SELECT id, allocation_id, tax_code, gross_amount, gst_amount "
                "FROM invoice_payment_tax_components ORDER BY id"
            )
        ).all()
        original_transactions = session.execute(
            text(
                "SELECT id, amount, direction, occurred_at FROM bank_transactions "
                "WHERE id IN (1, 2) ORDER BY id"
            )
        ).all()

    requests = (
        (1, "bill", 4, ap_baseline.baseline_event_id, "ap-replacement"),
        (2, "invoice", 3, ar_baseline.baseline_event_id, "ar-replacement"),
    )
    for transaction_id, source_kind, new_document_id, baseline_id, key in requests:
        with Session(engine) as session:
            write_allocation_correction(
                session,
                bank_transaction_id=transaction_id,
                effective_date="2026-10-01",
                recorded_at="2026-10-13 00:00:00",
                idempotency_key=key,
                reason="Synthetic unchanged-vector document replacement",
                evidence_reference="synthetic://projected-cache",
                actor_placeholder="Synthetic operator",
                expected_prior_version=0,
                lines=[AllocationLineSpec(
                    source_kind, new_document_id, 1000, 100,
                    (("standard", 1000, 100),),
                )],
                residual_cents=0,
                residual_account_id=None,
                baseline_finalization_id=baseline_id,
                books_locked_through=None,
            )
            session.commit()

    with Session(engine) as session:
        states = {
            int(row.id): (Decimal(row.paid_amount), row.status, row.paid_date)
            for row in session.execute(
                text("SELECT id, paid_amount, status, paid_date FROM invoices WHERE id BETWEEN 1 AND 4")
            )
        }
        assert states[1] == (Decimal("0.00"), "authorised", None)
        assert states[2] == (Decimal("0.00"), "authorised", None)
        assert states[3][0] == Decimal("10.00")
        assert states[3][1] == "partial"
        assert str(states[3][2]) == "2026-10-01"
        assert states[4][0] == Decimal("10.00")
        assert states[4][1] == "partial"
        assert str(states[4][2]) == "2026-10-01"
        assert session.execute(
            text(
                "SELECT id, amount, direction, occurred_at FROM bank_transactions "
                "WHERE id IN (1, 2) ORDER BY id"
            )
        ).all() == original_transactions
        assert session.execute(
            text(
                "SELECT id, bank_transaction_id, invoice_id, amount, gst_amount, created_at "
                "FROM invoice_payment_allocations ORDER BY id"
            )
        ).all() == original_allocations
        assert session.execute(
            text(
                "SELECT id, allocation_id, tax_code, gross_amount, gst_amount "
                "FROM invoice_payment_tax_components ORDER BY id"
            )
        ).all() == original_components
    engine.dispose()


def test_batch_cache_failure_rolls_back_after_finalization_even_if_caller_commits(
    tmp_path,
):
    from unittest.mock import patch

    from app.services.allocation_batches import write_allocation_correction

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "projected-cache-rollback.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE invoices SET status='authorised', paid_amount=0, paid_date=NULL WHERE id=2")
        )
    request = _standard_writer_request(baseline_id, idempotency_key="cache-failure")
    cache_before = (Decimal("0.00"), "authorised", None)
    with Session(engine) as session:
        cache_before = session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one()

    with Session(engine) as session:
        def fail_after_finalization(db, _paid_cents_by_invoice):
            assert db.execute(
                text("SELECT COUNT(*) FROM allocation_batch_finalizations")
            ).scalar_one() == 1
            raise RuntimeError("synthetic projected-cache failure")

        with patch(
            "app.services.allocation_batches.refresh_projected_paid_state_cache",
            side_effect=fail_after_finalization,
        ):
            with pytest.raises(RuntimeError, match="projected-cache failure"):
                write_allocation_correction(session, **request)
        session.commit()

    with Session(engine) as observer:
        assert observer.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == (0, 0, 0, 0)
        assert observer.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one() == cache_before
    engine.dispose()


@pytest.mark.parametrize(
    "writer_name",
    (
        "write_allocation_correction",
        "write_allocation_reversal",
        "write_allocation_reallocation",
    ),
)
def test_allocation_writers_reject_changed_vectors_without_approval(
    tmp_path, writer_name
):
    import app.services.allocation_batches as batches
    from app.services.allocation_batches import (
        AllocationLineSpec,
        ManualReviewRequiredError,
    )

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / f"changed-vector-{writer_name}.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    request = _standard_writer_request(baseline_id, idempotency_key=f"changed-{writer_name}")
    request["lines"] = [
        AllocationLineSpec("invoice", 2, 900, 90, (("standard", 900, 90),))
    ]
    request["residual_cents"] = 200
    with Session(engine) as session:
        cache_before = session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one()

    writer = getattr(batches, writer_name)
    with Session(engine) as session:
        with pytest.raises(ManualReviewRequiredError):
            writer(session, **request)
        session.commit()

    with Session(engine) as session:
        assert session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batches), "
                "(SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == (0, 0, 0, 0)
        assert session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one() == cache_before
    engine.dispose()


def test_batch_writer_propagates_unexpected_replay_database_errors(tmp_path):
    from unittest.mock import patch

    from sqlalchemy.exc import OperationalError

    from app.services.allocation_batches import write_allocation_correction

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "replay-database-error.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    request = _standard_writer_request(baseline_id, idempotency_key="replay-database-error")
    with Session(engine) as session:
        write_allocation_correction(session, **request)
        session.commit()

    with Session(engine) as session:
        with patch(
            "app.services.allocation_batches._stored_request_payload_rows",
            side_effect=OperationalError("SELECT", {}, RuntimeError("connection lost")),
        ):
            with pytest.raises(OperationalError, match="connection lost"):
                write_allocation_correction(session, **request)
        session.rollback()
    engine.dispose()


def test_batch_writer_serializes_different_keys_at_same_version(tmp_path, monkeypatch):
    import app.services.allocation_batches as batches
    from app.services.allocation_batches import StaleVersionError, write_allocation_correction

    engine, baseline_id = _exact_bank_cash_writer_database(
        tmp_path / "concurrent-stale-version.db",
        bank_amount="11.00",
        allocation_amount="10.00",
        gst_amount="1.00",
        tax_code="standard",
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE invoices SET status='authorised', paid_amount=0, paid_date=NULL WHERE id=2")
        )
    requests = (
        _standard_writer_request(baseline_id, idempotency_key="concurrent-version-first"),
        _standard_writer_request(baseline_id, idempotency_key="concurrent-version-second"),
    )
    first_acquired = threading.Event()
    second_at_boundary = threading.Event()
    second_acquired = threading.Event()
    release_first = threading.Event()
    original_begin = batches.begin_sqlite_immediate
    insert_calls = []
    original_insert = batches._insert_batch_rows

    def coordinated_begin(session):
        if session.info.get("writer_begin_seen"):
            return original_begin(session)
        session.info["writer_begin_seen"] = True
        label = session.info["writer_race_label"]
        if label == "second":
            second_at_boundary.set()
        original_begin(session)
        if label == "first":
            first_acquired.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first writer did not release its immediate lock")
        else:
            second_acquired.set()

    def count_insert(*args, **kwargs):
        insert_calls.append(True)
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(batches, "begin_sqlite_immediate", coordinated_begin)
    monkeypatch.setattr(batches, "_insert_batch_rows", count_insert)

    def write(label, request):
        with Session(engine) as session:
            session.info["writer_race_label"] = label
            try:
                result = write_allocation_correction(session, **request)
                session.commit()
                return "success", result
            except StaleVersionError as exc:
                session.rollback()
                return "stale", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(write, "first", requests[0])
        assert first_acquired.wait(timeout=5), "first writer did not acquire BEGIN IMMEDIATE"
        second_future = pool.submit(write, "second", requests[1])
        try:
            assert second_at_boundary.wait(timeout=5), "second writer did not reach BEGIN IMMEDIATE"
            assert not second_acquired.wait(timeout=0.25), (
                "second writer acquired BEGIN IMMEDIATE while first still held it"
            )
        finally:
            release_first.set()
        outcomes = (first_future.result(timeout=10), second_future.result(timeout=10))

    assert [outcome[0] for outcome in outcomes] == ["success", "stale"]
    assert "locked" not in outcomes[1][1].lower()
    assert len(insert_calls) == 1
    with Session(engine) as session:
        assert session.execute(
            text(
                "SELECT COUNT(*), MIN(transaction_version), MAX(transaction_version) "
                "FROM allocation_batches WHERE bank_transaction_id=2"
            )
        ).one() == (1, 1, 1)
        assert session.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM allocation_batch_lines), "
                "(SELECT COUNT(*) FROM allocation_batch_tax_components), "
                "(SELECT COUNT(*) FROM allocation_batch_finalizations)"
            )
        ).one() == (1, 1, 1)
        assert session.execute(
            text("SELECT paid_amount, status, paid_date FROM invoices WHERE id=2")
        ).one() == (10, "partial", "2026-10-01")
    engine.dispose()


def test_projector_rejects_malformed_required_cutoffs(tmp_path):
    from app.services.allocation_projection import (
        UncertainProjectionError,
        project_allocation_state,
    )

    engine, _baseline_id = _allocation_batch_database(
        tmp_path / "projector-invalid-cutoffs.db"
    )
    valid_known = datetime(2026, 10, 12, 10)
    valid_effective = date(2026, 10, 1)
    invalid_cutoffs = (
        ("not-a-date", valid_known),
        (valid_effective, "not-a-timestamp"),
        ("", valid_known),
        (valid_effective, " "),
        ("2026-02-30", valid_known),
        (valid_effective, "2026-02-30"),
        (object(), valid_known),
        (valid_effective, object()),
        (0, valid_known),
        (valid_effective, 0),
        (None, valid_known),
        (valid_effective, None),
        ("2026-10-01T00:00:00+25:00", valid_known),
        (valid_effective, "2026-10-12T10:00:00+02:99"),
    )
    for effective_as_of, known_as_of in invalid_cutoffs:
        with Session(engine) as session:
            with pytest.raises(UncertainProjectionError):
                project_allocation_state(
                    session, 2, effective_as_of, known_as_of
                )
            session.rollback()
    engine.dispose()


def test_projector_normalizes_valid_naive_and_offset_cutoffs(tmp_path):
    from datetime import timedelta

    from app.services.allocation_projection import project_allocation_state

    engine, _baseline_id = _allocation_batch_database(
        tmp_path / "projector-valid-cutoffs.db"
    )
    effective_naive = datetime(2026, 10, 1, 0)
    known_naive = datetime(2026, 10, 12, 10)
    with Session(engine) as session:
        naive_projection = project_allocation_state(
            session, 2, effective_naive, known_naive
        )
        assert naive_projection.source == "complete_baseline"
        assert naive_projection.certainty == "complete"

    offset = timezone(timedelta(hours=2))
    with Session(engine) as session:
        offset_projection = project_allocation_state(
            session,
            2,
            datetime(2026, 10, 1, 2, tzinfo=offset),
            datetime(2026, 10, 12, 12, tzinfo=offset),
        )
        assert offset_projection.source == "complete_baseline"
        assert offset_projection.certainty == "complete"

    with Session(engine) as session:
        offset_string_projection = project_allocation_state(
            session,
            2,
            "2026-10-01T02:00:00+02:00",
            "2026-10-12T12:00:00+02:00",
        )
        assert offset_string_projection.source == "complete_baseline"
        assert offset_string_projection.certainty == "complete"

    with Session(engine) as session:
        before_boundary = project_allocation_state(
            session,
            2,
            datetime(2026, 10, 1, 1, tzinfo=offset),
            known_naive,
        )
        assert before_boundary.source == "uncertain"
        assert before_boundary.uncertainty_reason == "pre_baseline_operation"
    engine.dispose()


@pytest.mark.parametrize(
    "value",
    (
        True,
        1123.0,
        1123.5,
        Decimal("1123.5"),
        "1123.5",
        "1e3",
        Decimal("1e3"),
        "NaN",
        Decimal("NaN"),
        "Infinity",
        Decimal("Infinity"),
        -1,
        Decimal("-1"),
        "9223372036854775808",
        "-9223372036854775809",
        Decimal("9223372036854775808"),
        2**63,
        "001123",
        "+1123",
        "1123.0",
        " 1123",
        "1123 ",
        object(),
    ),
)
def test_stored_cent_validator_rejects_noncanonical_values(value):
    from app.services.allocation_projection import (
        UncertainProjectionError,
        _validate_stored_cents,
    )

    with pytest.raises(UncertainProjectionError):
        _validate_stored_cents(value, label="synthetic stored cents")


@pytest.mark.parametrize(
    ("value", "expected"),
    ((1123, 1123), (Decimal("1123"), 1123), (Decimal("1123.0"), 1123), ("1123", 1123)),
)
def test_stored_cent_validator_preserves_canonical_integral_values(value, expected):
    from app.services.allocation_projection import _validate_stored_cents

    assert _validate_stored_cents(value, label="synthetic stored cents") == expected


@pytest.mark.parametrize(
    ("scope", "query_fragment", "column", "value"),
    (
        ("baseline", "SELECT baseline_event_id, observed_payment_date", "observed_payment_date", "2026-02-30"),
        ("baseline", "SELECT baseline_event_id, observed_payment_date", "observed_at", "not-a-timestamp"),
        ("batch", "SELECT b.id, b.transaction_version", "effective_date", "2026-02-30"),
        ("batch", "SELECT b.id, b.transaction_version", "recorded_at", "not-a-timestamp"),
        ("batch", "SELECT b.id, b.transaction_version", "finalized_at", "2026-10-12T10:00:00+25:00"),
    ),
)
def test_projector_rejects_malformed_persisted_timestamps(
    tmp_path, monkeypatch, scope, query_fragment, column, value
):
    from app.services.allocation_projection import (
        UncertainProjectionError,
        project_allocation_state,
    )

    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"projector-malformed-time-{scope}-{column}.db"
    )
    if scope == "batch":
        with engine.begin() as connection:
            batch_id = _insert_finalized_allocation_batch(
                connection,
                baseline_id,
                idempotency_key="malformed-stored-time",
                line=_standard_batch_line(),
            )
    else:
        batch_id = None

    with Session(engine) as session:
        matched = _substitute_projector_read(
            monkeypatch,
            session,
            query_fragment=query_fragment,
            column=column,
            value=value,
        )
        with pytest.raises(UncertainProjectionError):
            project_allocation_state(
                session, 2, date(2026, 10, 1), datetime(2026, 10, 13, 12)
            )
        assert matched()
        session.rollback()
    engine.dispose()


@pytest.mark.parametrize(
    ("scope", "name", "query_fragment", "column", "bad_value", "mapped"),
    (
        (
            "baseline", "binding-gross", "FROM allocation_history_baseline_bindings b",
            "allocated_gross_cents", "1000.5", True,
        ),
        (
            "baseline", "binding-gst", "FROM allocation_history_baseline_bindings b",
            "allocated_gst_cents", "100.5", True,
        ),
        (
            "baseline", "component-gross", "SELECT tax_code, component_gross_cents",
            "component_gross_cents", "1000.5", False,
        ),
        (
            "baseline", "component-gst", "SELECT tax_code, component_gross_cents",
            "component_gst_cents", "100.5", False,
        ),
        (
            "baseline", "finalization-gross", "SELECT baseline_event_id, observed_payment_date",
            "gross_allocation_total_cents", "1000.5", True,
        ),
        (
            "baseline", "finalization-gst", "SELECT baseline_event_id, observed_payment_date",
            "gst_total_cents", "100.5", True,
        ),
        (
            "batch", "line-gross", "FROM allocation_batch_lines l",
            "gross_cents", "1000.5", True,
        ),
        (
            "batch", "line-gst", "FROM allocation_batch_lines l",
            "gst_cents", "100.5", True,
        ),
        (
            "batch", "component-gross", "FROM allocation_batch_lines l",
            "component_gross_cents", "1000.5", True,
        ),
        (
            "batch", "component-gst", "FROM allocation_batch_lines l",
            "component_gst_cents", "100.5", True,
        ),
        (
            "batch", "residual-amount", "SELECT b.id, b.transaction_version",
            "residual_amount_cents", "200.5", True,
        ),
        (
            "batch", "finalization-gross", "SELECT b.id, b.transaction_version",
            "total_allocated_cents", "1000.5", True,
        ),
        (
            "batch", "finalization-gst", "SELECT b.id, b.transaction_version",
            "total_gst_cents", "100.5", True,
        ),
        (
            "batch", "finalization-residual", "SELECT b.id, b.transaction_version",
            "finalized_residual_cents", "200.5", True,
        ),
        (
            "batch", "finalization-bank-cash", "SELECT b.id, b.transaction_version",
            "bank_cash_cents", "1200.5", True,
        ),
    ),
)
def test_projector_rejects_fractional_stored_cent_sources(
    tmp_path, monkeypatch, scope, name, query_fragment, column, bad_value, mapped
):
    from app.services.allocation_projection import (
        UncertainProjectionError,
        project_allocation_state,
    )

    engine, baseline_id = _allocation_batch_database(
        tmp_path / f"projector-fractional-cents-{scope}-{name}.db"
    )
    if scope == "batch":
        with engine.begin() as connection:
            stored_id = _insert_finalized_allocation_batch(
                connection,
                baseline_id,
                idempotency_key=f"fractional-{name}",
                line=_standard_batch_line(),
            )
    else:
        stored_id = baseline_id

    with Session(engine) as session:
        matched = _substitute_projector_read(
            monkeypatch,
            session,
            query_fragment=query_fragment,
            column=column,
            value=bad_value,
            mapped=mapped,
        )
        with pytest.raises(UncertainProjectionError):
            project_allocation_state(
                session, 2, date(2026, 10, 1), datetime(2026, 10, 13, 12)
            )
        assert matched()
        session.rollback()
    engine.dispose()


def test_projector_does_not_hide_malformed_future_batch_cents_behind_baseline(
    tmp_path, monkeypatch
):
    from app.services.allocation_projection import (
        UncertainProjectionError,
        project_allocation_state,
    )

    engine, baseline_id = _allocation_batch_database(
        tmp_path / "projector-future-malformed-cent.db"
    )
    with engine.begin() as connection:
        batch_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            idempotency_key="future-malformed-cent",
            line=_standard_batch_line(),
        )

    with Session(engine) as session:
        matched = _substitute_projector_read(
            monkeypatch,
            session,
            query_fragment="FROM allocation_batch_lines l",
            column="component_gross_cents",
            value="1000.5",
        )
        with pytest.raises(UncertainProjectionError):
            project_allocation_state(
                session, 2, date(2026, 10, 1), datetime(2026, 10, 12, 10, 30)
            )
        assert matched()
        session.rollback()
    engine.dispose()


def test_projector_rejects_malformed_unfinalized_batch_timestamp(tmp_path, monkeypatch):
    from app.services.allocation_projection import (
        UncertainProjectionError,
        project_allocation_state,
    )

    engine, baseline_id = _allocation_batch_database(
        tmp_path / "projector-unfinalized-malformed-time.db"
    )
    with engine.begin() as connection:
        batch_id = _insert_allocation_batch(
            connection,
            baseline_id,
            idempotency_key="unfinalized-malformed-time",
        )

    with Session(engine) as session:
        matched = _substitute_projector_read(
            monkeypatch,
            session,
            query_fragment="SELECT b.id, b.transaction_version",
            column="recorded_at",
            value="not-a-timestamp",
        )
        with pytest.raises(UncertainProjectionError):
            project_allocation_state(
                session, 2, date(2026, 10, 1), datetime(2026, 10, 13, 12)
            )
        assert matched()
        session.rollback()
    engine.dispose()


def test_projector_test_fixtures_preserve_append_only_database_guards(tmp_path):
    engine, baseline_id = _allocation_batch_database(
        tmp_path / "projector-append-only-guards.db"
    )
    with engine.begin() as connection:
        batch_id = _insert_finalized_allocation_batch(
            connection,
            baseline_id,
            idempotency_key="append-only-projector-guard",
            line=_standard_batch_line(),
        )

    with pytest.raises(IntegrityError, match="Allocation history is append-only"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE allocation_history_baseline_bindings "
                    "SET allocated_gross_cents=999.5 WHERE baseline_event_id=:id"
                ),
                {"id": baseline_id},
            )

    with pytest.raises(IntegrityError, match="Allocation batches are append-only"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE allocation_batches SET reason='mutated' WHERE id=:id"),
                {"id": batch_id},
            )
    engine.dispose()