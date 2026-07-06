from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...deps import PathId, get_company_db, get_current_company
from ...models.company import JournalEntrySource
from ...models.master import Company
from ...schemas.journal import (
    JournalEntryCreate,
    JournalEntryOut,
    JournalEntryUpdate,
)
from ...services import journal as journal_service
router = APIRouter(prefix="/journal", tags=["journal"])


def _list_entries_impl(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Case-insensitive substring match on memo / reference.",
    ),
    date_from: date | None = Query(
        default=None,
        alias="from",
        description="Earliest entry_date (inclusive, YYYY-MM-DD).",
    ),
    date_to: date | None = Query(
        default=None,
        alias="to",
        description="Latest entry_date (inclusive, YYYY-MM-DD).",
    ),
    source_type: JournalEntrySource | None = Query(default=None),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return journal_service.list_entries(
        db,
        limit=limit,
        offset=offset,
        q=q,
        date_from=date_from,
        date_to=date_to,
        source_type=source_type.value if source_type is not None else None,
    )


router.add_api_route("", _list_entries_impl, methods=["GET"], response_model=list[JournalEntryOut])
router.add_api_route("/entries", _list_entries_impl, methods=["GET"], response_model=list[JournalEntryOut])


@router.post("", response_model=JournalEntryOut, status_code=201)
def create_entry(
    payload: JournalEntryCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return journal_service.create_entry(db, payload)
    except journal_service.JournalError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e)) from e


@router.get("/{entry_id}", response_model=JournalEntryOut)
def get_entry(
    entry_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    entry = journal_service.get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    return entry


@router.patch("/{entry_id}", response_model=JournalEntryOut)
def update_entry(
    entry_id: PathId,
    payload: JournalEntryUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        entry = journal_service.update_entry(db, entry_id, payload)
    except journal_service.JournalError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e)) from e
    if entry is None:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    return entry


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    entry_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    ok = journal_service.delete_entry(db, entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    return None
