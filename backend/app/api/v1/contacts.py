from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db.company import begin_sqlite_immediate
from ...deps import PathId, get_company_db, get_current_company
from ...hooks import iter_contact_reference_checks
from ...models.company import Contact, Invoice
from ...models.master import Company
from ...schemas.contact import ContactCreate, ContactOut, ContactUpdate


router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("", response_model=list[ContactOut])
def list_contacts(
    q: str | None = Query(default=None, description="Case-insensitive name substring"),
    kind: str | None = Query(default=None, pattern="^(customer|supplier|both)$"),
    active_only: bool = Query(
        default=False,
        description="When true, return only active contacts (use for pickers/dropdowns).",
    ),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    query = db.query(Contact)
    if q:
        query = query.filter(Contact.name.ilike(f"%{q}%"))
    if kind:
        query = query.filter(Contact.kind == kind)
    if active_only:
        query = query.filter(Contact.active.is_(True))
    return query.order_by(Contact.name.asc()).limit(200).all()


@router.post("", response_model=ContactOut, status_code=201)
def create_contact(
    payload: ContactCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    begin_sqlite_immediate(db)
    existing = (
        db.query(Contact)
        .filter(
            func.lower(Contact.name) == payload.name.lower(),
            Contact.kind == payload.kind,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Contact '{payload.name}' already exists")
    contact = Contact(**payload.model_dump())
    db.add(contact)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Contact could not be saved because it conflicts with an existing record",
        ) from None
    db.refresh(contact)
    return contact


@router.patch("/{contact_id}", response_model=ContactOut)
def update_contact(
    contact_id: PathId,
    payload: ContactUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    begin_sqlite_immediate(db)
    c = db.query(Contact).filter(Contact.id == contact_id).one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"].lower() != c.name.lower():
        clash = (
            db.query(Contact)
            .filter(
                func.lower(Contact.name) == data["name"].lower(),
                Contact.id != contact_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=409, detail=f"Contact '{data['name']}' already exists")

    for field, value in data.items():
        # Preserve the established empty-string-to-null behaviour for nullable
        # text fields while allowing an explicit JSON null to clear a value.
        if field in {"abn", "email", "phone", "address", "notes"}:
            value = value or None
        setattr(c, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Contact could not be saved because it conflicts with an existing record",
        ) from None
    db.refresh(c)
    return c


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact(
    contact_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    c = db.query(Contact).filter(Contact.id == contact_id).one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    if db.query(Invoice).filter(Invoice.contact_id == contact_id).first():
        raise HTTPException(
            status_code=409,
            detail="Contact is referenced by invoices. Remove or reassign those first.",
        )
    # Additional reference checks are registered by other modules via
    # app.hooks.register_contact_reference_check. In M1-standalone (no M2
    # loaded) this loop is a no-op; with M2 loaded, it includes the
    # OutgoingDocument check.
    for check in iter_contact_reference_checks():
        reason = check(db, contact_id)
        if reason is not None:
            raise HTTPException(status_code=409, detail=reason)
    db.delete(c)
    db.commit()
    return None


def get_or_create_contact(*, db: Session, name: str, kind: str, abn: str | None = None) -> Contact:
    """Helper used by invoice import flows. Matches by lowercased name + kind."""
    name = name.strip()
    if not name:
        raise ValueError("Contact name must not be empty")
    existing = db.query(Contact).filter(Contact.name.ilike(name)).first()
    if existing:
        # Promote to 'both' if a contact already exists in the other role
        if existing.kind != kind and existing.kind != "both":
            existing.kind = "both"
        return existing
    contact = Contact(name=name, kind=kind, abn=abn)
    db.add(contact)
    db.flush()
    return contact
