"""GST exposure report (M2.3).

Aggregates bank transactions by tax_code so the user can see what their
BAS will look like before they lodge. Maps directly to the AU BAS
quarterly boxes:

  Sales (IN):
    G1   = Total sales (gross IN, all tax codes)
    G3   = Other GST-free sales (IN with tax_code=gst_free)
    G4   = Input-taxed sales      (IN with tax_code=input_taxed)
    G6   = Sales subject to GST   = G1 − G3 − G4
    1A   = GST on sales           = Σ gst_amount on IN with tax_code=standard

  Purchases (OUT):
    G10  = Capital purchases      (OUT with tax_code=capital)
    G11  = Non-capital purchases  (all other BAS-relevant OUT tax codes)
    G14  = Purchases without GST in the price (GST-free / input-taxed,
           plus capital rows carrying no GST)
    1B   = GST on purchases       = Σ gst_amount on OUT with tax_code in (standard, capital)

  Net:
    net_gst_payable = 1A − 1B    (positive → owe ATO, negative → refund)

Anything tax_code=none is excluded from BAS entirely (owner draws,
inter-account transfers). Uncategorised transactions are also excluded from BAS
boxes until the user assigns an account; otherwise bank deposits such as capital
top-ups would inflate turnover.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session, selectinload

from ..models.company import (
    Account,
    BankAccount,
    BankTransaction,
    BankTxnDirection,
    InvoiceDirection,
    InvoicePaymentAllocation,
    TaxCode,
)
from .reports import _au_fy_quarter_bounds
from .transaction_classification import bank_event_is_sale


ZERO = Decimal("0")


def gst_exposure(
    db: Session,
    *,
    period_start: date,
    period_end: date,
    gst_registered: bool = True,
) -> dict:
    """GST exposure for a period, computed on a CASH BASIS.

    BAS here is intentionally cash-basis: every box is summed from
    ``BankTransaction`` rows on business/savings accounts (see the query
    below) — money that actually moved through the bank in the period. It does
    NOT read the general ledger or posted invoices.

    Consequence (by design, not a bug): a posted-but-unpaid AR invoice raises
    GST Collected in the GL — so it shows in the trial balance / P&L (accrual)
    — but contributes 0 to BAS until its payment is recorded as a categorised
    bank transaction. So BAS and the TB can legitimately disagree on GST for
    unsettled invoices. This mirrors the GL-vs-bank-module boundary the trial
    balance exposes via its ``bank:`` rows.

    Cash basis is a valid AU BAS method for small businesses (turnover under
    the ATO threshold). If accrual BAS is ever required, this function must be
    rewritten to source from the GL / invoice register, not bank transactions.
    """
    if period_start > period_end:
        raise ValueError("period_start must be <= period_end")
    if not gst_registered:
        # A non-registered entity has no BAS GST boxes. Write paths reject new
        # GST splits; returning the policy-zero view here also prevents legacy
        # inconsistent rows from being presented as a lodgable BAS.
        return _empty(period_start, period_end)

    biz_ids = [b.id for b in db.query(BankAccount).all()]
    if not biz_ids:
        return _empty(period_start, period_end)

    txns = (
        db.query(BankTransaction)
        .options(
            selectinload(BankTransaction.invoice_allocations).selectinload(
                InvoicePaymentAllocation.tax_components
            ),
            selectinload(BankTransaction.invoice_allocations).selectinload(
                InvoicePaymentAllocation.invoice
            ),
        )
        .filter(
            BankTransaction.bank_account_id.in_(biz_ids),
            BankTransaction.occurred_at >= period_start,
            BankTransaction.occurred_at <= period_end,
        )
        .all()
    )

    # Buckets.
    G1 = ZERO   # all IN gross
    G3 = ZERO   # IN gst_free
    G4 = ZERO   # IN input_taxed
    one_A = ZERO  # GST on standard IN

    G10 = ZERO  # capital purchases (TaxCode.CAPITAL)
    G11 = ZERO  # every other BAS-relevant purchase
    G14 = ZERO  # purchases without GST in the price; overlaps G10/G11
    one_B = ZERO  # GST on standard + capital OUT
    total_purchases = ZERO  # all OUT gross (excl. tax_code=none); consumed by reports.bas()

    excluded_count = 0
    uncategorised_count = 0

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
        if t.invoice_allocations:
            allocated_gross = ZERO
            component_gross = ZERO
            for allocation in t.invoice_allocations:
                allocated_gross += Decimal(allocation.amount)
                invoice_direction = (
                    allocation.invoice.direction.value
                    if hasattr(allocation.invoice.direction, "value")
                    else str(allocation.invoice.direction)
                )
                treat_as_sale = invoice_direction == InvoiceDirection.AR.value
                for component in allocation.tax_components:
                    try:
                        tc = TaxCode(component.tax_code)
                    except ValueError as exc:
                        raise RuntimeError(
                            f"Payment allocation {allocation.id} has invalid tax "
                            f"component {component.tax_code!r}."
                        ) from exc
                    amt = Decimal(component.gross_amount)
                    gst = Decimal(component.gst_amount)
                    component_gross += amt
                    if tc == TaxCode.NONE:
                        excluded_count += 1
                        continue
                    if treat_as_sale:
                        G1 += amt
                        if tc == TaxCode.GST_FREE:
                            G3 += amt
                        elif tc == TaxCode.INPUT_TAXED:
                            G4 += amt
                        elif tc == TaxCode.STANDARD:
                            one_A += gst
                    else:
                        total_purchases += amt
                        if tc == TaxCode.CAPITAL:
                            G10 += amt
                        else:
                            G11 += amt
                        if tc in (TaxCode.STANDARD, TaxCode.CAPITAL):
                            one_B += gst
                        if tc in (TaxCode.GST_FREE, TaxCode.INPUT_TAXED) or (
                            tc == TaxCode.CAPITAL and gst == 0
                        ):
                            G14 += amt
            if component_gross != allocated_gross:
                raise RuntimeError(
                    f"Bank transaction {t.id} has incomplete invoice-payment tax "
                    "composition; remove and re-allocate it before reporting GST."
                )
            # Any unapplied overpayment/prepayment remainder is deliberately
            # outside BAS until the operator applies or otherwise classifies it.
            continue

        try:
            tc = TaxCode(t.tax_code) if isinstance(t.tax_code, str) else t.tax_code
        except ValueError:
            tc = TaxCode.STANDARD

        if tc == TaxCode.NONE:
            excluded_count += 1
            continue

        acc = _acc(t.account_id) if t.account_id is not None else None
        if acc is None:
            uncategorised_count += 1
            continue

        # Classify each txn as a sale or a purchase by the CATEGORISED account's
        # type, not direction alone. A cross-type flow — an IN categorised to an
        # expense account (a purchase refund) or an OUT categorised to an income
        # account (a sale refund) — is a decreasing adjustment to the OTHER side,
        # mirroring the contra logic reports.py uses for the P&L so BAS and P&L
        # agree on cross-type rows. Rows without a resolved account are skipped
        # above and counted separately.
        treat_as_sale = bank_event_is_sale(
            account_code=acc.code,
            account_type=acc.type,
            tax_code=tc,
            direction=t.direction,
        )

        # +1 when the txn's direction agrees with its category (a true sale on an
        # IN, or a purchase on an OUT); -1 for the cross-type refund leg.
        if treat_as_sale:
            sign = 1 if t.direction == BankTxnDirection.IN else -1
        else:
            sign = 1 if t.direction == BankTxnDirection.OUT else -1

        amt = sign * t.amount
        gst = sign * t.gst_amount

        if treat_as_sale:
            G1 += amt
            if tc == TaxCode.GST_FREE:
                G3 += amt
            elif tc == TaxCode.INPUT_TAXED:
                G4 += amt
            elif tc == TaxCode.STANDARD:
                one_A += gst
        else:
            total_purchases += amt
            # ATO worksheet membership overlaps: every purchase belongs in
            # G10 (capital) or G11 (non-capital), while purchases without GST
            # in the price ALSO belong in G14. The sign already handles
            # cross-type refunds/decreasing adjustments.
            # Capital is an explicit, stable tax dimension. Account types and
            # user-defined account codes are not reliable capital classifiers
            # (for example, inventory is commonly an ASSET but belongs at G11).
            is_capital_purchase = tc == TaxCode.CAPITAL
            if is_capital_purchase:
                G10 += amt
            else:
                G11 += amt
            if tc in (TaxCode.STANDARD, TaxCode.CAPITAL):
                one_B += gst
            if tc in (TaxCode.GST_FREE, TaxCode.INPUT_TAXED) or (
                tc == TaxCode.CAPITAL and gst == 0
            ):
                G14 += amt

    G6 = G1 - G3 - G4
    net_gst = one_A - one_B

    return {
        "period_start": period_start,
        "period_end": period_end,
        "g1_total_sales": G1,
        "g3_gst_free_sales": G3,
        "g4_input_taxed_sales": G4,
        "g6_sales_subject_to_gst": G6,
        "one_a_gst_on_sales": one_A,
        "g10_capital_purchases": G10,
        "g11_non_capital_purchases": G11,
        "g14_gst_free_purchases": G14,
        "one_b_gst_on_purchases": one_B,
        "total_purchases": total_purchases,
        "net_gst_payable": net_gst,
        "excluded_count": excluded_count,
        "uncategorised_count": uncategorised_count,
    }


def gst_exposure_for_quarter(
    db: Session,
    *,
    fy_year: int,
    quarter: int,
    gst_registered: bool = True,
) -> dict:
    start, end = _au_fy_quarter_bounds(fy_year, quarter)
    out = gst_exposure(
        db,
        period_start=start,
        period_end=end,
        gst_registered=gst_registered,
    )
    out["fy_year"] = fy_year
    out["quarter"] = quarter
    return out


def _empty(start: date, end: date) -> dict:
    return {
        "period_start": start,
        "period_end": end,
        "g1_total_sales": ZERO,
        "g3_gst_free_sales": ZERO,
        "g4_input_taxed_sales": ZERO,
        "g6_sales_subject_to_gst": ZERO,
        "one_a_gst_on_sales": ZERO,
        "g10_capital_purchases": ZERO,
        "g11_non_capital_purchases": ZERO,
        "g14_gst_free_purchases": ZERO,
        "one_b_gst_on_purchases": ZERO,
        "total_purchases": ZERO,
        "net_gst_payable": ZERO,
        "excluded_count": 0,
        "uncategorised_count": 0,
    }
