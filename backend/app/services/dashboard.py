"""Dashboard aggregation service.

Single read-only function that returns everything the home page needs in one
trip: KPIs, current-month trend, recent business transactions, recent unpaid AP.

All money values are Decimal — Pydantic serialises to string on the way out.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    BankAccount,
    BankTransaction,
    BankTxnDirection,
    Invoice,
    InvoiceDirection,
    InvoiceStatus,
)
from .reports import (
    _au_fy_quarter_bounds,  # type: ignore[reportPrivateUsage]
    profit_and_loss,
)
from .bank_accounts import bank_account_balance
from .trial_balance import trial_balance


ZERO = Decimal("0")


def _fy_to_date_bounds(today: date) -> tuple[date, date]:
    """Australian financial year-to-date: Jul 1 of current FY through today."""
    if today.month >= 7:
        return date(today.year, 7, 1), today
    return date(today.year - 1, 7, 1), today


def _current_month_bounds(today: date) -> tuple[date, date]:
    # Month-to-DATE: from the 1st through today. Ending at the last day of the
    # month would fold future-dated entries (e.g. 15/06 while today is 03/06)
    # into this month's income, overstating it.
    return date(today.year, today.month, 1), today


def dashboard_summary(db: Session, *, today: date | None = None) -> dict:
    today = today or date.today()
    fy_start, fy_end = _fy_to_date_bounds(today)
    month_start, month_end = _current_month_bounds(today)

    # --- Bank balances --------------------------------------------------
    bank_rows: list[dict] = []
    business_total = ZERO
    for ba in db.query(BankAccount).filter(BankAccount.is_active.is_(True)).all():
        bal = bank_account_balance(db, ba)
        bank_rows.append(
            {
                "id": ba.id,
                "name": ba.name,
                "balance": bal,
            }
        )
        business_total += bal

    # --- Unpaid AP ------------------------------------------------------
    unpaid_ap_total = ZERO
    overdue_ap_count = 0
    unpaid_invoices = (
        db.query(Invoice)
        .filter(
            Invoice.direction == InvoiceDirection.AP,
            # AUTHORISED = posted but not yet paid → still outstanding AP.
            # (Draft = not posted; paid/void = settled/cancelled.)
            Invoice.status.in_(
                [InvoiceStatus.AUTHORISED, InvoiceStatus.UNPAID, InvoiceStatus.PARTIAL]
            ),
        )
        .order_by(Invoice.due_date.asc().nullslast(), Invoice.issue_date.asc())
        .all()
    )
    recent_ap: list[dict] = []
    for inv in unpaid_invoices:
        outstanding = (inv.total or ZERO) - (inv.paid_amount or ZERO)
        if outstanding <= 0:
            continue
        unpaid_ap_total += outstanding
        if inv.due_date is not None and inv.due_date < today:
            overdue_ap_count += 1
        if len(recent_ap) < 5:
            recent_ap.append(
                {
                    "id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "contact_name": inv.contact.name if inv.contact else None,
                    "issue_date": inv.issue_date,
                    "due_date": inv.due_date,
                    "total": inv.total,
                    "outstanding": outstanding,
                    "is_overdue": inv.due_date is not None and inv.due_date < today,
                }
            )

    # --- P&L FY-to-date -------------------------------------------------
    pnl_fy = profit_and_loss(db, period_start=fy_start, period_end=fy_end)
    pnl_month = profit_and_loss(db, period_start=month_start, period_end=month_end)

    # --- Trial balance health (M2.2) -----------------------------------
    tb = trial_balance(db, as_of=today)

    # --- Recent bank transactions --------------------------------------
    biz_account_ids = [ba.id for ba in db.query(BankAccount).all()]
    recent_txns: list[dict] = []
    if biz_account_ids:
        txns = (
            db.query(BankTransaction)
            .filter(BankTransaction.bank_account_id.in_(biz_account_ids))
            .order_by(BankTransaction.occurred_at.desc(), BankTransaction.id.desc())
            .limit(8)
            .all()
        )
        for t in txns:
            acc = db.get(Account, t.account_id) if t.account_id else None
            recent_txns.append(
                {
                    "id": t.id,
                    "occurred_at": t.occurred_at,
                    "direction": t.direction.value
                    if hasattr(t.direction, "value")
                    else str(t.direction),
                    "amount": t.amount,
                    "memo": t.memo,
                    "counter_party_name": t.counter_party_name,
                    "account_code": acc.code if acc else None,
                    "account_name": acc.name if acc else None,
                }
            )

    # --- BAS quarter context (for the GST-not-registered banner) -------
    # Use AU fiscal year (ending year) for the quarter that contains `today`.
    # FY2026 = Jul 2025 – Jun 2026; in Jul–Dec 2025 the FY-ending year is 2026.
    fy_year = today.year + 1 if today.month >= 7 else today.year
    if today.month in (7, 8, 9):
        quarter = 1
    elif today.month in (10, 11, 12):
        quarter = 2
    elif today.month in (1, 2, 3):
        quarter = 3
    else:
        quarter = 4
    q_start, q_end = _au_fy_quarter_bounds(fy_year, quarter)

    return {
        "as_of": today,
        "fy_year": fy_year,
        "fy_period": {"start": fy_start, "end": fy_end},
        "current_month": {"start": month_start, "end": month_end},
        "current_quarter": {"fy_year": fy_year, "quarter": quarter, "start": q_start, "end": q_end},
        "bank_accounts": bank_rows,
        "business_total": business_total,
        "unpaid_ap_total": unpaid_ap_total,
        "overdue_ap_count": overdue_ap_count,
        "fy_net_profit": pnl_fy["net_profit"],
        "fy_total_income": pnl_fy["total_income"],
        "fy_total_expense": pnl_fy["total_expense"] + pnl_fy["total_cogs"],
        "month_income": pnl_month["total_income"],
        "month_expense": pnl_month["total_expense"] + pnl_month["total_cogs"],
        "month_uncategorised_in": pnl_month["uncategorised_in"],
        "month_uncategorised_out": pnl_month["uncategorised_out"],

        "tb_balanced": tb["is_balanced"],
        "tb_diff": tb["diff"],
        "tb_uncategorised_in": tb["uncategorised_bank_in"],
        "tb_uncategorised_out": tb["uncategorised_bank_out"],
        "recent_business_txns": recent_txns,
        "unpaid_ap": recent_ap,
    }
