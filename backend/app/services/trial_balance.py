"""Trial balance — M2.2.

The trial balance aggregates every "posting-like" data source per Chart-of-
Accounts entry as of a given date, and shows total debits / credits per
account plus a grand total.

Sources combined here:

  1. JournalLine — explicit Dr/Cr on an account_id.
  2. BankTransaction (categorised) — implicitly a balanced posting:
       Bank IN  → Dr Bank gross, Cr <other> net, Cr GST control (when GST > 0)
       Bank OUT → Dr <other> net, Dr GST control, Cr Bank gross
     AR/AP invoice settlements remain gross on the control account because the
     invoice journal already carries their GST control leg.
     The "Bank" side is the BankAccount's own row in the trial balance
     (one BankAccount maps to one CoA account by id when categorised;
     since BankAccount isn't an Account itself, we model the bank side
     by aggregating BankTransactions per BankAccount and then writing
     that as a single Dr/Cr against a synthetic "Bank #<id>" line).
  3. Opening balances on BankAccount — treated as the bank's starting Dr,
     with a contra Cr against equity so the books stay balanced (reversed
     legs for negative openings).

Deliberately NOT in the main aggregation (yet):

  - Legacy unposted Invoice AP/AR — surfaced separately under
    `supplementary.ap_ar`; invoices with journal provenance are counted
    through JournalLine only.

Result-shape contract:

    {
      "as_of": date,
      "rows": [
         {"key": "account:<id>" | "bank:<id>",
          "kind": "account" | "bank",
          "ref_id": int,
          "code": str | None,
          "name": str,
          "account_type": str | None,    # AccountType value, only for kind=account
          "debit_total": Decimal,
          "credit_total": Decimal,
          "net_debit": Decimal,            # debit_total - credit_total
         },
         ...
      ],
      "total_debit": Decimal,
      "total_credit": Decimal,
      "is_balanced": bool,
      "diff": Decimal,                     # total_debit - total_credit
      "uncategorised_bank_in": Decimal,    # sum of un-categorised IN
      "uncategorised_bank_out": Decimal,   # sum of un-categorised OUT
      "supplementary": {
         "ap_open_total": Decimal,
         "ar_open_total": Decimal,
      },
    }
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    AccountType,
    BankAccount,
    BankTransaction,
    BankTxnDirection,
    Invoice,
    InvoiceDirection,
    InvoiceStatus,
    JournalEntry,
    JournalEntrySource,
    JournalLine,
    TaxCode,
)
from .account_invariants import require_opening_balance_equity_account
from .reports import profit_and_loss
from .transaction_classification import bank_event_is_sale

# AR / AP invoice control accounts (same codes invoice_posting._control_account
# resolves). Bank txns categorised here are invoice settlements, not primary
# income/expense events — their GST is carried by the invoice journal.
INVOICE_CONTROL_ACCOUNT_CODES = ("1100", "2000")
GST_PAID_ACCOUNT_CODE = "1200"
GST_COLLECTED_ACCOUNT_CODE = "2100"


ZERO = Decimal("0")


def _opening_balance_equity_account(db: Session) -> Account:
    """Equity account that carries the contra leg for bank opening balances.

    The default CoA has no dedicated "Opening Balance Equity" account, so 3000
    Owner's Capital is the canonical contra account. It must remain active and
    EQUITY; silently falling back to another account would rewrite history.
    """
    return require_opening_balance_equity_account(db)


def trial_balance(db: Session, *, as_of: date | None = None) -> dict:
    """Build a trial balance as of the given date (inclusive). `None` = all-time."""

    # ------------------------------------------------------------------
    # Phase 1: per-account Dr/Cr from journal lines.
    # ------------------------------------------------------------------
    account_debit: dict[int, Decimal] = {}
    account_credit: dict[int, Decimal] = {}

    journal_q = (
        select(JournalLine.account_id, JournalLine.debit_amount, JournalLine.credit_amount)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
    )
    if as_of is not None:
        journal_q = journal_q.where(JournalEntry.entry_date <= as_of)

    for aid, d, c in db.execute(journal_q).all():
        d = d or ZERO
        c = c or ZERO
        if d > 0:
            account_debit[aid] = account_debit.get(aid, ZERO) + d
        if c > 0:
            account_credit[aid] = account_credit.get(aid, ZERO) + c

    # ------------------------------------------------------------------
    # Phase 2: BankTransaction sides — both the bank's own line and the
    # categorised contra side.
    # ------------------------------------------------------------------
    bank_debit: dict[int, Decimal] = {}
    bank_credit: dict[int, Decimal] = {}

    # Opening balances are the bank's starting Dr, with a contra Cr against
    # equity (Dr Bank / Cr Owner's Capital) so the trial balance — and the
    # balance sheet derived from it — stays balanced. Negative openings
    # reverse the legs (Cr Bank / Dr equity).
    opening_equity: Account | None = None
    for ba in db.query(BankAccount).all():
        if ba.opening_balance and ba.opening_balance != ZERO:
            ob = ba.opening_balance
            if opening_equity is None:
                opening_equity = _opening_balance_equity_account(db)
            contra = opening_equity
            if ob > 0:
                bank_debit[ba.id] = bank_debit.get(ba.id, ZERO) + ob
                account_credit[contra.id] = account_credit.get(contra.id, ZERO) + ob
            else:
                bank_credit[ba.id] = bank_credit.get(ba.id, ZERO) + (-ob)
                account_debit[contra.id] = account_debit.get(contra.id, ZERO) + (-ob)

    txn_q = db.query(BankTransaction)
    if as_of is not None:
        txn_q = txn_q.filter(BankTransaction.occurred_at <= as_of)
    txns = txn_q.all()

    txn_account_ids = {
        account_id
        for t in txns
        for account_id in (t.account_id, t.unapplied_account_id)
        if account_id is not None
    }
    txn_accounts_by_id: dict[int, Account] = {}
    if txn_account_ids:
        txn_accounts_by_id = {
            a.id: a
            for a in db.query(Account).filter(Account.id.in_(txn_account_ids)).all()
        }

    invoice_control_ids = {
        a.id
        for a in txn_accounts_by_id.values()
        if a.code in INVOICE_CONTROL_ACCOUNT_CODES
    }
    gst_controls = {
        a.code: a
        for a in db.query(Account)
        .filter(
            Account.code.in_((GST_PAID_ACCOUNT_CODE, GST_COLLECTED_ACCOUNT_CODE)),
            Account.active.is_(True),
        )
        .all()
    }

    uncategorised_in = ZERO
    uncategorised_out = ZERO

    for t in txns:
        # Bank-side leg always counts.
        if t.direction == BankTxnDirection.IN:
            bank_debit[t.bank_account_id] = bank_debit.get(t.bank_account_id, ZERO) + t.amount
        else:
            bank_credit[t.bank_account_id] = bank_credit.get(t.bank_account_id, ZERO) + t.amount

        # Contra side only when categorised.
        if t.account_id is None:
            if t.direction == BankTxnDirection.IN:
                uncategorised_in += t.amount
            else:
                uncategorised_out += t.amount
            continue

        # Invoice settlements deliberately remain gross on AR/AP. Their GST
        # control leg was posted by the invoice journal; the bank row carries
        # gst_amount only so cash-basis BAS recognises it at settlement date.
        gst_amount = Decimal(t.gst_amount or ZERO)
        unapplied_amount = Decimal(t.unapplied_amount or ZERO)
        primary_amount = Decimal(t.amount) - unapplied_amount
        split_gst = gst_amount > ZERO and t.account_id not in invoice_control_ids
        contra_amount = primary_amount - gst_amount if split_gst else primary_amount

        if t.direction == BankTxnDirection.IN:
            # Money coming in → credit the contra account (typical: income).
            account_credit[t.account_id] = (
                account_credit.get(t.account_id, ZERO) + contra_amount
            )
        else:
            # Money going out → debit the contra account (typical: expense / asset purchase).
            account_debit[t.account_id] = (
                account_debit.get(t.account_id, ZERO) + contra_amount
            )

        if unapplied_amount > ZERO:
            if t.unapplied_account_id is None:
                raise RuntimeError(
                    f"Bank transaction {t.id} has an unapplied amount without "
                    "a destination account."
                )
            if t.direction == BankTxnDirection.IN:
                account_credit[t.unapplied_account_id] = (
                    account_credit.get(t.unapplied_account_id, ZERO)
                    + unapplied_amount
                )
            else:
                account_debit[t.unapplied_account_id] = (
                    account_debit.get(t.unapplied_account_id, ZERO)
                    + unapplied_amount
                )

        if not split_gst:
            continue

        acc = txn_accounts_by_id.get(t.account_id)
        account_type = (
            acc.type.value if acc and hasattr(acc.type, "value") else (acc.type if acc else None)
        )
        try:
            tax_code = (
                TaxCode(t.tax_code) if isinstance(t.tax_code, str) else t.tax_code
            )
        except ValueError:
            tax_code = TaxCode.STANDARD

        treat_as_sale = bank_event_is_sale(
            account_code=acc.code if acc else None,
            account_type=account_type,
            tax_code=tax_code,
            direction=t.direction,
        )
        gst_control_code = (
            GST_COLLECTED_ACCOUNT_CODE
            if treat_as_sale
            else GST_PAID_ACCOUNT_CODE
        )

        gst_control = gst_controls.get(gst_control_code)
        if gst_control is None:
            raise RuntimeError(
                f"Missing active GST control account {gst_control_code}; "
                "cannot build a semantically correct Trial Balance."
            )
        if t.direction == BankTxnDirection.IN:
            account_credit[gst_control.id] = (
                account_credit.get(gst_control.id, ZERO) + gst_amount
            )
        else:
            account_debit[gst_control.id] = (
                account_debit.get(gst_control.id, ZERO) + gst_amount
            )

    # ------------------------------------------------------------------
    # Phase 3: assemble rows. Fetch all accounts and banks we touched.
    # ------------------------------------------------------------------
    touched_account_ids = set(account_debit) | set(account_credit)
    touched_bank_ids = set(bank_debit) | set(bank_credit)

    accounts_by_id: dict[int, Account] = {}
    if touched_account_ids:
        for a in db.query(Account).filter(Account.id.in_(touched_account_ids)).all():
            accounts_by_id[a.id] = a

    banks_by_id: dict[int, BankAccount] = {}
    if touched_bank_ids:
        for ba in db.query(BankAccount).filter(BankAccount.id.in_(touched_bank_ids)).all():
            banks_by_id[ba.id] = ba

    rows: list[dict] = []

    for aid in sorted(touched_account_ids):
        a = accounts_by_id.get(aid)
        d = account_debit.get(aid, ZERO)
        c = account_credit.get(aid, ZERO)
        rows.append({
            "key": f"account:{aid}",
            "kind": "account",
            "ref_id": aid,
            "code": a.code if a else "?",
            "name": a.name if a else f"<deleted account #{aid}>",
            "account_type": (a.type.value if a and hasattr(a.type, "value") else (a.type if a else None)),
            "debit_total": d,
            "credit_total": c,
            "net_debit": d - c,
        })

    for bid in sorted(touched_bank_ids):
        ba = banks_by_id.get(bid)
        d = bank_debit.get(bid, ZERO)
        c = bank_credit.get(bid, ZERO)
        rows.append({
            "key": f"bank:{bid}",
            "kind": "bank",
            "ref_id": bid,
            "code": None,
            "name": ba.name if ba else f"<deleted bank #{bid}>",
            "account_type": None,
            "debit_total": d,
            "credit_total": c,
            "net_debit": d - c,
        })

    # Sort: accounts first by code, then bank lines by name.
    rows.sort(key=lambda r: (r["kind"], r.get("code") or "", r["name"]))

    total_debit = sum((r["debit_total"] for r in rows), ZERO)
    total_credit = sum((r["credit_total"] for r in rows), ZERO)
    diff = total_debit - total_credit
    # Allow uncategorised bank txns to break the balance — they're effectively
    # half-postings. Once everything is categorised, debit must equal credit.
    is_balanced = diff == ZERO

    # ------------------------------------------------------------------
    # Phase 4: supplementary blocks (AP/AR).
    # ------------------------------------------------------------------
    ap_open_total, ar_open_total = _ap_ar_outstanding(db, as_of=as_of)

    return {
        "as_of": as_of,
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "diff": diff,
        "is_balanced": is_balanced,
        "uncategorised_bank_in": uncategorised_in,
        "uncategorised_bank_out": uncategorised_out,
        "supplementary": {
            "ap_open_total": ap_open_total,
            "ar_open_total": ar_open_total,
        },
    }


def balance_sheet(db: Session, *, as_of: date | None = None) -> dict:
    """Balance Sheet at a point in time.

    Assets   = Σ(ASSET account net_debit) + Σ(bank net_debit) + AR outstanding
    Liabs    = Σ(LIABILITY account net_credit) + AP outstanding
    Equity   = Σ(EQUITY account net_credit) + retained earnings (income - expense)

    Notes:
      - Net_debit for asset/expense-natured accounts is the natural positive
        balance; we surface it directly. For liability/equity/income accounts
        we report `-net_debit` (i.e. credit balance).
      - Retained earnings is computed live from the period income statement;
        the user shouldn't manually post to 3900 yet. If they do, it adds on
        top.
    """
    if as_of is None:
        as_of = date.today()

    tb = trial_balance(db, as_of=as_of)

    groups_assets: dict[str, list[dict]] = {"Assets": [], "Banks": [], "Receivables": [], "GST credits": []}
    groups_liabs: dict[str, list[dict]] = {"Liabilities": [], "Payables": [], "GST payable": []}
    groups_equity: dict[str, list[dict]] = {"Equity": [], "Retained earnings": []}

    for row in tb["rows"]:
        if row["kind"] == "bank":
            # Bank lines always sit under assets (we don't model overdrafts
            # as liabilities at this stage).
            groups_assets["Banks"].append({
                "account_id": None,
                "code": None,
                "name": row["name"],
                "balance": row["net_debit"],
            })
            continue

        atype = row["account_type"]
        # Bank GST is already posted explicitly to 1200/2100 by trial_balance;
        # every account row is therefore its true ledger balance with no
        # balance-sheet-only stripping or synthetic repair.
        net_debit = row["net_debit"]
        if atype == AccountType.ASSET.value:
            groups_assets["Assets"].append({
                "account_id": row["ref_id"],
                "code": row["code"],
                "name": row["name"],
                "balance": net_debit,
            })
        elif atype == AccountType.LIABILITY.value:
            groups_liabs["Liabilities"].append({
                "account_id": row["ref_id"],
                "code": row["code"],
                "name": row["name"],
                "balance": -net_debit,  # liabilities have natural credit balance
            })
        elif atype == AccountType.EQUITY.value:
            groups_equity["Equity"].append({
                "account_id": row["ref_id"],
                "code": row["code"],
                "name": row["name"],
                "balance": -net_debit,
            })
        elif atype == AccountType.INCOME.value:
            pass
        elif atype in (AccountType.EXPENSE.value, AccountType.COST_OF_SALES.value):
            pass
        # P&L-natured rows are rolled into retained earnings below using the
        # same net-of-GST calculation as the Profit & Loss report.

    # AP/AR rollups go into their own lines.
    supp = tb["supplementary"]
    if supp["ar_open_total"] > 0:
        groups_assets["Receivables"].append({
            "account_id": None,
            "code": None,
            "name": "Accounts Receivable (open invoices)",
            "balance": supp["ar_open_total"],
        })
    if supp["ap_open_total"] > 0:
        groups_liabs["Payables"].append({
            "account_id": None,
            "code": None,
            "name": "Accounts Payable (open invoices)",
            "balance": supp["ap_open_total"],
        })

    pnl_to_date = profit_and_loss(db, period_start=date.min, period_end=as_of)
    retained = pnl_to_date["net_profit"]
    if retained != ZERO:
        groups_equity["Retained earnings"].append({
            "account_id": None,
            "code": None,
            "name": f"Retained earnings (income - expenses up to {as_of.isoformat()})",
            "balance": retained,
        })

    def _materialise(groups: dict[str, list[dict]]) -> list[dict]:
        out: list[dict] = []
        for label, lines in groups.items():
            if not lines:
                continue
            subtotal = sum((Decimal(line["balance"]) for line in lines), ZERO)
            out.append({"label": label, "lines": lines, "subtotal": subtotal})
        return out

    assets = _materialise(groups_assets)
    liabilities = _materialise(groups_liabs)
    equity = _materialise(groups_equity)

    total_assets = sum((g["subtotal"] for g in assets), ZERO)
    total_liabs = sum((g["subtotal"] for g in liabilities), ZERO)
    total_equity = sum((g["subtotal"] for g in equity), ZERO)

    diff = total_assets - (total_liabs + total_equity)
    is_balanced = diff == ZERO

    return {
        "as_of": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "total_assets": total_assets,
        "total_liabilities": total_liabs,
        "total_equity": total_equity,
        "is_balanced": is_balanced,
        "diff": diff,
    }


def _ap_ar_outstanding(db: Session, *, as_of: date | None) -> tuple[Decimal, Decimal]:
    """Sum of unpaid (total - paid_amount) for AP and AR invoices.

    Capped to as_of by issue_date; an invoice issued after the cutoff doesn't
    exist on a trial balance "as of" that date.
    """
    posted_invoice_ids = (
        select(JournalEntry.source_id)
        .where(
            JournalEntry.source_type.in_(
                [JournalEntrySource.INVOICE_AR, JournalEntrySource.INVOICE_AP]
            ),
            JournalEntry.source_id.isnot(None),
        )
    )
    q = (
        db.query(Invoice)
        # Exclude void (cancelled) and draft (not a live document yet — a draft
        # isn't in the ledger or AP/AR totals until it's authorised). Posted
        # invoices live in the GL control accounts, so they're excluded here
        # via the posted-ids subquery to avoid double counting.
        .filter(Invoice.status.notin_([InvoiceStatus.VOID, InvoiceStatus.DRAFT]))
        .filter(~Invoice.id.in_(posted_invoice_ids))
    )
    if as_of is not None:
        q = q.filter(Invoice.issue_date <= as_of)

    ap_total = ZERO
    ar_total = ZERO
    for inv in q.all():
        outstanding = (inv.total or ZERO) - (inv.paid_amount or ZERO)
        if outstanding <= 0:
            continue
        if inv.direction == InvoiceDirection.AP:
            ap_total += outstanding
        else:
            ar_total += outstanding
    return ap_total, ar_total
