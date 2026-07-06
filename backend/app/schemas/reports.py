"""Out-schemas for the reporting endpoints (M3)."""

from __future__ import annotations

from datetime import date
from pydantic import BaseModel

from ._money import Money


# ---------------------------------------------------------------------------
# Monthly bank statement
# ---------------------------------------------------------------------------


class BankStatementRow(BaseModel):
    id: int
    occurred_at: date
    direction: str
    amount: Money
    gst_amount: Money
    memo: str | None
    counter_party_name: str | None
    account_code: str | None
    account_name: str | None
    running_balance: Money


class BankStatementOut(BaseModel):
    bank_account_id: int
    bank_account_name: str
    year: int
    month: int
    period_start: date
    period_end: date
    opening_balance: Money
    closing_balance: Money
    total_in: Money
    total_out: Money
    net_change: Money
    rows: list[BankStatementRow]


# ---------------------------------------------------------------------------
# Profit & Loss
# ---------------------------------------------------------------------------


class PnLLine(BaseModel):
    account_id: int
    code: str
    name: str
    total: Money


class PnLOut(BaseModel):
    period_start: date
    period_end: date
    income_rows: list[PnLLine]
    cogs_rows: list[PnLLine]
    expense_rows: list[PnLLine]
    uncategorised_in: Money
    uncategorised_out: Money
    total_income: Money
    total_cogs: Money
    total_expense: Money
    gross_profit: Money
    net_profit: Money


# ---------------------------------------------------------------------------
# Trial Balance (M2.2)
# ---------------------------------------------------------------------------


class TrialBalanceRow(BaseModel):
    key: str
    kind: str                    # "account" | "bank"
    ref_id: int
    code: str | None
    name: str
    account_type: str | None
    debit_total: Money
    credit_total: Money
    net_debit: Money


class TrialBalanceSupplementary(BaseModel):
    ap_open_total: Money
    ar_open_total: Money


class TrialBalanceOut(BaseModel):
    as_of: date | None
    rows: list[TrialBalanceRow]
    total_debit: Money
    total_credit: Money
    diff: Money
    is_balanced: bool
    uncategorised_bank_in: Money
    uncategorised_bank_out: Money
    supplementary: TrialBalanceSupplementary


# ---------------------------------------------------------------------------
# Balance Sheet (M2.2)
# ---------------------------------------------------------------------------


class BalanceSheetLine(BaseModel):
    account_id: int | None       # None for synthetic lines (e.g. bank, AP/AR rollup)
    code: str | None
    name: str
    balance: Money


class BalanceSheetGroup(BaseModel):
    label: str                   # e.g. "Current Assets"
    lines: list[BalanceSheetLine]
    subtotal: Money


class BalanceSheetOut(BaseModel):
    as_of: date
    assets: list[BalanceSheetGroup]
    liabilities: list[BalanceSheetGroup]
    equity: list[BalanceSheetGroup]
    total_assets: Money
    total_liabilities: Money
    total_equity: Money
    is_balanced: bool
    diff: Money


# ---------------------------------------------------------------------------
# BAS
# ---------------------------------------------------------------------------


class BASOut(BaseModel):
    fy_year: int
    quarter: int
    period_start: date
    period_end: date
    g1_total_sales: Money
    one_a_gst_on_sales: Money
    total_purchases: Money
    one_b_gst_on_purchases: Money
    net_gst_payable: Money
    uncategorised_count: int
    gst_registered: bool


# ---------------------------------------------------------------------------
# GST exposure (M2.3) — richer breakdown than the BAS placeholder above.
# ---------------------------------------------------------------------------


class GSTExposureOut(BaseModel):
    period_start: date
    period_end: date
    fy_year: int | None = None
    quarter: int | None = None

    g1_total_sales: Money
    g3_gst_free_sales: Money
    g4_input_taxed_sales: Money
    g6_sales_subject_to_gst: Money
    one_a_gst_on_sales: Money

    g10_capital_purchases: Money
    g11_non_capital_purchases: Money
    g14_gst_free_purchases: Money
    one_b_gst_on_purchases: Money

    net_gst_payable: Money
    excluded_count: int
    uncategorised_count: int
