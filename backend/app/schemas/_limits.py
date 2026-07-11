"""Storage limits shared by write-boundary schemas."""

from __future__ import annotations

from decimal import Decimal


SQLITE_INT_MAX = 2**63 - 1

# SQLAlchemy's SQLite NUMERIC adapter stores values through an IEEE-754
# double. NUMERIC(16, 2)'s theoretical 14 integer digits are wider than the
# range where every cent survives that conversion. Thirteen integer digits
# keep the floating-point error comfortably below half a cent, so a round trip
# back to Decimal(..., 2) is stable.
SQLITE_EXACT_MONEY_MAX = Decimal("9999999999999.99")
