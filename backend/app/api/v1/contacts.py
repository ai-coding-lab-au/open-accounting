from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

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
    existing = (
        db.query(Contact)
        .filter(Contact.name.ilike(payload.name), Contact.kind == payload.kind)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Contact '{payload.name}' already exists")
    contact = Contact(**payload.model_dump())
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


@router.patch("/{contact_id}", response_model=ContactOut)
def update_contact(
    contact_id: PathId,
    payload: ContactUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    c = db.query(Contact).filter(Contact.id == contact_id).one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    if payload.name is not None and payload.name.strip().lower() != c.name.lower():
        clash = (
            db.query(Contact)
            .filter(Contact.name.ilike(payload.name), Contact.id != contact_id)
            .first()
        )
        if clash:
            raise HTTPException(
                status_code=409, detail=f"Contact '{payload.name}' already exists"
            )
        c.name = payload.name
    if payload.kind is not None:
        c.kind = payload.kind
    if payload.abn is not None:
        c.abn = payload.abn or None
    if payload.email is not None:
        c.email = payload.email or None
    if payload.phone is not None:
        c.phone = payload.phone or None
    if payload.address is not None:
        c.address = payload.address or None
    if payload.notes is not None:
        c.notes = payload.notes or None
    if payload.active is not None:
        c.active = payload.active

    db.commit()
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
    existing = (
        db.query(Contact)
        .filter(Contact.name.ilike(name))
        .first()
    )
    if existing:
        # Promote to 'both' if a contact already exists in the other role
        if existing.kind != kind and existing.kind != "both":
            existing.kind = "both"
        return existing
    contact = Contact(name=name, kind=kind, abn=abn)
    db.add(contact)
    db.flush()
    return contact
