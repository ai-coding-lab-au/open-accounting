"""Default Australian Chart of Accounts seed.

Numbering follows a common AU SME convention:
  1xxx Assets, 2xxx Liabilities, 3xxx Equity, 4xxx Income, 5xxx COGS, 6xxx Expenses
GST accounts are pre-wired so GST handling works out of the box.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models.company import Account, AccountType


DEFAULT_AU_COA: list[tuple[str, str, AccountType, bool]] = [
    # code, name, type, is_gst
    # Assets
    ("1000", "Bank — Operating Account", AccountType.ASSET, False),
    ("1100", "Accounts Receivable", AccountType.ASSET, False),
    ("1200", "GST Paid (Input Tax Credits)", AccountType.ASSET, True),
    ("1500", "Prepayments", AccountType.ASSET, False),
    ("1700", "Property, Plant & Equipment", AccountType.ASSET, False),
    ("1710", "Accumulated Depreciation", AccountType.ASSET, False),
    # Liabilities
    ("2000", "Accounts Payable", AccountType.LIABILITY, False),
    ("2100", "GST Collected", AccountType.LIABILITY, True),
    ("2200", "PAYG Withholding Payable", AccountType.LIABILITY, False),
    ("2300", "Superannuation Payable", AccountType.LIABILITY, False),
    ("2400", "Credit Card", AccountType.LIABILITY, False),
    ("2500", "Loans Payable", AccountType.LIABILITY, False),
    # Equity
    ("3000", "Owner's Capital", AccountType.EQUITY, False),
    ("3100", "Owner's Drawings", AccountType.EQUITY, False),
    ("3900", "Retained Earnings", AccountType.EQUITY, False),
    # Income
    ("4000", "Sales — Services", AccountType.INCOME, False),
    ("4010", "Sales — Products", AccountType.INCOME, False),
    ("4100", "Interest Income", AccountType.INCOME, False),
    ("4900", "Other Income", AccountType.INCOME, False),
    # Cost of Sales
    ("5000", "Cost of Goods Sold", AccountType.COST_OF_SALES, False),
    ("5100", "Subcontractor Costs", AccountType.COST_OF_SALES, False),
    # Expenses
    ("6000", "Wages & Salaries", AccountType.EXPENSE, False),
    ("6010", "Superannuation Expense", AccountType.EXPENSE, False),
    ("6100", "Rent", AccountType.EXPENSE, False),
    ("6110", "Utilities", AccountType.EXPENSE, False),
    ("6200", "Telephone & Internet", AccountType.EXPENSE, False),
    ("6300", "Motor Vehicle Expenses", AccountType.EXPENSE, False),
    ("6310", "Travel & Accommodation", AccountType.EXPENSE, False),
    ("6400", "Office Supplies", AccountType.EXPENSE, False),
    ("6410", "Software Subscriptions", AccountType.EXPENSE, False),
    ("6500", "Bank Fees", AccountType.EXPENSE, False),
    ("6510", "Merchant Fees", AccountType.EXPENSE, False),
    ("6600", "Accounting & Legal Fees", AccountType.EXPENSE, False),
    ("6700", "Insurance", AccountType.EXPENSE, False),
    ("6800", "Depreciation", AccountType.EXPENSE, False),
    ("6900", "Other Expenses", AccountType.EXPENSE, False),
]


def seed_default_coa(session: Session) -> int:
    """Insert the default CoA if the accounts table is empty. Returns rows inserted."""
    existing = session.query(Account).count()
    if existing > 0:
        return 0
    for code, name, type_, is_gst in DEFAULT_AU_COA:
        session.add(Account(code=code, name=name, type=type_, is_gst=is_gst))
    session.commit()
    return len(DEFAULT_AU_COA)
