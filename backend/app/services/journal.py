"""Manual journal entry service (M2.1).

Scope (C-route per ROADMAP): the journal is *additive* to Invoice/Outgoing/
Bank/Trust, not a mirror of them. Use it for the things those modules don't
cover — opening balances, period-end adjustments, depreciation, bad debt,
manual corrections.

Invariants enforced here:
  I1. Every entry has >= 2 lines.
  I2. Each line has either debit > 0 OR credit > 0, never both. (DB CHECK
      also enforces this, but we validate first to give a clean error.)
  I3. sum(debit) == sum(credit) across all lines of an entry.
  I4. Every referenced account_id exists and is active.

Editability: by user choice (small one-person firm, no audit requirement)
entries remain fully editable after creation. If that changes later, add
a `locked` flag and gate update/delete on it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models.company import Account, JournalEntry, JournalEntrySource, JournalLine
from ..schemas.journal import JournalEntryCreate, JournalEntryUpdate, JournalLineCreate


class JournalError(Exception):
    """Base for journal business-rule violations. Router maps to HTTP 400."""

    http_status: int = 400


class UnbalancedEntry(JournalError):
    pass


class InvalidLine(JournalError):
    pass


class UnknownAccount(JournalError):
    pass


class JournalLocked(JournalError):
    http_status = 409


def _validate_lines(session: Session, lines: list[JournalLineCreate]) -> None:
    """Enforce I1-I4. Raises JournalError on the first violation."""
    if len(lines) < 2:
        raise InvalidLine("A journal entry needs at least two lines.")

    total_debit = Decimal("0")
    total_credit = Decimal("0")
    account_ids: set[int] = set()
    debit_accounts: set[int] = set()
    credit_accounts: set[int] = set()

    for idx, line in enumerate(lines, start=1):
        d = line.debit_amount or Decimal("0")
        c = line.credit_amount or Decimal("0")
        if (d > 0 and c > 0) or (d == 0 and c == 0):
            raise InvalidLine(
                f"Line {idx}: each line must have exactly one of debit_amount "
                f"or credit_amount > 0 (got debit={d}, credit={c})."
            )
        total_debit += d
        total_credit += c
        account_ids.add(line.account_id)
        if d > 0:
            debit_accounts.add(line.account_id)
        else:
            credit_accounts.add(line.account_id)

    # The same account on both a debit and a credit line nets to zero on that
    # account — almost always a data-entry mistake, so reject it.
    both_sides = debit_accounts & credit_accounts
    if both_sides:
        raise InvalidLine(
            f"Account id(s) {sorted(both_sides)} appear on both a debit and a "
            f"credit line; an account cannot be debited and credited in the "
            f"same entry."
        )

    if total_debit != total_credit:
        raise UnbalancedEntry(
            f"Entry is unbalanced: total debit {total_debit} != total credit {total_credit}."
        )

    # Verify every referenced account exists + is active. One query, not N.
    found = {
        a.id
        for a in session.query(Account.id, Account.active)
        .filter(Account.id.in_(account_ids))
        .all()
        if a.active
    }
    missing = account_ids - found
    if missing:
        raise UnknownAccount(
            f"Account id(s) not found or inactive: {sorted(missing)}."
        )


def create_entry(session: Session, payload: JournalEntryCreate) -> JournalEntry:
    _validate_lines(session, payload.lines)

    entry = JournalEntry(
        entry_date=payload.entry_date,
        memo=payload.memo,
        reference=payload.reference,
    )
    for line in payload.lines:
        entry.lines.append(
            JournalLine(
                account_id=line.account_id,
                debit_amount=line.debit_amount or Decimal("0"),
                credit_amount=line.credit_amount or Decimal("0"),
                description=line.description,
            )
        )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def get_entry(session: Session, entry_id: int) -> JournalEntry | None:
    return session.get(JournalEntry, entry_id)


def list_entries(
    session: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_type: str | None = None,
) -> list[JournalEntry]:
    query = session.query(JournalEntry)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (JournalEntry.memo.ilike(like)) | (JournalEntry.reference.ilike(like))
        )
    if date_from is not None:
        query = query.filter(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        query = query.filter(JournalEntry.entry_date <= date_to)
    if source_type is not None:
        query = query.filter(JournalEntry.source_type == source_type)
    return (
        query.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def update_entry(
    session: Session, entry_id: int, payload: JournalEntryUpdate
) -> JournalEntry | None:
    entry = session.get(JournalEntry, entry_id)
    if entry is None:
        return None
    if entry.source_type != JournalEntrySource.MANUAL and entry.source_type != JournalEntrySource.MANUAL.value:
        raise JournalLocked(f"Journal entry {entry_id} is locked because it was generated from {entry.source_type}.")

    if payload.entry_date is not None:
        entry.entry_date = payload.entry_date
    if payload.memo is not None:
        entry.memo = payload.memo
    if payload.reference is not None:
        entry.reference = payload.reference

    if payload.lines is not None:
        _validate_lines(session, payload.lines)
        # Replace all lines. cascade="all, delete-orphan" cleans the old ones.
        entry.lines.clear()
        session.flush()
        for line in payload.lines:
            entry.lines.append(
                JournalLine(
                    account_id=line.account_id,
                    debit_amount=line.debit_amount or Decimal("0"),
                    credit_amount=line.credit_amount or Decimal("0"),
                    description=line.description,
                )
            )

    session.commit()
    session.refresh(entry)
    return entry


def delete_entry(session: Session, entry_id: int) -> bool:
    entry = session.get(JournalEntry, entry_id)
    if entry is None:
        return False
    if entry.source_type != JournalEntrySource.MANUAL and entry.source_type != JournalEntrySource.MANUAL.value:
        raise JournalLocked(f"Journal entry {entry_id} is locked because it was generated from {entry.source_type}.")
    session.delete(entry)
    session.commit()
    return True
