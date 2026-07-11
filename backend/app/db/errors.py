"""Storage-integrity failures that require operator-led recovery."""

from __future__ import annotations


class DataRecoveryRequiredError(RuntimeError):
    """Refuse to create over or continue past missing/orphaned ledger files."""

    code = "DATA_RECOVERY_REQUIRED"
