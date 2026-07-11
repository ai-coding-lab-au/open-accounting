"""Shared economic-side classification for categorised bank cash events."""

from __future__ import annotations

from ..models.company import AccountType, BankTxnDirection, TaxCode


def bank_event_is_sale(
    *,
    account_code: str | None,
    account_type: AccountType | str | None,
    tax_code: TaxCode | str,
    direction: BankTxnDirection | str,
) -> bool:
    """Return sale-vs-purchase semantics independently of cash direction.

    Cash direction supplies the sign (normal event vs refund), not always the
    economic side. AR is always a sale side, AP/capital/expense/non-AR assets
    are purchase sides, and only otherwise-ambiguous balance-sheet categories
    fall back to direction.
    """
    type_value = (
        account_type.value if hasattr(account_type, "value") else account_type
    )
    tax_value = tax_code.value if hasattr(tax_code, "value") else tax_code
    direction_value = (
        direction.value if hasattr(direction, "value") else direction
    )

    if account_code == "1100":
        return True
    if account_code == "2000":
        return False
    if tax_value == TaxCode.CAPITAL.value:
        return False
    if type_value == AccountType.INCOME.value:
        return True
    if type_value in {
        AccountType.EXPENSE.value,
        AccountType.COST_OF_SALES.value,
        AccountType.ASSET.value,
    }:
        return False
    return direction_value == BankTxnDirection.IN.value
