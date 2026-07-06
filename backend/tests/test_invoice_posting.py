from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import CompanyBase
import app.models.company as company_models
from app.models.company import (
    Account,
    Attachment,
    Contact,
    Invoice,
    InvoiceDirection,
    InvoiceLine,
    InvoiceStatus,
    JournalEntry,
    JournalEntrySource,
)
from app.schemas.journal import JournalEntryCreate, JournalEntryUpdate, JournalLineCreate
from app.services import invoice_posting, journal
from app.services.chart_of_accounts import seed_default_coa
from app.services.trial_balance import trial_balance

KEEP_SQLALCHEMY_MODELS = tuple(
    value for value in vars(company_models).values() if isinstance(value, type)
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    CompanyBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as db:
        seed_default_coa(db)
        yield db


@pytest.fixture()
def accounts(session: Session):
    return {a.code: a for a in session.query(Account).all()}


def _contact(session: Session, name="Acme Pty Ltd") -> Contact:
    c = Contact(name=name, kind="customer")
    session.add(c)
    session.flush()
    return c


def _invoice(
    session: Session,
    accounts,
    *,
    direction="AR",
    number="INV-001",
    subtotal=Decimal("100.00"),
    gst=Decimal("10.00"),
    total=Decimal("110.00"),
    account_codes=("4000",),
) -> Invoice:
    c = _contact(session, "Customer" if direction == "AR" else "Supplier")
    inv = Invoice(
        direction=direction,
        contact_id=c.id,
        invoice_number=number,
        issue_date=date(2026, 5, 31),
        subtotal=subtotal,
        gst_amount=gst,
        total=total,
        gst_inclusive=True,
        status=InvoiceStatus.DRAFT,
    )
    per_line = (subtotal / Decimal(len(account_codes))).quantize(Decimal("0.01"))
    for idx, code in enumerate(account_codes, start=1):
        amount = per_line if idx < len(account_codes) else subtotal - per_line * (len(account_codes) - 1)
        line_gst = (amount * (gst / subtotal)).quantize(Decimal("0.01")) if subtotal else Decimal("0.00")
        inv.lines.append(
            InvoiceLine(
                description=f"Line {idx}",
                account_id=accounts[code].id if code is not None else None,
                quantity=Decimal("1"),
                unit_price=amount,
                gst_rate=Decimal("0.10") if gst else Decimal("0"),
                line_subtotal=amount,
                line_gst=line_gst,
                line_total=amount + line_gst,
            )
        )
    session.add(inv)
    session.flush()
    return inv


def _lines_by_code(session: Session, entry: JournalEntry):
    accounts = {a.id: a.code for a in session.query(Account).all()}
    return [(accounts[l.account_id], l.debit_amount, l.credit_amount) for l in entry.lines]


def test_ar_posting_happy_path(session, accounts):
    inv = _invoice(session, accounts, account_codes=("4000", "4010"))
    entry = invoice_posting.post_invoice(session, inv.id)

    assert inv.status == InvoiceStatus.AUTHORISED
    assert inv.authorised_at is not None
    assert entry.source_type == JournalEntrySource.INVOICE_AR
    assert entry.source_id == inv.id
    assert len(entry.lines) == 4
    assert sum(l.debit_amount for l in entry.lines) == inv.total
    assert sum(l.credit_amount for l in entry.lines) == inv.total
    by_code = _lines_by_code(session, entry)
    assert ("1100", Decimal("110.00"), Decimal("0.00")) in by_code
    assert sum(c for code, _, c in by_code if code in {"4000", "4010"}) == inv.subtotal
    assert ("2100", Decimal("0.00"), Decimal("10.00")) in by_code


def test_ap_posting_happy_path(session, accounts):
    inv = _invoice(
        session, accounts, direction="AP", number="BILL-001", account_codes=("6100", "6400")
    )
    entry = invoice_posting.post_invoice(session, inv.id)

    assert entry.source_type == JournalEntrySource.INVOICE_AP
    by_code = _lines_by_code(session, entry)
    assert sum(d for code, d, _ in by_code if code in {"6100", "6400"}) == inv.subtotal
    assert ("1200", Decimal("10.00"), Decimal("0.00")) in by_code
    assert ("2000", Decimal("0.00"), Decimal("110.00")) in by_code


def test_gst_free_invoice_omits_gst_line(session, accounts):
    inv = _invoice(session, accounts, gst=Decimal("0.00"), total=Decimal("100.00"))
    entry = invoice_posting.post_invoice(session, inv.id)
    assert len(entry.lines) == 2
    assert "2100" not in {code for code, _, _ in _lines_by_code(session, entry)}


def test_missing_line_account_raises_without_state_change(session, accounts):
    inv = _invoice(session, accounts, account_codes=(None,))
    with pytest.raises(invoice_posting.MissingAccount):
        invoice_posting.post_invoice(session, inv.id)
    assert inv.status == InvoiceStatus.DRAFT
    assert session.query(JournalEntry).count() == 0


def test_missing_control_account_raises(session, accounts):
    session.delete(accounts["1100"])
    session.flush()
    inv = _invoice(session, accounts)
    with pytest.raises(invoice_posting.MissingControlAccount):
        invoice_posting.post_invoice(session, inv.id)


def test_double_post_raises(session, accounts):
    inv = _invoice(session, accounts)
    invoice_posting.post_invoice(session, inv.id)
    with pytest.raises(invoice_posting.AlreadyPosted):
        invoice_posting.post_invoice(session, inv.id)


def test_void_happy_path_reverses_original(session, accounts):
    inv = _invoice(session, accounts)
    original = invoice_posting.post_invoice(session, inv.id)
    original_lines = [(l.account_id, l.debit_amount, l.credit_amount) for l in original.lines]
    reversal = invoice_posting.void_invoice(session, inv.id)

    assert reversal.source_type == JournalEntrySource.INVOICE_REVERSAL
    assert reversal.reverses_entry_id == original.id
    assert inv.status == InvoiceStatus.VOID
    assert [(l.account_id, l.credit_amount, l.debit_amount) for l in reversal.lines] == original_lines
    tb = trial_balance(session)
    ar_row = next(r for r in tb["rows"] if r["code"] == "1100")
    assert ar_row["net_debit"] == Decimal("0.00")


def test_void_of_non_posted_invoice_raises(session, accounts):
    inv = _invoice(session, accounts)
    with pytest.raises(invoice_posting.InvalidTransition):
        invoice_posting.void_invoice(session, inv.id)


def test_double_void_raises(session, accounts):
    inv = _invoice(session, accounts)
    invoice_posting.post_invoice(session, inv.id)
    invoice_posting.void_invoice(session, inv.id)
    inv.status = InvoiceStatus.AUTHORISED
    with pytest.raises(invoice_posting.AlreadyVoided):
        invoice_posting.void_invoice(session, inv.id)


def test_posted_entry_is_locked_but_manual_entry_still_editable(session, accounts):
    inv = _invoice(session, accounts)
    entry = invoice_posting.post_invoice(session, inv.id)
    with pytest.raises(journal.JournalLocked):
        journal.update_entry(session, entry.id, JournalEntryUpdate(memo="nope"))
    with pytest.raises(journal.JournalLocked):
        journal.delete_entry(session, entry.id)

    manual = journal.create_entry(
        session,
        JournalEntryCreate(
            entry_date=date(2026, 5, 31),
            memo="Manual",
            lines=[
                JournalLineCreate(account_id=accounts["1000"].id, debit_amount=Decimal("1.00")),
                JournalLineCreate(account_id=accounts["3000"].id, credit_amount=Decimal("1.00")),
            ],
        ),
    )
    updated = journal.update_entry(session, manual.id, JournalEntryUpdate(memo="Manual edited"))
    assert updated.memo == "Manual edited"
    assert journal.delete_entry(session, manual.id) is True
