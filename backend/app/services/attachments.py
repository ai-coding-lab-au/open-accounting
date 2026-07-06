"""Attachment storage.

Files are written to disk under the per-company data folder:
    data/companies/{company_id}/attachments/invoices/{YYYY-MM}/{uuid}.{ext}
Only metadata + the relative path is stored in books.db. Backup = copy the
company folder.

All paths are validated to stay inside the company directory (defense-in-depth
against path traversal via crafted filenames).
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models.company import Attachment


_SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


class AttachmentError(Exception):
    """Raised for invalid attachment operations."""


def _company_root(company_id: str) -> Path:
    root = settings.company_dir(company_id).resolve()
    return root


def _resolve_inside(company_id: str, rel: str) -> Path:
    """Resolve a relative path and ensure it does not escape the company root."""
    root = _company_root(company_id)
    abs_path = (root / rel).resolve()
    try:
        abs_path.relative_to(root)
    except ValueError as e:
        raise AttachmentError(f"Path escapes company root: {rel}") from e
    return abs_path


def _sanitise_ext(filename: str) -> str:
    suffix = Path(filename).suffix.lstrip(".").lower()
    if suffix and _SAFE_EXT_RE.match(suffix):
        return suffix
    return "bin"


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def save_bytes(
    *,
    db: Session,
    company_id: str,
    content: bytes,
    original_filename: str,
    mime_type: str | None = None,
    invoice_id: int | None = None,
    issue_date: datetime | None = None,
) -> Attachment:
    """Persist raw bytes to disk + create an Attachment row.

    De-dupes on sha256: if the same bytes are already on disk for this company,
    reuses the existing file (a new Attachment row is still created so an
    attachment can be linked to multiple invoices independently, but disk usage
    stays flat).
    """
    if not content:
        raise AttachmentError("Empty file")

    sha = _hash_bytes(content)
    ext = _sanitise_ext(original_filename)
    mime = mime_type or mimetypes.guess_type(original_filename)[0] or "application/octet-stream"

    bucket_date = issue_date or datetime.utcnow()
    bucket = bucket_date.strftime("%Y-%m")

    # Check for existing file with this hash for this company.
    existing = db.query(Attachment).filter(Attachment.sha256 == sha).first()
    if existing is not None:
        # Idempotency rule (audit P1): when the caller is uploading the
        # same bytes that are already on disk AND not yet linked to any
        # invoice (typical for /upload-pdf — the user can re-drag the
        # same file without ending up with two orphan attachments), reuse
        # the existing row. A new row is still created when:
        #   - the existing attachment is already linked to an invoice
        #     (the caller may want a separate attachment to link
        #     elsewhere — preserves the original "linked to multiple
        #     invoices independently" use case)
        #   - the caller explicitly attaches to an invoice (invoice_id
        #     not None) — same reason, lets one PDF support two invoices
        rel_path = existing.rel_path
        abs_path = _resolve_inside(company_id, rel_path)
        if not abs_path.exists():
            # File was deleted out-of-band; rewrite it.
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(content)
        if invoice_id is None and existing.invoice_id is None:
            return existing
    else:
        att_id = str(uuid.uuid4())
        rel_path = f"attachments/invoices/{bucket}/{att_id}.{ext}"
        abs_path = _resolve_inside(company_id, rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(content)

    attachment = Attachment(
        id=str(uuid.uuid4()),
        invoice_id=invoice_id,
        filename=Path(original_filename).name,
        mime_type=mime,
        size_bytes=len(content),
        rel_path=rel_path,
        sha256=sha,
    )
    db.add(attachment)
    db.flush()
    return attachment


def read_bytes(*, company_id: str, attachment: Attachment) -> bytes:
    abs_path = _resolve_inside(company_id, attachment.rel_path)
    if not abs_path.exists():
        raise AttachmentError(f"Attachment file missing on disk: {attachment.rel_path}")
    return abs_path.read_bytes()


def attachment_absolute_path(company_id: str, attachment: Attachment) -> Path:
    return _resolve_inside(company_id, attachment.rel_path)


def unlink_file_if_unreferenced(
    *, db: Session, company_id: str, attachment: Attachment
) -> None:
    """Delete an attachment file only when no other row points at it."""
    other = (
        db.query(Attachment.id)
        .filter(
            Attachment.rel_path == attachment.rel_path,
            Attachment.id != attachment.id,
        )
        .first()
    )
    if other is not None:
        return
    attachment_absolute_path(company_id, attachment).unlink(missing_ok=True)
