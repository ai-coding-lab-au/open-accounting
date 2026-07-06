"""Trial balance — M2.2.

The trial balance aggregates every "posting-like" data source per Chart-of-
Accounts entry as of a given date, and shows total debits / credits per
account plus a grand total.

Sources combined here:

  1. JournalLine — explicit Dr/Cr on an account_id.
  2. BankTransaction (categorised) — implicitly a Dr/Cr pair:
       Bank IN  + account_id of any type        → Dr Bank,    Cr <other>
       Bank OUT + account_id of any type        → Dr <other>, Cr Bank
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

from sqlalchemy import func, select
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
)
from .reports import profit_and_loss

# AR / AP invoice control accounts (same codes invoice_posting._control_account
# resolves). Bank txns categorised here are invoice settlements, not primary
# income/expense events — their GST is carried by the invoice journal.
INVOICE_CONTROL_ACCOUNT_CODES = ("1100", "2000")


ZERO = Decimal("0")


def _opening_balance_equity_account(db: Session) -> Account | None:
    """Equity account that carries the contra leg for bank opening balances.

    The default CoA has no dedicated "Opening Balance Equity" account, so we
    use 3000 Owner's Capital — an opening bank balance is capital the owner
    brought onto the books at system start. 3900 Retained Earnings is
    deliberately avoided: the balance sheet computes retained earnings live
    from the P&L and treats manual 3900 balances as additive on top.
    Falls back to the lowest-coded EQUITY account if 3000 is missing.
    """
    acc = (
        db.query(Account)
        .filter(Account.code == "3000", Account.type == AccountType.EQUITY)
        .first()
    )
    if acc is None:
        acc = (
            db.query(Account)
            .filter(Account.type == AccountType.EQUITY)
            .order_by(Account.code)
            .first()
        )
    return acc


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
    opening_equity = _opening_balance_equity_account(db)
    for ba in db.query(BankAccount).all():
        if ba.opening_balance and ba.opening_balance != ZERO:
            ob = ba.opening_balance
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

        if t.direction == BankTxnDirection.IN:
            # Money coming in → credit the contra account (typical: income).
            account_credit[t.account_id] = account_credit.get(t.account_id, ZERO) + t.amount
        else:
            # Money going out → debit the contra account (typical: expense / asset purchase).
            account_debit[t.account_id] = account_debit.get(t.account_id, ZERO) + t.amount

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

    income_total = ZERO
    expense_total = ZERO

    # GST embedded in a bank txn categorised to a BALANCE-SHEET account
    # (asset/liability/equity) must be stripped from that account's balance and
    # left to live solely in the net-GST line below. The contra leg is posted at
    # GROSS (t.amount incl. GST); the net-GST line separately adds Σ gst. Without
    # stripping, the GST is counted twice and the balance sheet won't balance.
    # P&L-categorised txns are already handled by the Profit & Loss net-of-GST
    # calc, so they must NOT be stripped here. OUT posts to the account debit and
    # IN to the credit, so the net-debit adjustment is (Σ IN gst − Σ OUT gst).
    # (This generalises the old CAPITAL-only, OUT-only stripping to every
    # claimable tax code and both directions — a STANDARD-rated purchase booked
    # to an asset account used to leave the sheet unbalanced.)
    # EXCEPTION to the stripping: bank txns categorised to the AR/AP invoice
    # CONTROL accounts (1100 / 2000) are invoice settlements. Their GST already
    # lives in the invoice journal (2100 GST Collected / 1200 GST Paid), and the
    # gross contra leg exactly nets the journal's gross control-account balance.
    # Stripping here would resurrect a phantom control-account balance, and
    # counting their gst_amount in the net-GST line below would double the
    # journal GST accounts. The txn-level gst_amount still exists so the
    # cash-basis BAS recognises the GST at payment date (services/gst.py).
    invoice_control_ids = [
        row[0]
        for row in db.query(Account.id)
        .filter(Account.code.in_(INVOICE_CONTROL_ACCOUNT_CODES))
        .all()
    ]
    gst_net_debit_adj: dict[int, Decimal] = {}
    gst_adj_rows = (
        db.query(
            BankTransaction.account_id,
            BankTransaction.direction,
            func.sum(BankTransaction.gst_amount),
        )
        .filter(
            BankTransaction.occurred_at <= as_of,
            BankTransaction.account_id.isnot(None),
            BankTransaction.account_id.notin_(invoice_control_ids),
            BankTransaction.gst_amount > 0,
        )
        .group_by(BankTransaction.account_id, BankTransaction.direction)
        .all()
    )
    for account_id, direction, total in gst_adj_rows:
        g = Decimal(total or 0)
        signed = g if direction == BankTxnDirection.IN else -g
        gst_net_debit_adj[account_id] = (
            gst_net_debit_adj.get(account_id, ZERO) + signed
        )

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
        # Ex-GST net debit: strip the embedded GST (0 when GST-free / not
        # registered) so it isn't double-counted against the net-GST line.
        net_debit = row["net_debit"] + gst_net_debit_adj.get(row["ref_id"], ZERO)
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

    # Net-GST line: only CATEGORISED txns outside the AR/AP control accounts.
    # Uncategorised rows may carry a provisional standard-rate gst_amount from
    # import, but their economic nature is unknown (BAS skips them too);
    # control-account settlements' GST is already on the sheet via the invoice
    # journal's 2100/1200 balances (see the stripping exception above).
    invoice_control_ids = [
        row[0]
        for row in db.query(Account.id)
        .filter(Account.code.in_(INVOICE_CONTROL_ACCOUNT_CODES))
        .all()
    ]
    gst_rows = (
        db.query(BankTransaction.direction, func.sum(BankTransaction.gst_amount))
        .filter(
            BankTransaction.occurred_at <= as_of,
            BankTransaction.account_id.isnot(None),
            BankTransaction.account_id.notin_(invoice_control_ids),
        )
        .group_by(BankTransaction.direction)
        .all()
    )
    gst_collected = ZERO
    gst_paid = ZERO
    for direction, total in gst_rows:
        if direction == BankTxnDirection.IN:
            gst_collected += Decimal(total or 0)
        else:
            gst_paid += Decimal(total or 0)
    net_gst = gst_collected - gst_paid
    if net_gst > ZERO:
        groups_liabs["GST payable"].append({
            "account_id": None,
            "code": None,
            "name": "Net GST payable",
            "balance": net_gst,
        })
    elif net_gst < ZERO:
        groups_assets["GST credits"].append({
            "account_id": None,
            "code": None,
            "name": "Net GST refundable",
            "balance": -net_gst,
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
            subtotal = sum((Decimal(l["balance"]) for l in lines), ZERO)
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
