"""Document number generation for outgoing documents.

Format: <PREFIX>-<YYYY>-<NNNN>-<V>
  RCT-2026-0001-1   (receipt, first version)

Receipt is the only outgoing document type. The counter row is kept per-year in
the document_counters table; `set_counter` lets the user adjust the starting
value in Settings.
"""

from __future__ import annotations

from datetime import date
import re

from sqlalchemy.orm import Session

from ..db.company import begin_sqlite_immediate
from ..models.outgoing import DocumentCounter, DocumentType, OutgoingDocument


PREFIX = {
    DocumentType.RECEIPT: "RCT",
}


NUMBER_RE = re.compile(r"^[A-Z]+-(\d{4})-(\d{4})(?:-(\d+))?$")


def format_number(doc_type: DocumentType, year: int, n: int, variant: int | None = None) -> str:
    # Every document number carries a version suffix; the original issue is
    # always `-1` (the first version of that doc number), and each subsequent
    # void+reissue increments the suffix. Callers that omit `variant` get `-1`.
    base = f"{PREFIX[doc_type]}-{year}-{n:04d}"
    return f"{base}-{variant or 1}"


def number_from_serial(doc_type: DocumentType, serial: int, year: int) -> str:
    """Build a standard document number from a user-supplied serial.

    The user types only the running number (e.g. 222); the prefix and year are
    added automatically: RCT-2026-0222-1.
    """
    return format_number(doc_type, year, serial)


def parse_number(doc_number: str) -> tuple[int, int, int | None] | None:
    match = NUMBER_RE.match(doc_number.strip())
    if not match:
        return None
    year, serial, variant = match.groups()
    return int(year), int(serial), int(variant) if variant is not None else None


DOCUMENT_SEQUENCE_TYPES = (DocumentType.RECEIPT,)


def _counter_for(db: Session, doc_type: DocumentType, year: int) -> DocumentCounter:
    counter = db.get(DocumentCounter, (doc_type, year))
    if counter is None:
        _ensure_all_counters_exist(db, year)
        counter = db.get(DocumentCounter, (doc_type, year))
    if counter is None:
        raise RuntimeError(f"Missing document counter for {doc_type.value}/{year}")
    return counter


# Shared helper now lives in db.company (trust.py needs the same pattern).
# Kept under the old private name because api/v1/outgoing.py calls
# doc_numbering._begin_sqlite_immediate(db) directly.
_begin_sqlite_immediate = begin_sqlite_immediate


def _ensure_all_counters_exist(db: Session, year: int) -> None:
    """Create any missing shared counter rows for a year."""
    rows = (
        db.query(DocumentCounter.doc_type)
        .filter(
            DocumentCounter.year == year,
            DocumentCounter.doc_type.in_(DOCUMENT_SEQUENCE_TYPES),
        )
        .all()
    )
    existing = {DocumentType(doc_type) for (doc_type,) in rows}
    missing = [doc_type for doc_type in DOCUMENT_SEQUENCE_TYPES if doc_type not in existing]
    if not missing:
        return

    for doc_type in missing:
        db.add(DocumentCounter(doc_type=doc_type, year=year, last_number=0))
    db.flush()


def _lock_counters_for_year(db: Session, year: int) -> None:
    """Lock all shared counter rows for a year until the transaction commits."""
    (
        db.query(DocumentCounter)
        .filter(
            DocumentCounter.year == year,
            DocumentCounter.doc_type.in_(DOCUMENT_SEQUENCE_TYPES),
        )
        .order_by(DocumentCounter.doc_type.asc())
        .with_for_update()
        .all()
    )


def _prepare_counter_allocation(db: Session, year: int) -> None:
    _begin_sqlite_immediate(db)
    _ensure_all_counters_exist(db, year)
    _lock_counters_for_year(db, year)


def _bump_sequence_to_at_least(db: Session, year: int, serial: int) -> None:
    for doc_type in DOCUMENT_SEQUENCE_TYPES:
        counter = _counter_for(db, doc_type, year)
        if counter.last_number < serial:
            counter.last_number = serial
    db.flush()


def unified_last_number(db: Session, year: int) -> int:
    _ensure_all_counters_exist(db, year)
    return max(
        (_counter_for(db, doc_type, year).last_number for doc_type in DOCUMENT_SEQUENCE_TYPES),
        default=0,
    )


def _doc_number_exists(db: Session, doc_type: DocumentType, doc_number: str) -> bool:
    return (
        db.query(OutgoingDocument.id)
        .filter(
            OutgoingDocument.doc_type == doc_type,
            OutgoingDocument.doc_number == doc_number,
        )
        .first()
        is not None
    )


def peek_next(db: Session, doc_type: DocumentType, year: int) -> int:
    """Return what the next shared document serial would be, without incrementing."""
    return unified_last_number(db, year) + 1


def next_number(db: Session, doc_type: DocumentType, *, issue_date: date | None = None) -> str:
    """Atomically allocate the next document number for (doc_type, year).

    Caller is responsible for the surrounding transaction (we don't commit).
    """
    year = (issue_date or date.today()).year
    _prepare_counter_allocation(db, year)
    serial = unified_last_number(db, year) + 1
    candidate = format_number(doc_type, year, serial)
    _bump_sequence_to_at_least(db, year, serial)
    if _doc_number_exists(db, doc_type, candidate):
        raise RuntimeError(f"Document number already exists: {candidate}")
    return candidate


def advance_sequence_for_override(db: Session, doc_number: str) -> None:
    """Advance the shared sequence when a manual override uses the standard format."""
    parsed = parse_number(doc_number)
    if parsed is None:
        return
    year, serial, _ = parsed
    _prepare_counter_allocation(db, year)
    _bump_sequence_to_at_least(db, year, serial)


def _max_issued_serial(db: Session, year: int) -> int:
    """Highest standard-format serial already issued for a year (0 if none)."""
    best = 0
    for (num,) in db.query(OutgoingDocument.doc_number).all():
        parsed = parse_number(num)
        if parsed is None:
            continue
        parsed_year, serial, _suffix = parsed
        if parsed_year == year and serial > best:
            best = serial
    return best


def set_counter(db: Session, doc_type: DocumentType, year: int, last_number: int) -> None:
    """Override the shared document sequence for a year.

    `doc_type` is accepted for backwards compatibility with the existing API;
    all document counters are set together so future allocations stay aligned.

    Takes the write lock first (the one numbering-state writer that didn't),
    and refuses to rewind below the highest issued serial — a rewound counter
    made every subsequent auto-numbered create collide and fail until the
    operator raised it again.
    """
    if last_number < 0:
        raise ValueError("last_number must be >= 0")
    _prepare_counter_allocation(db, year)
    issued = _max_issued_serial(db, year)
    if last_number < issued:
        raise ValueError(
            f"last_number {last_number} is below the highest issued document "
            f"serial ({issued}) for {year}; the next automatic number would "
            f"collide with an existing document. Use {issued} or higher."
        )
    for sequence_type in DOCUMENT_SEQUENCE_TYPES:
        counter = _counter_for(db, sequence_type, year)
        counter.last_number = last_number
    db.flush()
