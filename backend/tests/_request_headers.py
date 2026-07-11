"""Request-header helpers shared by API tests."""

from uuid import uuid4


def manual_transaction_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return company headers plus a fresh manual-bank idempotency key."""
    return {**headers, "Idempotency-Key": f"test-manual-{uuid4()}"}
