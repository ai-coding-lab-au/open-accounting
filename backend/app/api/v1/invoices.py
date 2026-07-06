from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...deps import PathId, get_company_db, get_current_company
from ...models.company import (
    Attachment,
    Contact,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    JournalEntry,
    JournalEntrySource,
)
from ...models.master import Company
from ...schemas.invoice import (
    AttachmentOut,
    ExcelImportPayload,
    InvoiceCreate,
    InvoiceOut,
    InvoiceUpdate,
    PdfUploadResult,
    SpreadsheetPreview,
)
from ...services import attachments as attach_svc
from ...services import doc_numbering, excel_import, invoice_posting
from ...services.invoice_math import GstMathError, check_gst_math
from ...services.journal import JournalError
from ...utils.http import safe_filename
from .contacts import get_or_create_contact


# Hard cap on uploads to keep memory + disk under control. 25 MB comfortably
# covers a scanned 50-page invoice PDF or a 10k-row spreadsheet.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _check_gst_math(subtotal: Decimal, gst_amount: Decimal, total: Decimal) -> None:
    try:
        check_gst_math(subtotal, gst_amount, total)
    except GstMathError as exc:
        raise HTTPException(422, str(exc)) from exc


router = APIRouter(prefix="/invoices", tags=["invoices"])


def _excel_source_ref(*, row_no: int | None, parsed: dict) -> str:
    payload = {
        "row_no": row_no,
        "direction": parsed.get("direction"),
        "contact_name": parsed.get("contact_name"),
        "contact_abn": parsed.get("contact_abn"),
        "invoice_number": parsed.get("invoice_number"),
        "issue_date": parsed.get("issue_date"),
        "due_date": parsed.get("due_date"),
        "currency": parsed.get("currency") or "AUD",
        "subtotal": str(parsed.get("subtotal") or "0"),
        "gst_amount": str(parsed.get("gst_amount") or "0"),
        "total": str(parsed.get("total") or "0"),
        "notes": parsed.get("notes"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    return f"row-content:{digest}"


# ---------------------------------------------------------------------------
# List / detail / mutation
# ---------------------------------------------------------------------------


def _entry_summary(entry: JournalEntry) -> dict:
    return {
        "id": entry.id,
        "entry_date": entry.entry_date,
        "memo": entry.memo,
        "source_type": entry.source_type,
        "source_id": entry.source_id,
        "reverses_entry_id": entry.reverses_entry_id,
    }


def _attach_journal_entries(db: Session, inv: Invoice) -> Invoice:
    inv.journal_entries = (
        db.query(JournalEntry)
        .filter(
            JournalEntry.source_id == inv.id,
            JournalEntry.source_type.in_(
                [
                    JournalEntrySource.INVOICE_AR,
                    JournalEntrySource.INVOICE_AP,
                    JournalEntrySource.INVOICE_REVERSAL,
                ]
            ),
        )
        .order_by(JournalEntry.id)
        .all()
    )
    return inv


def _serialize(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "direction": inv.direction,
        "contact_id": inv.contact_id,
        "contact_name": inv.contact.name if inv.contact else None,
        "invoice_number": inv.invoice_number,
        "issue_date": inv.issue_date,
        "due_date": inv.due_date,
        "currency": inv.currency,
        "subtotal": inv.subtotal,
        "gst_amount": inv.gst_amount,
        "total": inv.total,
        "gst_inclusive": inv.gst_inclusive,
        "status": inv.status,
        "paid_amount": inv.paid_amount,
        "paid_date": inv.paid_date,
        "authorised_at": inv.authorised_at,
        "notes": inv.notes,
        "source": inv.source,
        "source_ref": inv.source_ref,
        "created_at": inv.created_at,
        "updated_at": inv.updated_at,
        "attachments": [
            {
                "id": a.id,
                "filename": a.filename,
                "mime_type": a.mime_type,
                "size_bytes": a.size_bytes,
                "uploaded_at": a.uploaded_at,
            }
            for a in inv.attachments
        ],
        "journal_entries": [
            _entry_summary(e)
            for e in sorted(
                inv.journal_entries if hasattr(inv, "journal_entries") else [],
                key=lambda e: e.id,
            )
        ],
    }


@router.get("", response_model=list[InvoiceOut])
def list_invoices(
    direction: str | None = Query(default=None, pattern="^(AP|AR)$"),
    status: str | None = Query(default=None, pattern="^(draft|authorised|unpaid|partial|paid|void)$"),
    contact_id: int | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    q: str | None = Query(default=None, description="Search invoice_number or contact name"),
    limit: int | None = Query(default=None, ge=1, le=10000, description="Optional row cap; default returns all matching rows"),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    query = db.query(Invoice).join(Contact, Invoice.contact_id == Contact.id)
    if direction:
        query = query.filter(Invoice.direction == direction)
    if status:
        query = query.filter(Invoice.status == status)
    if contact_id:
        query = query.filter(Invoice.contact_id == contact_id)
    if from_date:
        query = query.filter(Invoice.issue_date >= from_date)
    if to_date:
        query = query.filter(Invoice.issue_date <= to_date)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Invoice.invoice_number.ilike(like), Contact.name.ilike(like)))
    query = query.order_by(Invoice.issue_date.desc(), Invoice.id.desc())
    if limit is not None:
        query = query.limit(limit)
    rows = query.all()
    return [_serialize(_attach_journal_entries(db, r)) for r in rows]


@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(
    invoice_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return _serialize(_attach_journal_entries(db, inv))


def _find_source_ref_collision(db: Session, source, source_ref: str | None) -> Invoice | None:
    """Return the existing Invoice that owns (source, source_ref), if any.

    Used by both create_invoice and the Excel batch importer so the user
    sees the same friendly message regardless of entry path (audit P1 #2,
    re-audit polish). The DB partial UNIQUE index is still the race-safe
    guard — this pre-check just turns the IntegrityError into a 409.
    """
    if not source_ref:
        return None
    return (
        db.query(Invoice)
        .filter(Invoice.source == source, Invoice.source_ref == source_ref)
        .first()
    )


def _source_ref_collision_message(source, source_ref: str, existing: Invoice) -> str:
    return (
        f"An invoice with source={source!r} source_ref={source_ref!r} already "
        f"exists (id={existing.id}, number={existing.invoice_number!r})."
    )


@router.post("", response_model=InvoiceOut, status_code=201)
def create_invoice(
    payload: InvoiceCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    _check_gst_math(payload.subtotal, payload.gst_amount, payload.total)
    contact = _resolve_contact(db, payload)

    existing = _find_source_ref_collision(db, payload.source, payload.source_ref)
    if existing is not None:
        raise HTTPException(
            409,
            _source_ref_collision_message(payload.source, payload.source_ref, existing),
        )

    inv = Invoice(
        direction=payload.direction,
        contact_id=contact.id,
        invoice_number=payload.invoice_number,
        issue_date=payload.issue_date,
        due_date=payload.due_date,
        currency=payload.currency,
        subtotal=payload.subtotal,
        gst_amount=payload.gst_amount,
        total=payload.total,
        gst_inclusive=payload.gst_inclusive,
        notes=payload.notes,
        source=payload.source,
        source_ref=payload.source_ref,
        status=InvoiceStatus.DRAFT,
    )
    if payload.lines:
        for li in payload.lines:
            inv.lines.append(InvoiceLine(**li.model_dump()))
    db.add(inv)
    try:
        db.flush()
    except Exception as e:
        db.rollback()
        raise HTTPException(409, f"Duplicate invoice (direction + contact + number): {e}") from e

    if payload.attachment_id:
        att = db.get(Attachment, payload.attachment_id)
        if att is None:
            raise HTTPException(404, f"Attachment {payload.attachment_id} not found")
        if att.invoice_id is not None and att.invoice_id != inv.id:
            raise HTTPException(409, "Attachment is already linked to another invoice")
        att.invoice_id = inv.id

    requested_status = payload.status or InvoiceStatus.DRAFT.value
    if requested_status != InvoiceStatus.DRAFT.value:
        if requested_status == InvoiceStatus.VOID.value:
            # A void-on-create invoice would have no journal entry to reverse
            # and no way to ever be deleted — an unremovable zombie row.
            raise HTTPException(422, "Cannot create an invoice as void.")
        try:
            invoice_posting.post_invoice(db, inv.id)
            if requested_status in {
                InvoiceStatus.UNPAID.value,
                InvoiceStatus.PARTIAL.value,
                InvoiceStatus.PAID.value,
            }:
                inv.status = requested_status
        except invoice_posting.InvoicePostingError as e:
            db.rollback()
            _raise_posting_http(e)
        except (JournalError, ValidationError) as e:
            # Same operator-fixable class as on the /post endpoint — never 500.
            db.rollback()
            raise HTTPException(422, f"Posting failed ledger validation: {e}") from e

    db.commit()
    db.refresh(inv)
    return _serialize(_attach_journal_entries(db, inv))


def _raise_posting_http(exc: invoice_posting.InvoicePostingError) -> None:
    raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc


def _is_draft(inv: Invoice) -> bool:
    return inv.status == InvoiceStatus.DRAFT or inv.status == InvoiceStatus.DRAFT.value


def _is_void(inv: Invoice) -> bool:
    return inv.status == InvoiceStatus.VOID or inv.status == InvoiceStatus.VOID.value


def _resolve_contact(db: Session, payload: InvoiceCreate) -> Contact:
    if payload.contact_id:
        c = db.get(Contact, payload.contact_id)
        if c is None:
            raise HTTPException(404, f"Contact {payload.contact_id} not found")
        return c
    if not payload.contact_name:
        raise HTTPException(422, "Either contact_id or contact_name must be provided")
    kind = "supplier" if payload.direction == "AP" else "customer"
    return get_or_create_contact(db=db, name=payload.contact_name, kind=kind, abn=payload.contact_abn)


@router.patch("/{invoice_id}", response_model=InvoiceOut)
def update_invoice(
    invoice_id: PathId,
    payload: InvoiceUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes:
        # Status transitions go through the post/void endpoints only; payment
        # status is derived from paid_amount below. Allowing a raw status write
        # let a posted invoice be demoted to draft and hard-deleted, orphaning
        # its locked journal entry (audit P0-E).
        raise HTTPException(
            422,
            "Status cannot be changed via PATCH. Use the post/void endpoints; "
            "payment status is derived from paid_amount.",
        )
    if not _is_draft(inv):
        if _is_void(inv) and ({"paid_amount", "paid_date"} & set(changes)):
            # A void invoice's journal entry is reversed; recording a payment
            # would resurrect it to "paid" with zero GL backing.
            raise HTTPException(409, "Invoice is void — payments cannot be recorded on it.")
        if {"paid_amount", "paid_date"} & set(changes):
            raise HTTPException(
                409,
                "Invoice payment status is document-only until bank clearing is implemented. "
                "Record the bank transaction against the AR/AP control account instead.",
            )
        forbidden = (
            set(changes)
            - {"notes"}
        )
        if forbidden:
            raise HTTPException(422, f"Posted invoices are locked for financial edits: {sorted(forbidden)}")
    elif "paid_amount" in changes or "paid_date" in changes:
        # A never-posted draft has no AR/AP journal entry, so "paying" it would
        # mark it paid with zero GL posting and wedge it (audit P1-8).
        raise HTTPException(
            422,
            "Cannot record a payment on a draft invoice — post the invoice first.",
        )

    lines = changes.pop("lines", None)
    for field, value in changes.items():
        setattr(inv, field, value)
    if lines is not None:
        inv.lines.clear()
        db.flush()
        for li in lines:
            inv.lines.append(InvoiceLine(**li))

    # Re-validate totals after any change touching them.
    _check_gst_math(inv.subtotal, inv.gst_amount, inv.total)
    # TODO(payment-posting): paid_amount/status changes currently update only
    # the invoice document; the Bank ↔ AR/AP journal for payment matching is
    # a separate increment.
    if payload.paid_amount is not None:
        if Decimal(inv.paid_amount) <= 0:
            inv.status = "unpaid"
        elif Decimal(inv.paid_amount) >= Decimal(inv.total):
            inv.status = "paid"
        else:
            inv.status = "partial"
    db.commit()
    db.refresh(inv)
    return _serialize(_attach_journal_entries(db, inv))


@router.post("/{invoice_id}/post")
def post_invoice_endpoint(
    invoice_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    # Take the write lock BEFORE the idempotency SELECT inside post_invoice, so
    # two concurrent posts serialise: the loser sees the winner's entry and gets
    # AlreadyPosted (409) instead of racing to INSERT and hitting the DB unique
    # index → IntegrityError → 500 (audit round-3 P2, matching recognise/convert).
    doc_numbering._begin_sqlite_immediate(db)
    try:
        entry = invoice_posting.post_invoice(db, invoice_id)
        db.commit()
    except invoice_posting.InvoicePostingError as e:
        db.rollback()
        _raise_posting_http(e)
    except (JournalError, ValidationError) as e:
        # Ledger-level validation (e.g. unbalanced entry, negative line) is an
        # operator-fixable invoice problem, not a server fault — 422, never 500.
        db.rollback()
        raise HTTPException(422, f"Posting failed ledger validation: {e}") from e
    inv = db.get(Invoice, invoice_id)
    return {"invoice": _serialize(_attach_journal_entries(db, inv)), "journal_entry": _entry_summary(entry)}


@router.post("/{invoice_id}/void")
def void_invoice_endpoint(
    invoice_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    # Same write-lock treatment as /post: serialise concurrent voids so the loser
    # sees the winner's reversal and gets AlreadyVoided (409), not a 500 from the
    # uq_journal_source_doc unique index (audit round-3 P2).
    doc_numbering._begin_sqlite_immediate(db)
    try:
        entry = invoice_posting.void_invoice(db, invoice_id)
        db.commit()
    except invoice_posting.InvoicePostingError as e:
        db.rollback()
        _raise_posting_http(e)
    inv = db.get(Invoice, invoice_id)
    return {"invoice": _serialize(_attach_journal_entries(db, inv)), "journal_entry": _entry_summary(entry)}


@router.delete("/{invoice_id}", status_code=204)
def void_invoice(
    invoice_id: PathId,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    if _is_draft(inv):
        # Never hard-delete an invoice that has journal entries (belt-and-braces:
        # status PATCH no longer allows demoting a posted invoice to draft, but a
        # drifted row must not orphan a permanently locked entry — audit P0-E).
        linked_entry = (
            db.query(JournalEntry)
            .filter(
                JournalEntry.source_id == inv.id,
                JournalEntry.source_type.in_(
                    [
                        JournalEntrySource.INVOICE_AR,
                        JournalEntrySource.INVOICE_AP,
                        JournalEntrySource.INVOICE_REVERSAL,
                    ]
                ),
            )
            .first()
        )
        if linked_entry is not None:
            raise HTTPException(
                409,
                f"Invoice has journal entries (entry {linked_entry.id}) and cannot "
                "be deleted. Use the void endpoint instead.",
            )
        # The Attachment FK is ON DELETE SET NULL, so deleting the invoice would
        # leave the attachment row + its file on disk orphaned. Delete the linked
        # attachments and unlink de-duplicated files only when no other row
        # still points at the same rel_path.
        for att in list(inv.attachments):
            try:
                attach_svc.unlink_file_if_unreferenced(
                    db=db, company_id=company.id, attachment=att
                )
            except attach_svc.AttachmentError:
                pass
            db.delete(att)
        db.delete(inv)
        db.commit()
        return None
    try:
        invoice_posting.void_invoice(db, invoice_id)
        db.commit()
    except invoice_posting.InvoicePostingError as e:
        db.rollback()
        _raise_posting_http(e)
    return None


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@router.get("/{invoice_id}/attachment", response_class=FileResponse)
def download_attachment(
    invoice_id: PathId,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    inv = db.get(Invoice, invoice_id)
    if inv is None or not inv.attachments:
        raise HTTPException(404, "No attachment for this invoice")
    att = inv.attachments[0]
    abs_path = attach_svc.attachment_absolute_path(company.id, att)
    if not abs_path.exists():
        raise HTTPException(410, "Attachment file is missing on disk")
    safe_name = safe_filename(att.filename, default="attachment")
    # Serve a fixed, safe content type rather than the client-supplied mime_type:
    # the upload path enforces a .pdf extension, so application/pdf is correct,
    # and it prevents a malicious upload (Content-Type: text/html) from being
    # served inline as active content (stored XSS).
    return FileResponse(
        path=str(abs_path),
        media_type="application/pdf",
        filename=safe_name,
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


# ---------------------------------------------------------------------------
# PDF attachment upload
# ---------------------------------------------------------------------------


@router.post("/upload-pdf", response_model=PdfUploadResult)
async def upload_pdf(
    file: UploadFile = File(...),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"File too large ({len(content) // (1024 * 1024)} MB). "
            f"Maximum is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    # Save the file first (idempotent on sha256) so the user can re-confirm without re-uploading.
    try:
        att = attach_svc.save_bytes(
            db=db,
            company_id=company.id,
            content=content,
            original_filename=file.filename,
            mime_type=file.content_type or "application/pdf",
        )
        db.commit()
    except attach_svc.AttachmentError as e:
        raise HTTPException(400, str(e)) from e

    return PdfUploadResult(
        attachment_id=att.id,
        filename=att.filename,
        size_bytes=att.size_bytes,
    )


# ---------------------------------------------------------------------------
# Excel/CSV preview + bulk import
# ---------------------------------------------------------------------------


@router.post("/upload-excel", response_model=SpreadsheetPreview)
async def upload_excel(
    file: UploadFile = File(...),
    _: Company = Depends(get_current_company),
    __: Session = Depends(get_company_db),
):
    if not file.filename:
        raise HTTPException(400, "Missing filename")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"File too large ({len(content) // (1024 * 1024)} MB). "
            f"Maximum is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    try:
        preview = excel_import.parse_spreadsheet(content=content, filename=file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"Failed to parse spreadsheet: {e}") from e
    return preview


@router.post("/import-excel-rows")
def import_excel_rows(
    payload: ExcelImportPayload,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    """Bulk-create invoices from a pre-mapped spreadsheet preview.

    Returns {created: [...ids], skipped: [{row, reason}]}.

    Each row is wrapped in a SAVEPOINT so a duplicate/validation failure on one
    row does not poison the rest of the batch.
    """
    mapping = payload.mapping
    rows = payload.rows
    direction_default = payload.direction_default

    created: list[int] = []
    skipped: list[dict] = []

    for r in rows:
        raw = r.raw
        try:
            parsed = excel_import.apply_mapping(raw_row=raw, mapping=mapping)
        except Exception as e:
            skipped.append({"row": r.row_no, "reason": f"parse error: {e}"})
            continue

        direction = parsed.get("direction") or r.direction_default or direction_default
        contact_name = parsed.get("contact_name")
        if not contact_name:
            skipped.append({"row": r.row_no, "reason": "missing contact_name"})
            continue
        if not parsed.get("invoice_number"):
            skipped.append({"row": r.row_no, "reason": "missing invoice_number"})
            continue
        if not parsed.get("issue_date"):
            skipped.append({"row": r.row_no, "reason": "missing or unparseable issue_date"})
            continue
        if parsed.get("total") is None:
            skipped.append({"row": r.row_no, "reason": "missing total"})
            continue

        sub = Decimal(parsed.get("subtotal") or "0")
        gst = Decimal(parsed.get("gst_amount") or "0")
        total = Decimal(parsed.get("total"))
        if sub == 0 and gst == 0 and total > 0:
            # Assume GST-inclusive total, derive at 10%. ROUND_HALF_UP keeps us
            # in step with ATO BAS rounding (banker's rounding would round .5 down
            # in half the cases, leaving the line off by a cent).
            sub = (total / Decimal("1.10")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            gst = (total - sub).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        source_ref = _excel_source_ref(
            row_no=r.row_no,
            parsed={**parsed, "direction": direction},
        )
        existing = _find_source_ref_collision(db, "excel", source_ref)
        if existing is not None:
            skipped.append({
                "row": r.row_no,
                "reason": _source_ref_collision_message("excel", source_ref, existing),
            })
            continue

        # Per-row SAVEPOINT: a failure rolls back only this row's writes
        # (including the get_or_create_contact insert) so the batch survives.
        sp = db.begin_nested()
        try:
            kind = "supplier" if direction == "AP" else "customer"
            contact = get_or_create_contact(
                db=db, name=contact_name, kind=kind, abn=parsed.get("contact_abn")
            )

            inv = Invoice(
                direction=direction,
                contact_id=contact.id,
                invoice_number=parsed["invoice_number"],
                issue_date=date.fromisoformat(parsed["issue_date"]),
                due_date=date.fromisoformat(parsed["due_date"]) if parsed.get("due_date") else None,
                currency=parsed.get("currency") or "AUD",
                subtotal=sub,
                gst_amount=gst,
                total=total,
                gst_inclusive=True,
                notes=parsed.get("notes"),
                source="excel",
                source_ref=source_ref,
            )
            db.add(inv)
            db.flush()
            sp.commit()
            created.append(inv.id)
        except Exception as e:
            sp.rollback()
            skipped.append({"row": r.row_no, "reason": f"db error (likely duplicate): {e}"})

    db.commit()
    return {"created": created, "skipped": skipped}
