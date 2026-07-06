"""Deterministic PDF text/table extraction for bank statements.

No AI: uses pdfplumber (layout-aware) with a pypdf fallback. Scanned / image-only
PDFs have no extractable text — callers get a clear error telling them to export
a CSV/XLSX statement instead (OCR is out of scope).
"""

from __future__ import annotations

import io


class PdfStatementError(ValueError):
    """Raised when a PDF can't be turned into statement text (corrupt, encrypted,
    or scanned). Subclasses ValueError so the API maps it to a 400 like the other
    import errors."""


def extract_text(content: bytes) -> str:
    """Concatenated text of every page. Prefers pdfplumber; falls back to pypdf.

    Raises PdfStatementError when nothing readable comes out (scanned/image PDF).
    """
    text = _extract_with_pdfplumber(content)
    if not text:
        text = _extract_with_pypdf(content)
    if not text:
        raise PdfStatementError(
            "This PDF has no extractable text (it looks scanned). "
            "Export a CSV or XLSX statement from your bank instead."
        )
    return text


def extract_lines(content: bytes) -> list[str]:
    """Non-empty text lines, top-to-bottom, whitespace-trimmed on the right."""
    return [ln.rstrip() for ln in extract_text(content).splitlines() if ln.strip()]


def extract_tables(content: bytes) -> list[list[list[str]]]:
    """Tables pdfplumber can detect (best-effort; empty list if none/unavailable).

    A parser can use this when a statement renders as a real ruled table; most AU
    bank PDFs are positioned text, so the line-based parsers are the main path.
    """
    try:
        import pdfplumber
    except Exception:
        return []
    tables: list[list[list[str]]] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    tables.append([[(cell or "").strip() for cell in row] for row in table])
    except Exception:
        return []
    return tables


def _extract_with_pdfplumber(content: bytes) -> str:
    try:
        import pdfplumber

        parts: list[str] = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _extract_with_pypdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append("")
        return "\n".join(parts).strip()
    except Exception:
        return ""
