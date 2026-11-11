from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...deps import PathId, get_company_db, get_current_company
from ...models.company import Invoice
from ...models.master import Company
from ...schemas._limits import SQLITE_INT_MAX
from ...schemas.credit_note import (
    CreditNoteApplicationCreate,
    CreditNoteApplicationOut,
    CreditNoteApplicationReverse,
    CreditNoteCreate,
    CreditNoteOut,
    CreditNoteRefundCreate,
    CreditNoteRefundOut,
    CreditNoteRefundReverse,
    CreditNoteSourceOut,
    CreditNoteUpdate,
    CreditNoteVoid,
)
from ...services import credit_notes as credit_note_service
from ...services import doc_numbering
from ...services.journal import JournalError
from ...services.period_lock import AccountingPeriodLockedError, require_open_date


router = APIRouter(prefix="/credit-notes", tags=["credit-notes"])
application_router = APIRouter(prefix="/credit-note-applications", tags=["credit-notes"])


def _raise_domain_error(exc: credit_note_service.CreditNoteError) -> None:
    raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc


def _raise_integrity_conflict(db: Session, exc: IntegrityError) -> None:
    db.rollback()
    raise HTTPException(
        status_code=409,
        detail="Credit note conflicts with existing data.",
    ) from exc


def _with_source_contact_snapshot(db: Session, output: dict) -> dict:
    invoice = db.get(Invoice, output["source_invoice_id"])
    if invoice is not None and invoice.contact_name_snapshot is not None:
        output["contact_name"] = invoice.contact_name_snapshot
    return output


@router.get("", response_model=list[CreditNoteOut])
def list_credit_notes(
    direction: Literal["AR", "AP"] | None = None,
    source_invoice_id: int | None = Query(default=None, ge=1, le=SQLITE_INT_MAX),
    status_filter: Literal["draft"] | None = Query(default=None, alias="status"),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return [
        _with_source_contact_snapshot(
            db,
            credit_note_service.credit_note_output(note),
        )
        for note in credit_note_service.list_credit_notes(
            db,
            direction=direction,
            source_invoice_id=source_invoice_id,
        )
        if status_filter is None or note.status == status_filter
    ]


@router.get("/source-invoices/{invoice_id}", response_model=CreditNoteSourceOut)
def get_source_invoice(
    invoice_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        output = credit_note_service.source_snapshot(db, invoice_id)
        return _with_source_contact_snapshot(db, output)
    except credit_note_service.CreditNoteError as exc:
        _raise_domain_error(exc)


@router.get("/{credit_note_id}", response_model=CreditNoteOut)
def get_credit_note(
    credit_note_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        output = credit_note_service.credit_note_output(
            credit_note_service.get_credit_note(db, credit_note_id)
        )
        return _with_source_contact_snapshot(db, output)
    except credit_note_service.CreditNoteError as exc:
        _raise_domain_error(exc)


@router.post("", response_model=CreditNoteOut, status_code=status.HTTP_201_CREATED)
def create_credit_note(
    payload: CreditNoteCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        note = credit_note_service.create_credit_note(db, payload)
        output = credit_note_service.credit_note_output(note)
        db.commit()
        return _with_source_contact_snapshot(db, output)
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.patch("/{credit_note_id}", response_model=CreditNoteOut)
def update_credit_note(
    credit_note_id: PathId,
    payload: CreditNoteUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        note = credit_note_service.update_credit_note(db, credit_note_id, payload)
        output = credit_note_service.credit_note_output(note)
        db.commit()
        return _with_source_contact_snapshot(db, output)
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.delete("/{credit_note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credit_note(
    credit_note_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        credit_note_service.delete_credit_note(db, credit_note_id)
        db.commit()
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.post("/{credit_note_id}/post", response_model=CreditNoteOut)
def post_credit_note(
    credit_note_id: PathId,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        note = credit_note_service.get_credit_note(db, credit_note_id)
        require_open_date(company, note.issue_date, operation="post a credit note")
        credit_note_service.post_credit_note(
            db,
            credit_note_id,
            gst_registered=company.gst_registered,
        )
        output = credit_note_service.credit_note_output(
            credit_note_service.get_credit_note(db, credit_note_id)
        )
        db.commit()
        return _with_source_contact_snapshot(db, output)
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except AccountingPeriodLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except JournalError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.post("/{credit_note_id}/void", response_model=CreditNoteOut)
def void_credit_note(
    credit_note_id: PathId,
    payload: CreditNoteVoid,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        credit_note_service.void_credit_note(
            db,
            credit_note_id,
            payload,
            company=company,
        )
        note = credit_note_service.get_credit_note(db, credit_note_id)
        db.commit()
        return _with_source_contact_snapshot(db, credit_note_service.credit_note_output(note))
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except AccountingPeriodLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.post(
    "/{credit_note_id}/applications",
    response_model=CreditNoteApplicationOut,
    status_code=status.HTTP_201_CREATED,
)
def apply_credit_note(
    credit_note_id: PathId,
    payload: CreditNoteApplicationCreate,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        application = credit_note_service.apply_credit_note(
            db,
            credit_note_id,
            payload,
            company=company,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return credit_note_service._credit_note_application_output(application)
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except AccountingPeriodLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.post(
    "/{credit_note_id}/refunds",
    response_model=CreditNoteRefundOut,
    status_code=status.HTTP_201_CREATED,
)
def create_credit_note_refund(
    credit_note_id: PathId,
    payload: CreditNoteRefundCreate,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        refund = credit_note_service.create_credit_note_refund(
            db,
            credit_note_id,
            payload,
            company=company,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return credit_note_service.credit_note_refund_output(refund)
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except AccountingPeriodLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@router.post(
    "/{credit_note_id}/refunds/{credit_note_refund_id}/reverse",
    response_model=CreditNoteRefundOut,
)
def reverse_credit_note_refund(
    credit_note_id: PathId,
    credit_note_refund_id: PathId,
    payload: CreditNoteRefundReverse,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    doc_numbering._begin_sqlite_immediate(db)
    try:
        refund = credit_note_service.reverse_credit_note_refund(
            db,
            credit_note_id,
            credit_note_refund_id,
            payload,
            company=company,
        )
        db.commit()
        return credit_note_service.credit_note_refund_output(refund)
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except AccountingPeriodLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)


@application_router.post(
    "/{credit_note_application_id}/reverse",
    response_model=CreditNoteApplicationOut,
)
def reverse_credit_note_application(
    credit_note_application_id: PathId,
    payload: CreditNoteApplicationReverse,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        application = credit_note_service.reverse_credit_note_application(
            db,
            credit_note_application_id,
            payload,
            company=company,
        )
        db.commit()
        return credit_note_service._credit_note_application_output(application)
    except credit_note_service.CreditNoteError as exc:
        db.rollback()
        _raise_domain_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(db, exc)