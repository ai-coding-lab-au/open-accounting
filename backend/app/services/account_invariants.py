"""Chart-of-accounts invariants that posting and reporting depend on."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    AccountType,
    BankTransaction,
    InvoiceLine,
    JournalLine,
)


OPENING_BALANCE_EQUITY_ACCOUNT_CODE = "3000"
OPENING_BALANCE_EQUITY_ACCOUNT_TYPE = AccountType.EQUITY


@dataclass(frozen=True)
class SystemAccountSpec:
    code: str
    default_name: str
    type: AccountType
    is_gst: bool


# Posting/reporting identify these accounts exclusively by code; existing
# operator-edited names are display metadata and must be preserved. The names
# below are defaults only when a missing legacy row must be recreated.
SYSTEM_ACCOUNT_SPECS: dict[str, SystemAccountSpec] = {
    "1100": SystemAccountSpec(
        "1100", "Accounts Receivable", AccountType.ASSET, False
    ),
    "1200": SystemAccountSpec(
        "1200", "GST Paid (Input Tax Credits)", AccountType.ASSET, True
    ),
    "1500": SystemAccountSpec(
        "1500", "Supplier Prepayments", AccountType.ASSET, False
    ),
    "2000": SystemAccountSpec(
        "2000", "Accounts Payable", AccountType.LIABILITY, False
    ),
    "2050": SystemAccountSpec(
        "2050", "Customer Deposits", AccountType.LIABILITY, False
    ),
    "2100": SystemAccountSpec(
        "2100", "GST Collected", AccountType.LIABILITY, True
    ),
    "3000": SystemAccountSpec(
        "3000", "Owner's Capital", AccountType.EQUITY, False
    ),
}


# These four codes are referenced as control accounts by invoice posting,
# settlement, BAS and report projection.
PROTECTED_CONTROL_ACCOUNT_TYPES: dict[str, AccountType] = {
    code: SYSTEM_ACCOUNT_SPECS[code].type
    for code in ("1100", "1200", "2000", "2100")
}

# 3000 is not an AR/AP/GST control account, but it is the canonical contra
# account for every BankAccount opening balance. Its identity and availability
# are therefore just as structural as the four control accounts above.
PROTECTED_SYSTEM_ACCOUNT_TYPES: dict[str, AccountType] = {
    code: spec.type for code, spec in SYSTEM_ACCOUNT_SPECS.items()
}


class AccountInvariantError(ValueError):
    """A required chart-of-accounts invariant is missing or corrupted."""


def protected_control_type(code: str) -> AccountType | None:
    return PROTECTED_CONTROL_ACCOUNT_TYPES.get(code)


def protected_system_account_type(code: str) -> AccountType | None:
    """Return the immutable type for any posting/reporting system account."""
    return PROTECTED_SYSTEM_ACCOUNT_TYPES.get(code)


def financial_reference_labels(db: Session, account_id: int) -> list[str]:
    """Financial rows whose historic meaning depends on an account's type."""
    labels: list[str] = []
    if (
        db.query(BankTransaction.id)
        .filter(
            (BankTransaction.account_id == account_id)
            | (BankTransaction.unapplied_account_id == account_id)
        )
        .first()
    ):
        labels.append("bank transactions")
    if db.query(InvoiceLine.id).filter(InvoiceLine.account_id == account_id).first():
        labels.append("invoice lines")
    if db.query(JournalLine.id).filter(JournalLine.account_id == account_id).first():
        labels.append("journal entries")
    return labels


def require_opening_balance_equity_account(db: Session) -> Account:
    """Return the usable canonical opening-balance contra account or fail closed."""
    account = (
        db.query(Account)
        .filter(Account.code == OPENING_BALANCE_EQUITY_ACCOUNT_CODE)
        .one_or_none()
    )
    operator_action = (
        "Restore account 3000 as an active EQUITY account before recording or "
        "reporting bank opening balances."
    )
    if account is None:
        raise AccountInvariantError(
            f"Required opening-balance equity account 3000 is missing. {operator_action}"
        )
    if account.type != OPENING_BALANCE_EQUITY_ACCOUNT_TYPE:
        raise AccountInvariantError(
            "Required opening-balance account 3000 has an invalid account type. "
            + operator_action
        )
    if not account.active:
        raise AccountInvariantError(
            "Required opening-balance equity account 3000 is inactive. "
            + operator_action
        )
    return account
