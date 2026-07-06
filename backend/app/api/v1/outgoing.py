"""Outgoing documents API — Receipt only.

Receipts are created directly (pick a client + line items → an issued receipt
with a PDF). They can be voided and restored. Service Agreement / Payment
Request / outgoing-Invoice / Partner-document workflows were removed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import settings
from ...deps import PathId, get_company_db, get_current_company
from ...models.company import Client
from ...models.outgoing import (
    DocumentCounter,
    DocumentStatus,
    DocumentType,
    OutgoingDocument,
    OutgoingDocumentLine,
)
from ...models.master import Company
from ...schemas.outgoing import (
    CounterOut,
    CounterSet,
    OutgoingCreate,
    OutgoingLineIn,
    OutgoingOut,
    OutgoingUpdate,
)
from ...services import doc_numbering, html_render, pdf_render
from ...services.attachments import AttachmentError, _resolve_inside
from ...hooks import register_contact_reference_check
from ...utils.http import safe_filename


def _resolve_override_number(raw: str, dt: DocumentType, issue_date: date) -> str:
    """Turn a user-supplied document-number override into the stored number.

    The user types only the running serial (e.g. "222" or "0222"); the prefix
    and year are added automatically -> RCT-2026-0222-1. A non-numeric override
    is taken verbatim (back-compat escape hatch for a fully custom number).
    """
    text = (raw or "").strip()
    if text.isdigit():
        return doc_numbering.number_from_serial(dt, int(text), issue_date.year)
    return text


def _discard_doc_pdf(doc: OutgoingDocument, company_id: str) -> None:
    """Unlink a published PDF from disk and clear its rel_path.

    The PDF persists client PII; a void that merely nulled pdf_rel_path left the
    file orphaned on disk (audit). Resolve the path inside the company root (same
    guard the attachments layer uses) before unlinking.
    """
    rel = doc.pdf_rel_path
    if rel:
        try:
            _resolve_inside(company_id, rel).unlink(missing_ok=True)
        except AttachmentError:
            # Path escapes the company root (corrupt rel_path) — never unlink
            # outside the sandbox; just drop the reference.
            pass
    doc.pdf_rel_path = None


router = APIRouter(prefix="/outgoing", tags=["outgoing"])


def _outgoing_documents_reference_contact(db: Session, contact_id: int) -> str | None:
    """Veto Contact deletion if any OutgoingDocument still points at it.

    Registered as an M1 hook so the contacts router can run this check
    without importing OutgoingDocument directly.
    """
    found = (
        db.query(OutgoingDocument)
        .filter(OutgoingDocument.customer_id == contact_id)
        .first()
    )
    if found is None:
        return None
    return (
        "Contact is referenced by outgoing documents (receipts). "
        "Remove or reassign those first."
    )


register_contact_reference_check(_outgoing_documents_reference_contact)


# ---------------------------------------------------------------------------
# Counters (visible / editable from the Settings page)
# ---------------------------------------------------------------------------


@router.get("/counters", response_model=list[CounterOut])
def list_counters(
    year: int | None = Query(default=None),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    """List all counters, optionally filtered by year. Also computes the next preview number."""
    q = db.query(DocumentCounter)
    if year is not None:
        q = q.filter(DocumentCounter.year == year)
    rows = q.order_by(DocumentCounter.year.desc(), DocumentCounter.doc_type.asc()).all()
    return [
        CounterOut(
            doc_type=r.doc_type.value if hasattr(r.doc_type, "value") else r.doc_type,
            year=r.year,
            last_number=r.last_number,
            next_preview=doc_numbering.format_number(
                DocumentType(r.doc_type), r.year, r.last_number + 1
            ),
        )
        for r in rows
    ]


@router.put("/counters", response_model=CounterOut)
def set_counter(
    payload: CounterSet,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    dt = DocumentType(payload.doc_type)
    try:
        doc_numbering.set_counter(db, dt, payload.year, payload.last_number)
    except ValueError as e:
        # Rewinding below the highest issued serial would make every following
        # auto-numbered create collide — clean 409 instead of a wedged 500 loop.
        raise HTTPException(409, str(e))
    db.commit()
    return CounterOut(
        doc_type=dt.value,
        year=payload.year,
        last_number=payload.last_number,
        next_preview=doc_numbering.format_number(dt, payload.year, payload.last_number + 1),
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _compute_totals(lines: list[OutgoingLineIn]) -> tuple[Decimal, Decimal, list[dict]]:
    """Returns (subtotal, subtotal-again, list-of-cleaned-line-dicts).

    Line amounts are pre-GST. The caller adds GST on top (10% when
    `company.gst_registered`) and persists `gst_amount` and `total`.
    """
    cleaned: list[dict] = []
    subtotal = Decimal("0")
    for li in lines:
        qty = Decimal(li.quantity)
        unit = Decimal(li.unit_price)
        amt = li.amount if li.amount is not None else qty * unit
        amt = Decimal(amt).quantize(Decimal("0.01"))
        cleaned.append(
            {"description": li.description, "quantity": qty, "unit_price": unit, "amount": amt}
        )
        subtotal += amt
    subtotal = subtotal.quantize(Decimal("0.01"))
    return subtotal, subtotal, cleaned


def _serialize(doc: OutgoingDocument, db: Session | None = None) -> dict:
    return {
        "id": doc.id,
        "doc_type": doc.doc_type.value if hasattr(doc.doc_type, "value") else doc.doc_type,
        "doc_number": doc.doc_number,
        "issue_date": doc.issue_date,
        "customer_id": doc.customer_id,
        "client_ref_id": doc.client_ref_id,
        "customer_name": doc.customer_name,
        "customer_address": doc.customer_address,
        "customer_abn": doc.customer_abn,
        "customer_email": doc.customer_email,
        "customer_phone": doc.customer_phone,
        "currency": doc.currency,
        "subtotal": doc.subtotal,
        "gst_amount": doc.gst_amount,
        "total": doc.total,
        "status": doc.status.value if hasattr(doc.status, "value") else doc.status,
        "paid_date": doc.paid_date,
        "payment_method": doc.payment_method,
        "notes": doc.notes,
        "pdf_rel_path": doc.pdf_rel_path,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
        "lines": [
            {
                "id": ln.id,
                "order_no": ln.order_no,
                "description": ln.description,
                "quantity": ln.quantity,
                "unit_price": ln.unit_price,
                "amount": ln.amount,
            }
            for ln in doc.lines
        ],
    }


def _client_payload_or_404(db: Session, client_ref_id: int | None) -> Client:
    if client_ref_id is None:
        raise HTTPException(400, "Select an existing client before creating this document")
    client = db.get(Client, client_ref_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    if not client.is_active:
        raise HTTPException(400, "Selected client is inactive")
    return client


def _money(v: Decimal) -> Decimal:
    return Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@router.get("", response_model=list[OutgoingOut])
def list_documents(
    doc_type: str | None = Query(default=None, pattern="^receipt$"),
    status: str | None = Query(default=None, pattern="^(draft|issued|void)$"),
    q: str | None = None,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    query = db.query(OutgoingDocument)
    if doc_type:
        query = query.filter(OutgoingDocument.doc_type == DocumentType(doc_type))
    if status:
        query = query.filter(OutgoingDocument.status == DocumentStatus(status))
    if q:
        like = f"%{q}%"
        query = query.filter(
            (OutgoingDocument.doc_number.ilike(like))
            | (OutgoingDocument.customer_name.ilike(like))
        )
    rows = (
        query.order_by(OutgoingDocument.issue_date.desc(), OutgoingDocument.id.desc())
        .limit(500)
        .all()
    )
    return [_serialize(r, db) for r in rows]


@router.get("/{doc_id}", response_model=OutgoingOut)
def get_document(
    doc_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc = db.get(OutgoingDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return _serialize(doc, db)


@router.post("", response_model=OutgoingOut, status_code=201)
def create_document(
    payload: OutgoingCreate,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    dt = DocumentType.RECEIPT

    has_doc_number_override = bool(payload.doc_number_override)
    if has_doc_number_override:
        # Take the write lock BEFORE the existence pre-check. The check is a
        # SELECT, which opens a deferred read transaction; if we let that happen
        # first, the later BEGIN IMMEDIATE (inside advance_sequence_for_override)
        # no-ops and two concurrent overrides can collide as a raw "database is
        # locked" 500 instead of the clean 409 below. The auto-number path takes
        # this lock up front too.
        doc_numbering._begin_sqlite_immediate(db)
        doc_number = _resolve_override_number(
            payload.doc_number_override, dt, payload.issue_date
        )
        # Pre-check the override so a duplicate returns a clean message rather
        # than surfacing the raw DB constraint error (mirrors accounts.py).
        if doc_numbering._doc_number_exists(db, dt, doc_number):
            raise HTTPException(409, f"Document number {doc_number} already exists")
    else:
        try:
            doc_number = doc_numbering.next_number(db, dt, issue_date=payload.issue_date)
        except RuntimeError as e:
            # Sequence collided with an existing number (e.g. a legacy DB whose
            # counter drifted behind its documents): 409 with guidance, not a 500.
            raise HTTPException(
                409,
                f"{e}. Raise the document counter in Settings above the highest "
                "issued number, then retry.",
            )

    client = _client_payload_or_404(db, payload.client_ref_id)

    subtotal, _line_total, line_dicts = _compute_totals(payload.lines)
    # Apply 10% GST on top when the company is GST-registered.
    gst_amount = (
        _money(subtotal * Decimal("0.10")) if company.gst_registered else Decimal("0.00")
    )
    total = _money(subtotal + gst_amount)
    paid_date = payload.paid_date or payload.issue_date
    payment_method = payload.payment_method or "Bank transfer"

    doc = OutgoingDocument(
        doc_type=dt,
        doc_number=doc_number,
        issue_date=payload.issue_date,
        customer_id=None,
        client_ref_id=client.id,
        customer_name=client.display_name,
        customer_address=client.address,
        customer_email=client.email,
        customer_phone=client.phone,
        currency=payload.currency,
        subtotal=subtotal,
        gst_amount=gst_amount,
        total=total,
        status=DocumentStatus.ISSUED,
        paid_date=paid_date,
        payment_method=payment_method,
        notes=payload.notes,
    )
    for i, li in enumerate(line_dicts):
        doc.lines.append(OutgoingDocumentLine(order_no=i, **li))
    db.add(doc)
    try:
        db.flush()
        if has_doc_number_override:
            doc_numbering.advance_sequence_for_override(db, doc_number)
    except IntegrityError as e:
        # A racing insert beat the pre-check to the same doc_number. Return a
        # clean message — never leak the raw SQL / schema in the error detail.
        db.rollback()
        raise HTTPException(409, f"Document number {doc_number} already exists") from e

    db.commit()
    db.refresh(doc)
    return _serialize(doc, db)


@router.patch("/{doc_id}", response_model=OutgoingOut)
def update_document(
    doc_id: PathId,
    payload: OutgoingUpdate,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc = db.get(OutgoingDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    if payload.status is not None:
        # Status is never settable via generic PATCH — transitions go through
        # the dedicated endpoints (DELETE = void, /restore, /pdf issues a draft).
        raise HTTPException(
            422,
            "Status cannot be changed via PATCH. Use the void/restore endpoints "
            "(rendering the PDF issues a draft document).",
        )
    # Void docs are not editable, and financial fields (lines drive the
    # subtotal/GST/total) are locked once the receipt leaves DRAFT — receipts are
    # created ISSUED, so their money fields are immutable; void and re-create
    # instead (audit P1-5).
    if doc.status == DocumentStatus.VOID:
        raise HTTPException(400, "Cannot edit a void document")
    if payload.lines is not None and doc.status != DocumentStatus.DRAFT:
        raise HTTPException(
            409,
            "Lines and totals are locked once a document is issued. "
            "Void it and create a replacement instead.",
        )
    # The issue date is a financial field on an issued receipt (it's the printed
    # date and drives the numbering year); lock it like the lines. Change it by
    # voiding and re-issuing, not by back-dating in place.
    if payload.issue_date is not None and doc.status != DocumentStatus.DRAFT:
        raise HTTPException(
            409,
            "The issue date is locked once a receipt is issued. "
            "Void it and create a replacement to change the date.",
        )
    # Everything except `notes` is part of the issued receipt's financial/identity
    # record — payee (client_ref_id / customer_*), currency, and payment details.
    # Lock them once the receipt leaves DRAFT, the same way lines/issue_date are:
    # an issued receipt must not be silently re-pointed to a different client or
    # relabelled to another currency under the same document number. Void and
    # re-create to change these. Locking by exclusion also covers any field added
    # to OutgoingUpdate later (locked-by-default).
    if doc.status != DocumentStatus.DRAFT:
        locked = set(payload.model_dump(exclude_unset=True)) - {
            "notes",
            "status",
            "lines",
            "issue_date",
        }
        if locked:
            raise HTTPException(
                409,
                "Only notes can be edited once a receipt is issued "
                f"(locked: {', '.join(sorted(locked))}). Void it and create a "
                "replacement to change the payee, currency, or payment details.",
            )

    data = payload.model_dump(exclude_unset=True)
    lines_in = data.pop("lines", None)
    data.pop("status", None)  # rejected above; discard an explicit null
    if "client_ref_id" in data:
        client = _client_payload_or_404(db, data["client_ref_id"])
        data.update(
            {
                "customer_id": None,
                "customer_name": client.display_name,
                "customer_address": client.address,
                "customer_email": client.email,
                "customer_phone": client.phone,
            }
        )

    for field, value in data.items():
        setattr(doc, field, value)
    if data:
        # Unlink the stale PDF, not just the reference: a bare
        # pdf_rel_path=None leaves the PII PDF on disk, and a later void
        # (which cleans up via pdf_rel_path) would silently skip it.
        _discard_doc_pdf(doc, company.id)

    if lines_in is not None:
        # Replace all lines. Line amounts are pre-GST; when the company is
        # GST-registered we add 10% on top so total = subtotal + GST.
        doc.lines.clear()
        subtotal, _line_total_unused, line_dicts = _compute_totals(
            [OutgoingLineIn(**ld) for ld in lines_in]
        )
        for i, ld in enumerate(line_dicts):
            doc.lines.append(OutgoingDocumentLine(order_no=i, **ld))
        gst_amount = (
            _money(subtotal * Decimal("0.10")) if company.gst_registered else Decimal("0.00")
        )
        doc.subtotal = subtotal
        doc.gst_amount = gst_amount
        doc.total = _money(subtotal + gst_amount)
        _discard_doc_pdf(doc, company.id)  # totals changed

    db.commit()
    db.refresh(doc)
    return _serialize(doc, db)


@router.delete("/{doc_id}", status_code=204)
def void_document(
    doc_id: PathId,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc = db.get(OutgoingDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    doc.status = DocumentStatus.VOID
    _discard_doc_pdf(doc, company.id)
    db.commit()


@router.post("/{doc_id}/restore", response_model=OutgoingOut)
def restore_document(
    doc_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc = db.get(OutgoingDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    if doc.status != DocumentStatus.VOID:
        raise HTTPException(400, "Only a void document can be restored")
    doc.status = DocumentStatus.ISSUED
    doc.pdf_rel_path = None
    db.commit()
    db.refresh(doc)
    return _serialize(doc, db)


# ---------------------------------------------------------------------------
# PDF rendering + download
# ---------------------------------------------------------------------------


def _render_for(doc: OutgoingDocument, company: Company) -> bytes:
    bank = {
        "bank_account_name": company.bank_account_name,
        "bank_name": company.bank_name,
        "bank_bsb": company.bank_bsb,
        "bank_account_number": company.bank_account_number,
        "bank_swift": company.bank_swift,
    }
    render_args = dict(
        doc_type=doc.doc_type.value if hasattr(doc.doc_type, "value") else doc.doc_type,
        doc_number=doc.doc_number,
        issue_date=doc.issue_date,
        company={
            "name": company.name,
            "address_line1": company.address_line1,
            "address_line2": company.address_line2,
            "suburb": company.suburb,
            "state": company.state,
            "postcode": company.postcode,
            "phone": company.phone,
            "email": company.email,
            "abn": company.abn,
            **bank,
        },
        customer={
            "name": doc.customer_name,
            "address": doc.customer_address,
            "abn": doc.customer_abn,
            "email": doc.customer_email,
            "phone": doc.customer_phone,
        },
        lines=[
            {
                "description": ln.description,
                "quantity": ln.quantity,
                "unit_price": ln.unit_price,
                "amount": ln.amount,
            }
            for ln in doc.lines
        ],
        subtotal=doc.subtotal,
        gst_amount=doc.gst_amount,
        total=doc.total,
        currency=doc.currency,
        paid_date=doc.paid_date,
        payment_method=doc.payment_method,
        notes=doc.notes,
        is_gst_registered=company.gst_registered,
    )

    # Prefer the HTML→PDF renderer (CSS handles layout/wrapping); fall back to
    # the reportlab renderer if Chromium isn't available so a document always
    # renders.
    try:
        return html_render.render_document_pdf(**render_args)
    except html_render.HtmlRenderUnavailable as exc:
        print(f"[render] HTML renderer unavailable, using reportlab: {exc}", flush=True)
        return pdf_render.render_document_pdf(**render_args)


@router.post("/{doc_id}/pdf")
def render_pdf(
    doc_id: PathId,
    inline: bool = Query(default=True),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc = db.get(OutgoingDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")

    pdf_bytes = _render_for(doc, company)

    # A void document's PDF must NOT be persisted: void() deletes the on-disk PDF
    # precisely because it holds client PII, and simply opening the void receipt
    # would otherwise re-render and re-write it (repopulating pdf_rel_path),
    # defeating that cleanup. Still render to the response so a void receipt stays
    # viewable, but never write it back to disk.
    #
    # Serialise the final status-check + write + commit against a concurrent void
    # (DELETE) to close the check-then-write race: end the read snapshot opened
    # by the render above, take a write lock (BEGIN IMMEDIATE) — which starts a
    # fresh snapshot that sees any already-committed void and blocks a racing void
    # until we finish — then re-read status. If it's void, release without
    # persisting; otherwise write + commit while the lock is held.
    db.rollback()  # release the render's read snapshot before locking
    doc_numbering._begin_sqlite_immediate(db)
    db.refresh(doc, attribute_names=["status"])
    if doc.status == DocumentStatus.VOID:
        db.rollback()  # release the write lock; nothing to persist
    else:
        # Persist PDF to disk (overwrite any prior render) for the historical record.
        company_dir = settings.company_dir(company.id)
        out_dir = company_dir / "outgoing" / doc.issue_date.strftime("%Y-%m")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{doc.doc_number}.pdf"
        out_path.write_bytes(pdf_bytes)

        doc.pdf_rel_path = str(out_path.relative_to(company_dir)).replace("\\", "/")
        if doc.status == DocumentStatus.DRAFT:
            doc.status = DocumentStatus.ISSUED
        db.commit()

    disposition = "inline" if inline else "attachment"
    filename = f"{safe_filename(doc.doc_number, default='document')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )
