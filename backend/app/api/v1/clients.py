"""Clients router.

Clients are people/entities we provide migration services to. Distinct from
`contacts` (which represents providers / suppliers we pay).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db.company import begin_sqlite_immediate
from ...deps import PathId, get_company_db, get_current_company
from ...models.company import Client
from ...models.master import Company
from ...schemas.parties import (
    ClientCreate,
    ClientOut,
    ClientUpdate,
)


router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("", response_model=list[ClientOut])
def list_clients(
    q: str | None = Query(default=None, description="Case-insensitive name substring"),
    active_only: bool = Query(default=True),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    query = db.query(Client)
    if q:
        query = query.filter(Client.display_name.ilike(f"%{q}%"))
    if active_only:
        query = query.filter(Client.is_active.is_(True))
    return query.order_by(Client.display_name.asc()).limit(500).all()


@router.post("", response_model=ClientOut, status_code=201)
def create_client(
    payload: ClientCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    begin_sqlite_immediate(db)
    existing = (
        db.query(Client).filter(func.lower(Client.display_name) == payload.display_name.lower()).first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Client '{payload.display_name}' already exists")
    ref = payload.client_ref
    if ref:
        dup_ref = db.query(Client).filter(Client.client_ref == ref).first()
        if dup_ref:
            raise HTTPException(
                status_code=409,
                detail=f"Internal ref '{ref}' is already used by another client",
            )
    client = Client(**payload.model_dump())
    db.add(client)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Client could not be saved because it conflicts with an existing record",
        ) from None
    db.refresh(client)
    return client


@router.get("/{client_id}", response_model=ClientOut)
def get_client(
    client_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


@router.patch("/{client_id}", response_model=ClientOut)
def update_client(
    client_id: PathId,
    payload: ClientUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    begin_sqlite_immediate(db)
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    data = payload.model_dump(exclude_unset=True)
    if "display_name" in data:
        dup_name = (
            db.query(Client)
            .filter(
                func.lower(Client.display_name) == data["display_name"].lower(),
                Client.id != client_id,
            )
            .first()
        )
        if dup_name:
            raise HTTPException(
                status_code=409,
                detail=f"Client '{data['display_name']}' already exists",
            )
    if "client_ref" in data:
        ref = data["client_ref"]
        if ref:
            dup_ref = db.query(Client).filter(Client.client_ref == ref, Client.id != client_id).first()
            if dup_ref:
                raise HTTPException(
                    status_code=409,
                    detail=f"Internal ref '{ref}' is already used by another client",
                )
    for field, value in data.items():
        setattr(client, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Client could not be saved because it conflicts with an existing record",
        ) from None
    db.refresh(client)
    return client


# Legacy migrate-from-contacts endpoint removed alongside the legacy
# ServiceAgreement table. The new Documents UI creates Clients directly
# and the legacy backfill is no longer reachable.
