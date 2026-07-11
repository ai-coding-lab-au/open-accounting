"""Currency validation shared by every financial write schema.

The ledger currently has no exchange-rate model.  Accepting a foreign
currency code would therefore label AUD amounts as if they had been
converted when they have not.  Keep the invariant in one place so new write
paths cannot accidentally implement a weaker version of it.
"""

from __future__ import annotations


def normalise_aud(value: object) -> str:
    """Return canonical ``AUD`` or reject the value.

    Case and surrounding whitespace are harmless input variation.  Every
    other value is unsupported until an FX/rate model is implemented.
    """

    if not isinstance(value, str):
        raise ValueError("currency must be AUD")
    currency = value.strip().upper()
    if currency != "AUD":
        raise ValueError("Only AUD is supported until foreign-exchange rates are implemented")
    return currency
