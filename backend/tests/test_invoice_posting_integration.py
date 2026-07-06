from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import CompanyBase
import app.models.company as company_models
from app.models.company import Account, Attachment, Contact, Invoice, InvoiceLine, InvoiceStatus
from app.services.chart_of_accounts import seed_default_coa
from app.services.invoice_posting import post_invoice, void_invoice
from app.services.reports import profit_and_loss
from app.services.trial_balance import trial_balance

KEEP_SQLALCHEMY_MODELS = tuple(
    value for value in vars(company_models).values() if isinstance(value, type)
)


def _session():
    engine = create_engine("sqlite://", future=True)
    CompanyBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = SessionLocal()
    seed_default_coa(db)
    return db


def _accounts(db):
    return {a.code: a for a in db.query(Account).all()}


def _invoice(db, accounts, *, direction, number, subtotal, gst, total, account_code):
    c = Contact(name=f"{direction} Contact {number}", kind="customer" if direction == "AR" else "supplier")
    db.add(c)
    db.flush()
    inv = Invoice(
        direction=direction,
        contact_id=c.id,
        invoice_number=number,
        issue_date=date(2026, 5, 31),
        subtotal=Decimal(subtotal),
        gst_amount=Decimal(gst),
        total=Decimal(total),
        gst_inclusive=True,
        status=InvoiceStatus.DRAFT,
    )
    inv.lines.append(
        InvoiceLine(
            description=number,
            account_id=accounts[account_code].id,
            quantity=Decimal("1"),
            unit_price=Decimal(subtotal),
            gst_rate=Decimal("0.10"),
            line_subtotal=Decimal(subtotal),
            line_gst=Decimal(gst),
            line_total=Decimal(total),
        )
    )
    db.add(inv)
    db.flush()
    return inv


def _by_code(tb):
    return {r["code"]: r for r in tb["rows"] if r["kind"] == "account"}


def _snapshot(tb):
    rows = _by_code(tb)
    return {
        code: {
            "debit_total": rows[code]["debit_total"],
            "credit_total": rows[code]["credit_total"],
            "net_debit": rows[code]["net_debit"],
        }
        for code in ["1100", "1200", "2000", "2100", "4000"]
        if code in rows
    } | {
        "total_debit": tb["total_debit"],
        "total_credit": tb["total_credit"],
        "is_balanced": tb["is_balanced"],
    }


def test_invoice_postings_feed_trial_balance_and_void_reverses_one():
    db = _session()
    try:
        accounts = _accounts(db)
        ar1 = _invoice(db, accounts, direction="AR", number="AR-110", subtotal="100.00", gst="10.00", total="110.00", account_code="4000")
        ar2 = _invoice(db, accounts, direction="AR", number="AR-220", subtotal="200.00", gst="20.00", total="220.00", account_code="4000")
        ar3 = _invoice(db, accounts, direction="AR", number="AR-330", subtotal="300.00", gst="30.00", total="330.00", account_code="4000")
        ap1 = _invoice(db, accounts, direction="AP", number="AP-55", subtotal="50.00", gst="5.00", total="55.00", account_code="6100")
        ap2 = _invoice(db, accounts, direction="AP", number="AP-77", subtotal="70.00", gst="7.00", total="77.00", account_code="6400")
        for inv in [ar1, ar2, ar3, ap1, ap2]:
            post_invoice(db, inv.id)

        tb = trial_balance(db)
        rows = _by_code(tb)
        assert rows["1100"]["debit_total"] == Decimal("660.00")
        assert rows["2000"]["credit_total"] == Decimal("132.00")
        assert rows["4000"]["credit_total"] == Decimal("600.00")
        assert rows["2100"]["credit_total"] == Decimal("60.00")
        assert rows["1200"]["debit_total"] == Decimal("12.00")
        assert tb["total_debit"] == tb["total_credit"]

        void_invoice(db, ar1.id)
        tb = trial_balance(db)
        rows = _by_code(tb)
        assert rows["1100"]["net_debit"] == Decimal("550.00")
        assert -rows["4000"]["net_debit"] == Decimal("500.00")
        assert -rows["2100"]["net_debit"] == Decimal("50.00")
        print("INTEGRATION_TB_AFTER_VOID", _snapshot(tb))
    finally:
        db.close()


def test_void_reversal_dated_to_original_entry_nets_prior_period_to_zero():
    """Round-3 P1: a void of a prior-period invoice must not leave phantom
    revenue in that period. The reversal JE is dated to the ORIGINAL posting's
    entry_date (issue date), so the prior month's P&L nets to zero even though
    the void is recorded "today".
    """
    db = _session()
    try:
        accounts = _accounts(db)
        # Invoice issued in a prior month (issue_date 2026-05-31).
        inv = _invoice(
            db, accounts, direction="AR", number="AR-VOID",
            subtotal="1000.00", gst="100.00", total="1100.00", account_code="4000",
        )
        original = post_invoice(db, inv.id)
        assert original.entry_date == date(2026, 5, 31)

        # Void it (the reversal must inherit the original's date, not now()).
        reversal = void_invoice(db, inv.id)
        assert reversal.entry_date == original.entry_date == date(2026, 5, 31)

        # P&L over the issue month nets to zero: the original + reversal cancel.
        pnl = profit_and_loss(
            db, period_start=date(2026, 5, 1), period_end=date(2026, 5, 31)
        )
        assert pnl["total_income"] == Decimal("0")
        assert pnl["net_profit"] == Decimal("0")
    finally:
        db.close()
