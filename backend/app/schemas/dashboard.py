"""Out-schemas for the dashboard summary endpoint."""

from __future__ import annotations

from datetime import date
from pydantic import BaseModel

from ._money import Money


class BankAccountSummary(BaseModel):
    id: int
    name: str
    balance: Money


class DashboardTxnRow(BaseModel):
    id: int
    occurred_at: date
    direction: str
    amount: Money
    memo: str | None
    counter_party_name: str | None
    account_code: str | None
    account_name: str | None


class DashboardApRow(BaseModel):
    id: int
    invoice_number: str
    contact_name: str | None
    issue_date: date
    due_date: date | None
    total: Money
    outstanding: Money
    is_overdue: bool


class PeriodOut(BaseModel):
    start: date
    end: date


class QuarterOut(BaseModel):
    fy_year: int
    quarter: int
    start: date
    end: date


class DashboardSummary(BaseModel):
    as_of: date
    fy_year: int
    fy_period: PeriodOut
    current_month: PeriodOut
    current_quarter: QuarterOut

    bank_accounts: list[BankAccountSummary]
    business_total: Money
    unpaid_ap_total: Money
    overdue_ap_count: int

    fy_net_profit: Money
    fy_total_income: Money
    fy_total_expense: Money

    month_income: Money
    month_expense: Money
    month_uncategorised_in: Money
    month_uncategorised_out: Money

    tb_balanced: bool
    tb_diff: Money
    tb_uncategorised_in: Money
    tb_uncategorised_out: Money

    recent_business_txns: list[DashboardTxnRow]
    unpaid_ap: list[DashboardApRow]
