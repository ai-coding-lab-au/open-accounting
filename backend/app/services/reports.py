"""Reporting service (M3).

Pure read-side: every function takes a SQLAlchemy Session and returns
plain dicts (or domain objects) that map cleanly onto Pydantic out-schemas.
No writes happen here.

Three reports today:

1. Monthly bank statement (one bank account, one month).
2. Profit & loss (bank-account-driven; classified by Account.type).
3. BAS — Australian quarterly GST return. The structure is in place but
   the firm is not GST-registered yet, so GST fields default to zero.

Design notes:
  - We treat the bank account as the source of truth for operating cash.
    P&L is derived from BankTransaction.account_id +
    direction (no separate Journal yet — that's M4 if ever).
  - All money is Decimal. Frontend serialises via Pydantic to string.
  - Date ranges are inclusive on both ends.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    AccountType,
    BankAccount,
    BankTransaction,
    BankTxnDirection,
)
ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _signed_amount(direction: BankTxnDirection, amount: Decimal) -> Decimal:
    return amount if direction == BankTxnDirection.IN else -amount


def _au_fy_quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    """`year` is the AU financial year ending year (FY2026 = Jul 2025 → Jun 2026).
    Q1=Jul-Sep, Q2=Oct-Dec, Q3=Jan-Mar, Q4=Apr-Jun.
    """
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter must be 1..4")
    if quarter == 1:
        start = date(year - 1, 7, 1)
        end = date(year - 1, 9, 30)
    elif quarter == 2:
        start = date(year - 1, 10, 1)
        end = date(year - 1, 12, 31)
    elif quarter == 3:
        start = date(year, 1, 1)
        end = date(year, 3, 31)
    else:
        start = date(year, 4, 1)
        end = date(year, 6, 30)
    return start, end


# ---------------------------------------------------------------------------
# 1) Monthly bank statement
# ---------------------------------------------------------------------------


def bank_statement(
    db: Session,
    *,
    bank_account_id: int,
    year: int,
    month: int,
) -> dict:
    """Returns: opening, closing, total_in, total_out, rows[].

    Opening balance = (account.opening_balance) + signed sum of all txns
    strictly before period_start.
    Closing balance = opening + signed sum within [start, end].
    """
    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise ValueError(f"Bank account {bank_account_id} not found")

    period_start, period_end = _month_bounds(year, month)

    # Opening balance: bank.opening_balance + everything strictly before period_start.
    prior_rows = db.execute(
        select(BankTransaction.direction, func.sum(BankTransaction.amount))
        .where(
            BankTransaction.bank_account_id == bank.id,
            BankTransaction.occurred_at < period_start,
        )
        .group_by(BankTransaction.direction)
    ).all()
    opening = bank.opening_balance
    for direction, total in prior_rows:
        if total is None:
            continue
        opening += _signed_amount(BankTxnDirection(direction), Decimal(total))

    # In-period rows.
    rows = (
        db.query(BankTransaction)
        .filter(
            BankTransaction.bank_account_id == bank.id,
            BankTransaction.occurred_at >= period_start,
            BankTransaction.occurred_at <= period_end,
        )
        .order_by(BankTransaction.occurred_at.asc(), BankTransaction.id.asc())
        .all()
    )

    total_in = ZERO
    total_out = ZERO
    running = opening
    enriched: list[dict] = []
    account_cache: dict[int, Account] = {}
    for txn in rows:
        signed = _signed_amount(txn.direction, txn.amount)
        running += signed
        if txn.direction == BankTxnDirection.IN:
            total_in += txn.amount
        else:
            total_out += txn.amount
        acc = None
        if txn.account_id is not None:
            acc = account_cache.get(txn.account_id)
            if acc is None:
                acc = db.get(Account, txn.account_id)
                if acc is not None:
                    account_cache[txn.account_id] = acc
        unapplied_acc = None
        if txn.unapplied_account_id is not None:
            unapplied_acc = account_cache.get(txn.unapplied_account_id)
            if unapplied_acc is None:
                unapplied_acc = db.get(Account, txn.unapplied_account_id)
                if unapplied_acc is not None:
                    account_cache[txn.unapplied_account_id] = unapplied_acc
        enriched.append(
            {
                "id": txn.id,
                "occurred_at": txn.occurred_at,
                "direction": txn.direction.value
                if hasattr(txn.direction, "value")
                else txn.direction,
                "amount": txn.amount,
                "gst_amount": txn.gst_amount,
                "memo": txn.memo,
                "counter_party_name": txn.counter_party_name,
                "account_code": acc.code if acc else None,
                "account_name": acc.name if acc else None,
                "unapplied_account_code": (
                    unapplied_acc.code if unapplied_acc else None
                ),
                "unapplied_account_name": (
                    unapplied_acc.name if unapplied_acc else None
                ),
                "unapplied_amount": txn.unapplied_amount,
                "running_balance": running,
            }
        )

    return {
        "bank_account_id": bank.id,
        "bank_account_name": bank.name,
        "year": year,
        "month": month,
        "period_start": period_start,
        "period_end": period_end,
        "opening_balance": opening,
        "closing_balance": running,
        "total_in": total_in,
        "total_out": total_out,
        "net_change": running - opening,
        "rows": enriched,
    }


# ---------------------------------------------------------------------------
# 2) Profit & Loss
# ---------------------------------------------------------------------------


_INCOME_TYPES = {AccountType.INCOME}
_EXPENSE_TYPES = {AccountType.EXPENSE, AccountType.COST_OF_SALES}


def profit_and_loss(
    db: Session,
    *,
    period_start: date,
    period_end: date,
) -> dict:
    """P&L derived from business-bank-account transactions, grouped by Account.

    Convention while we don't have journals:
      - Money IN to a business account that's categorised against an INCOME
        account counts as revenue.
      - Money OUT categorised against EXPENSE / COST_OF_SALES counts as cost.
      - Uncategorised txns are bucketed under "Uncategorised" so they're visible
        and the user can fix them.
    """
    if period_start > period_end:
        raise ValueError("period_start must be <= period_end")

    biz_account_ids = [a.id for a in db.query(BankAccount).all()]
    if not biz_account_ids:
        return _empty_pnl(period_start, period_end)

    txns = (
        db.query(BankTransaction)
        .filter(
            BankTransaction.bank_account_id.in_(biz_account_ids),
            BankTransaction.occurred_at >= period_start,
            BankTransaction.occurred_at <= period_end,
        )
        .all()
    )

    income_by_acc: dict[int | None, Decimal] = {}
    expense_by_acc: dict[int | None, Decimal] = {}
    cogs_by_acc: dict[int | None, Decimal] = {}
    uncategorised_in = ZERO
    uncategorised_out = ZERO

    account_cache: dict[int, Account] = {}

    def _acc(aid: int) -> Account | None:
        if aid not in account_cache:
            a = db.get(Account, aid)
            if a is not None:
                account_cache[aid] = a
            else:
                return None
        return account_cache[aid]

    for t in txns:
        if t.account_id is None:
            if t.direction == BankTxnDirection.IN:
                uncategorised_in += t.amount
            else:
                uncategorised_out += t.amount
            continue
        acc = _acc(t.account_id)
        if acc is None:
            continue
        net_amount = t.amount - (t.gst_amount or ZERO)
        # Cross-type rows count as contras so the P&L agrees with the trial
        # balance (which posts the contra leg for EVERY categorised txn):
        # IN on an expense/COGS account is a refund netting cost down; OUT on
        # an income account is a reversal netting revenue down.
        if acc.type in _INCOME_TYPES:
            delta = net_amount if t.direction == BankTxnDirection.IN else -net_amount
            income_by_acc[acc.id] = income_by_acc.get(acc.id, ZERO) + delta
        elif acc.type == AccountType.COST_OF_SALES:
            delta = net_amount if t.direction == BankTxnDirection.OUT else -net_amount
            cogs_by_acc[acc.id] = cogs_by_acc.get(acc.id, ZERO) + delta
        elif acc.type == AccountType.EXPENSE:
            delta = net_amount if t.direction == BankTxnDirection.OUT else -net_amount
            expense_by_acc[acc.id] = expense_by_acc.get(acc.id, ZERO) + delta
        # ASSET / LIABILITY / EQUITY categorisations stay off the income statement.

    # M2.2: also fold in journal lines that hit P&L-natured accounts.
    # Income accounts have a natural credit balance: Cr increases revenue, Dr reduces it.
    # Expense / COGS have a natural debit balance: Dr increases cost, Cr reduces it.
    from ..models.company import JournalEntry, JournalLine

    journal_pnl = db.execute(
        select(
            JournalLine.account_id,
            func.sum(JournalLine.debit_amount),
            func.sum(JournalLine.credit_amount),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
        )
        .group_by(JournalLine.account_id)
    ).all()

    for aid, total_d, total_c in journal_pnl:
        acc = _acc(aid)
        if acc is None:
            continue
        d = Decimal(total_d or 0)
        c = Decimal(total_c or 0)
        if acc.type in _INCOME_TYPES:
            # Revenue = credit - debit (debits to income are reductions / refunds).
            net = c - d
            if net != ZERO:
                income_by_acc[acc.id] = income_by_acc.get(acc.id, ZERO) + net
        elif acc.type == AccountType.COST_OF_SALES:
            net = d - c
            if net != ZERO:
                cogs_by_acc[acc.id] = cogs_by_acc.get(acc.id, ZERO) + net
        elif acc.type == AccountType.EXPENSE:
            net = d - c
            if net != ZERO:
                expense_by_acc[acc.id] = expense_by_acc.get(acc.id, ZERO) + net
        # ASSET / LIABILITY / EQUITY journal postings don't belong on P&L.

    def _expand(bucket: dict[int, Decimal]) -> list[dict]:
        out = []
        for aid, total in bucket.items():
            a = account_cache.get(aid)
            out.append({
                "account_id": aid,
                "code": a.code if a else "?",
                "name": a.name if a else "?",
                "total": total,
            })
        out.sort(key=lambda r: r["code"])
        return out

    income_rows = _expand(income_by_acc)
    cogs_rows = _expand(cogs_by_acc)
    expense_rows = _expand(expense_by_acc)
    total_income = sum((r["total"] for r in income_rows), ZERO)
    total_cogs = sum((r["total"] for r in cogs_rows), ZERO)
    total_expense = sum((r["total"] for r in expense_rows), ZERO)
    gross_profit = total_income - total_cogs
    net_profit = gross_profit - total_expense

    return {
        "period_start": period_start,
        "period_end": period_end,
        "income_rows": income_rows,
        "cogs_rows": cogs_rows,
        "expense_rows": expense_rows,
        "uncategorised_in": uncategorised_in,
        "uncategorised_out": uncategorised_out,
        "total_income": total_income,
        "total_cogs": total_cogs,
        "total_expense": total_expense,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
    }


def _empty_pnl(start: date, end: date) -> dict:
    return {
        "period_start": start,
        "period_end": end,
        "income_rows": [],
        "cogs_rows": [],
        "expense_rows": [],
        "uncategorised_in": ZERO,
        "uncategorised_out": ZERO,
        "total_income": ZERO,
        "total_cogs": ZERO,
        "total_expense": ZERO,
        "gross_profit": ZERO,
        "net_profit": ZERO,
    }


# ---------------------------------------------------------------------------
# 4) BAS — quarterly GST return (placeholder while firm is not registered)
# ---------------------------------------------------------------------------


def bas(db: Session, *, fy_year: int, quarter: int, gst_registered: bool = False) -> dict:
    """Australian Business Activity Statement, quarterly view.

    CASH BASIS: every box is sourced from bank transactions in the period, not
    from the general ledger. A posted-but-unpaid invoice contributes 0 to BAS
    until its payment lands as a categorised bank transaction — so BAS can
    legitimately read 0 GST while the trial balance (accrual) shows GST on the
    same invoice. See gst.gst_exposure for the full rationale.

    Delegates the box computation to gst.gst_exposure_for_quarter so the BAS
    and the GST-exposure report can never disagree: both exclude
    tax_code=none transactions (owner draws, inter-account transfers).

    `gst_registered` is surfaced on the response so the UI can decide whether
    to show "you don't need to file this" disclaimers vs. a real BAS view.
    Caller is expected to pass company.gst_registered.
    """
    # Local import: gst.py imports _au_fy_quarter_bounds from this module.
    from .gst import gst_exposure_for_quarter

    g = gst_exposure_for_quarter(
        db,
        fy_year=fy_year,
        quarter=quarter,
        gst_registered=gst_registered,
    )

    return {
        "fy_year": fy_year,
        "quarter": quarter,
        "period_start": g["period_start"],
        "period_end": g["period_end"],
        "g1_total_sales": g["g1_total_sales"],
        "one_a_gst_on_sales": g["one_a_gst_on_sales"],
        "total_purchases": g["total_purchases"],
        "one_b_gst_on_purchases": g["one_b_gst_on_purchases"],
        "net_gst_payable": g["net_gst_payable"],  # > 0 → you owe; < 0 → refund
        "uncategorised_count": g["uncategorised_count"],
        "gst_registered": gst_registered,     # surfaced for the UI to render disclaimers
    }
