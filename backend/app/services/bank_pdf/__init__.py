"""PDF bank-statement import (deterministic, per-bank, no AI).

`parse_pdf` returns the same ``(headers, data_rows)`` shape as the CSV/XLSX
readers so it plugs straight into ``bank_import.parse_statement`` and the rest of
the preview → dedup → commit pipeline.
"""

from __future__ import annotations

from .extract import (
    PdfStatementError,
    extract_lines,
    extract_tables,
    extract_text,
)
from .parsers import (
    BANK_PARSERS,
    SUPPORTED_BANKS,
    detect_bank,
    parse_generic,
)

__all__ = [
    "PdfStatementError",
    "SUPPORTED_BANKS",
    "parse_pdf",
    "extract_text",
    "extract_lines",
    "extract_tables",
    "detect_bank",
]

# Column headers of the synthetic table PDF rows are mapped through. These match
# the CSV/XLSX header hints in bank_import.propose_mapping (Date -> occurred_at,
# Description -> memo, Counter-party -> counter_party_name, Amount -> signed amount).
_HEADERS = ["Date", "Description", "Counter-party", "Amount"]

_AUTO = {"", "auto", "auto-detect", "autodetect", "detect"}


def parse_pdf(content: bytes, bank_format: str | None = None) -> tuple[list[str], list[list[str]]]:
    """Extract statement rows from a PDF into (headers, data_rows).

    `bank_format` selects a bank-specific parser; None / "auto" auto-detects from
    the statement text and falls back to the generic parser when unrecognised.
    Raises PdfStatementError for scanned/unreadable PDFs.
    """
    fmt = (bank_format or "").strip().lower()
    if fmt in _AUTO:
        detected = detect_bank(extract_text(content))  # raises on scanned PDF
        parser = BANK_PARSERS.get(detected, parse_generic)
    else:
        parser = BANK_PARSERS.get(fmt, parse_generic)

    rows = parser(content)
    data_rows = [
        [r["date"], r["description"], r.get("counter_party") or "", r["amount"]]
        for r in rows
    ]
    return _HEADERS, data_rows
