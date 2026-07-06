"""Bank statement import service (M3).

Two-step pipeline:

  1. preview(file, bank_account_id) → returns parsed rows with proposed
     column mapping, dedup status (NEW / DUPLICATE), and rule-match
     suggestions. NO writes.
  2. commit(bank_account_id, rows_payload) → writes only the rows the
     caller explicitly accepted, applies their (possibly overridden)
     account_id / tax_code, sets dedup_key so re-imports don't double up.

Supported formats: .csv and .xlsx (the same parser core that powers the
invoice spreadsheet import).

Bank statement column heuristics — typical AU bank exports have one of:
  - separate "Debit" / "Credit" columns (most common — ANZ, NAB, CBA)
  - signed "Amount" column where positive = credit (Westpac, some online)
We support both. The user can override the mapping in the preview UI.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ..models.company import (
    Account,
    AccountType,
    BankAccount,
    BankRule,
    BankTransaction,
    BankTxnDirection,
    TaxCode,
)
from .excel_import import (
    _cell_to_str,
    _normalise,
    _parse_date,
    _parse_decimal,
    _read_csv,
    _read_xlsx,
)
from .bank_accounts import (
    InvoicePaymentWouldDoubleCount,
    reject_expense_category_for_matching_ap_payment,
    reject_income_category_for_matching_ar_payment,
)
from ..schemas.bank import check_txn_date


# ---------------------------------------------------------------------------
# Header heuristics
# ---------------------------------------------------------------------------


BANK_FIELDS = [
    "occurred_at",   # required
    "memo",          # narrative / description / details
    "counter_party_name",
    "amount",        # signed; positive = IN
    "debit",         # unsigned OUT
    "credit",        # unsigned IN
]


_HINTS: dict[str, list[str]] = {
    "occurred_at": ["date", "transaction date", "posted", "value date", "日期"],
    "memo": ["description", "narrative", "details", "memo", "particulars", "摘要"],
    "counter_party_name": [
        "payee",
        "payer",
        "merchant",
        "counterparty",
        "counter party",
        "counter-party",
        "counter party name",
        "counter-party name",
        "counter_party",
        "counter_party_name",
        "对方",
    ],
    "amount": ["amount", "value", "金额"],
    "debit": ["debit", "withdrawal", "out", "支出", "借方"],
    "credit": ["credit", "deposit", "in", "收入", "贷方"],
}


_SUBSTRING_FIELD_ORDER = [
    "occurred_at",
    "memo",
    "counter_party_name",
    "debit",
    "credit",
    "amount",
]

# Bare 2-3 letter ASCII hints ("in", "out") must match a whole word in the
# header, never a substring: with debit/credit resolved before amount, a raw
# substring "in" ⊂ "incl"/"spending" would let credit steal a signed
# "Amount (incl GST)" or unsigned "Spending Amount" column and flip every
# row's direction. Longer hints and CJK hints keep plain substring matching.
_SHORT_ASCII_HINT_LEN = 3


def _hint_matches(hint: str, header: str) -> bool:
    if len(hint) > _SHORT_ASCII_HINT_LEN or not hint.isascii():
        return hint in header
    return hint in re.split(r"[^a-z0-9]+", header)


def propose_mapping(headers: list[str]) -> dict[str, int | None]:
    norm = [_normalise(h) for h in headers]
    mapping: dict[str, int | None] = {f: None for f in BANK_FIELDS}
    used: set[int] = set()
    # Pass 1: exact
    for field, hints in _HINTS.items():
        for i, h in enumerate(norm):
            if i in used or not h:
                continue
            if any(hint == h for hint in hints):
                mapping[field] = i
                used.add(i)
                break
    # Pass 2: substring
    for field in _SUBSTRING_FIELD_ORDER:
        hints = _HINTS[field]
        if mapping[field] is not None:
            continue
        for i, h in enumerate(norm):
            if i in used or not h:
                continue
            if any(_hint_matches(hint, h) for hint in hints):
                mapping[field] = i
                used.add(i)
                break
    return mapping


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _looks_like_transaction_row(cells: list[Any]) -> bool:
    """A row is data (not a header) when its first cell is a date and another
    cell is a money amount — used to detect header-less CSV exports."""
    if not cells:
        return False
    strs = [_cell_to_str(c) for c in cells]
    return _parse_date(strs[0]) is not None and any(
        _parse_decimal(c) is not None for c in strs[1:]
    )


def _synthesize_headers(row: list[Any]) -> list[str]:
    """Name the columns of a header-less CSV by cell shape. CommBank's NetBank
    export is Date, Amount(signed), Description, Balance with no header row."""
    headers: list[str] = []
    money_seen = 0
    for i, cell in enumerate(row):
        s = _cell_to_str(cell)
        if _parse_date(s) is not None and "Date" not in headers:
            headers.append("Date")
        elif _parse_decimal(s) is not None:
            headers.append("Amount" if money_seen == 0 else "Balance")
            money_seen += 1
        elif "Description" not in headers:
            headers.append("Description")
        else:
            headers.append(f"col{i}")
    return headers


def parse_statement(
    *, content: bytes, filename: str, bank_format: str | None = None
) -> dict[str, Any]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in {"xlsx", "xlsm"}:
        headers, data_rows = _read_xlsx(content)
    elif ext == "csv":
        headers, data_rows = _read_csv(content)
        # Header-less export (e.g. CommBank NetBank CSV): the "header" row read
        # above is actually the first transaction — put it back and name the
        # columns by shape so the mapping/preview works.
        if _looks_like_transaction_row(headers):
            data_rows = [headers, *data_rows]
            headers = _synthesize_headers(headers)
    elif ext == "pdf":
        # Deterministic PDF extraction (pdfplumber/pypdf) → per-bank parser →
        # the same (headers, data_rows) shape as CSV/XLSX. `bank_format` picks a
        # bank; None auto-detects. No AI (the old Ollama pdf_extract stays gone).
        from .bank_pdf import parse_pdf

        headers, data_rows = parse_pdf(content, bank_format)
    else:
        raise ValueError(f"Unsupported format: .{ext} (use .csv, .xlsx or .pdf)")

    mapping = propose_mapping(headers)
    rows_out: list[dict[str, Any]] = []
    for i, raw in enumerate(data_rows, start=2):
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in raw):
            continue
        rows_out.append({
            "row_no": i,
            "cells": [_cell_to_str(c) for c in raw],
            "raw": list(raw),
        })

    return {
        "headers": headers,
        "mapping": mapping,
        "rows": rows_out,
        "field_options": BANK_FIELDS,
    }


def _row_to_txn_shape(
    raw: list[Any], mapping: dict[str, int | None]
) -> dict[str, Any]:
    """Materialise one raw row into a tentative BankTransaction shape.

    Direction logic:
      - If both debit and credit columns are mapped, use whichever is non-zero.
      - Else if a signed amount column is mapped, positive = IN.

    `amount` in the returned dict is always positive (the BankTransaction
    convention); `direction` carries the sign.
    """
    def cell(field: str) -> Any:
        idx = mapping.get(field)
        if idx is None or idx >= len(raw):
            return None
        return raw[idx]

    occurred_at = _parse_date(cell("occurred_at"))
    memo = _cell_to_str(cell("memo")) or None
    counter = _cell_to_str(cell("counter_party_name")) or None

    debit_raw = cell("debit")
    credit_raw = cell("credit")
    amount_raw = cell("amount")

    direction: str | None = None
    amount: Decimal | None = None

    debit = _parse_decimal(debit_raw) if debit_raw not in (None, "") else None
    credit = _parse_decimal(credit_raw) if credit_raw not in (None, "") else None
    signed = _parse_decimal(amount_raw) if amount_raw not in (None, "") else None

    # A single row with BOTH a debit and a credit is ambiguous — we can't tell
    # the direction, so flag it rather than silently pick one and drop the other.
    ambiguous = bool(debit and Decimal(debit) > 0 and credit and Decimal(credit) > 0)

    if debit and Decimal(debit) > 0:
        direction = "out"
        amount = Decimal(debit)
    elif credit and Decimal(credit) > 0:
        direction = "in"
        amount = Decimal(credit)
    elif signed:
        d = Decimal(signed)
        if d > 0:
            direction = "in"
            amount = d
        elif d < 0:
            direction = "out"
            amount = -d
        # zero → skip; falls through

    return {
        "occurred_at": occurred_at,
        "memo": memo,
        "counter_party_name": counter,
        "direction": direction,
        "amount": str(amount) if amount is not None else None,
        "ambiguous": ambiguous,
    }


def _row_has_zero_amount(raw: list[Any], mapping: dict[str, int | None]) -> bool:
    values: list[Decimal] = []

    for field in ("debit", "credit", "amount"):
        idx = mapping.get(field)
        if idx is None or idx >= len(raw):
            continue
        raw_value = raw[idx]
        if raw_value in (None, ""):
            continue
        parsed = _parse_decimal(raw_value)
        if parsed is not None:
            values.append(Decimal(parsed))

    return bool(values) and all(v == 0 for v in values)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def compute_dedup_key(
    *,
    bank_account_id: int,
    direction: str,
    amount: Decimal | str,
    occurred_at: date | str,
    memo: str | None,
) -> str:
    """Stable SHA-256 hash. Same logical txn → same hash, regardless of
    whether import is via CSV or XLSX or a re-import of the same statement.

    Memo is normalised (trim + lowercase + collapse whitespace) so trivial
    formatting changes don't break dedup.
    """
    amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    occ = occurred_at if isinstance(occurred_at, str) else occurred_at.isoformat()
    norm_memo = " ".join((memo or "").lower().split())
    payload = f"{bank_account_id}|{direction}|{amt}|{occ}|{norm_memo}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def existing_dedup_keys(
    db: Session, *, bank_account_id: int, keys: Iterable[str]
) -> set[str]:
    keys = list(keys)
    if not keys:
        return set()
    rows = (
        db.query(BankTransaction.dedup_key)
        .filter(
            BankTransaction.bank_account_id == bank_account_id,
            BankTransaction.dedup_key.in_(keys),
        )
        .all()
    )
    return {r[0] for r in rows if r[0]}


def _norm_text(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def existing_txn_fingerprints(
    db: Session, *, bank_account_id: int
) -> dict[tuple[str, str, str], list[set[str]]]:
    """Index of every EXISTING transaction on the account, keyed by
    (direction, amount, date), each mapping to a list of normalised
    memo/counter-party token sets.

    Used to flag a preview row as a duplicate of a transaction already in the
    account — including manually-entered rows, which carry no dedup_key and so
    are invisible to the dedup_key path. This is what stops re-importing a
    statement that overlaps rows the user typed by hand (or a prior import under
    a different memo) from silently double-counting money.
    """
    rows = (
        db.query(
            BankTransaction.direction,
            BankTransaction.amount,
            BankTransaction.occurred_at,
            BankTransaction.memo,
            BankTransaction.counter_party_name,
        )
        .filter(BankTransaction.bank_account_id == bank_account_id)
        .all()
    )
    index: dict[tuple[str, str, str], list[set[str]]] = {}
    for direction, amount, occurred_at, memo, counter_party in rows:
        dir_v = direction.value if hasattr(direction, "value") else str(direction)
        key = (
            dir_v,
            str(Decimal(amount).quantize(Decimal("0.01"))),
            occurred_at.isoformat(),
        )
        tokens = {t for t in (_norm_text(memo), _norm_text(counter_party)) if t}
        index.setdefault(key, []).append(tokens)
    return index


def fingerprint_is_duplicate(
    index: dict[tuple[str, str, str], list[set[str]]],
    *,
    direction: str,
    amount: Decimal | str,
    occurred_at: date | str,
    memo: str | None,
    counter_party: str | None,
) -> bool:
    """A preview row duplicates an existing txn when it matches on
    (direction, amount, date) AND their memo/counter-party overlap — the
    amount+date+direction match tightened by text so two genuinely different
    same-amount, same-day movements aren't falsely flagged. Rows with no text on
    either side fall back to the amount+date+direction match alone."""
    key = (
        direction,
        str(Decimal(str(amount)).quantize(Decimal("0.01"))),
        occurred_at if isinstance(occurred_at, str) else occurred_at.isoformat(),
    )
    candidates = index.get(key)
    if not candidates:
        return False
    cand_tokens = {t for t in (_norm_text(memo), _norm_text(counter_party)) if t}
    for existing_tokens in candidates:
        if not cand_tokens and not existing_tokens:
            return True
        if cand_tokens & existing_tokens:
            return True
    return False


# ---------------------------------------------------------------------------
# Rule matching + deterministic fallback suggestions
# ---------------------------------------------------------------------------


_GST_BEARING_CODES = {TaxCode.STANDARD, TaxCode.CAPITAL}

# Fast, deterministic fallback hints for common AU SMB bank memos. The value is
# (default CoA account code, tax code). Rules still win when present.
_MEMO_HEURISTICS: dict[str, tuple[str, str]] = {
    "invoice payment": ("4000", "standard"),
    "consulting": ("4000", "standard"),
    "service fee": ("4000", "standard"),
    "stripe payout": ("4000", "standard"),
    "square payout": ("4000", "standard"),
    "card settlement": ("4000", "standard"),
    "retainer": ("4000", "standard"),
    "customer": ("4000", "standard"),
    "savings interest": ("4100", "input_taxed"),
    "interest": ("4100", "none"),
    "owner": ("3000", "none"),
    "quarterly sweep": ("1010", "none"),
    "opening top-up": ("1010", "none"),
    "internal transfer": ("1010", "none"),
    "rent": ("6100", "gst_free"),
    "lease": ("6100", "gst_free"),
    "electricity": ("6110", "standard"),
    "energy": ("6110", "standard"),
    "water": ("6110", "standard"),
    "telco": ("6200", "standard"),
    "telstra": ("6200", "standard"),
    "optus": ("6200", "standard"),
    "vodafone": ("6200", "standard"),
    "internet": ("6200", "standard"),
    "travel to client": ("6310", "gst_free"),
    "uber": ("6310", "standard"),
    "qantas": ("6310", "standard"),
    "virgin": ("6310", "standard"),
    "hotel": ("6310", "standard"),
    "officeworks": ("6400", "standard"),
    "office supplies": ("6400", "standard"),
    "stationery": ("6400", "standard"),
    "fresh": ("6400", "gst_free"),
    "coffee": ("6400", "gst_free"),
    "client": ("4000", "standard"),
    "aws invoice": ("6410", "none"),
    "aws": ("6410", "standard"),
    "amazon web services": ("6410", "standard"),
    "subscription": ("6410", "standard"),
    "adobe": ("6410", "standard"),
    "xero": ("6410", "standard"),
    "microsoft": ("6410", "standard"),
    "google workspace": ("6410", "standard"),
    "account fee": ("6500", "input_taxed"),
    "bank fee": ("6500", "input_taxed"),
    "platform settlement fee": ("6500", "input_taxed"),
    "merchant fee": ("6500", "input_taxed"),
    "accounting": ("6600", "standard"),
    "legal": ("6600", "standard"),
    "insurance": ("6700", "standard"),
    "payroll": ("6000", "none"),
    "wages": ("6000", "none"),
    "salary": ("6000", "none"),
    "salaries": ("6000", "none"),
    "superannuation": ("6010", "none"),
    "ato": ("6900", "none"),
    "coworking": ("6900", "standard"),
    "plumbing repair": ("6110", "standard"),
    "warehouse supplies": ("6400", "standard"),
    "contract": ("5100", "standard"),
    "capital": ("1700", "capital"),
}

def _tax_code_value(value: TaxCode | str | None) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _suggested_gst_amount(amount: Decimal, tax_code: TaxCode | str | None) -> str | None:
    if tax_code is None:
        return None
    try:
        tc = TaxCode(_tax_code_value(tax_code))
    except ValueError:
        return None
    if tc in _GST_BEARING_CODES:
        return str((amount / Decimal("11")).quantize(Decimal("0.01")))
    return "0.00"


def _accounts_by_code(db: Session) -> dict[str, Account]:
    return {
        a.code: a
        for a in db.query(Account).filter(Account.active.is_(True)).all()
    }


def heuristic_suggestion(
    accounts_by_code: dict[str, Account],
    *,
    direction: str,
    memo: str | None,
    counter_party: str | None,
) -> tuple[Account, str, str] | None:
    haystack = f"{memo or ''} {counter_party or ''}".lower()
    if not haystack.strip():
        return None
    for keyword, (code, tax_code) in _MEMO_HEURISTICS.items():
        if keyword in haystack:
            account = accounts_by_code.get(code)
            if account is not None and _account_allowed_for_direction(account, direction):
                return account, tax_code, keyword
    return None


def _account_allowed_for_direction(account: Account, direction: str) -> bool:
    account_type = AccountType(account.type)
    if direction == "in":
        return account_type not in {AccountType.EXPENSE, AccountType.COST_OF_SALES}
    if direction == "out":
        return account_type != AccountType.INCOME
    return True



def load_active_rules(db: Session) -> list[BankRule]:
    return (
        db.query(BankRule)
        .filter(BankRule.is_active.is_(True))
        .order_by(BankRule.priority.asc(), BankRule.id.asc())
        .all()
    )


def match_rule(
    rules: list[BankRule],
    *,
    direction: str,
    amount: Decimal,
    memo: str | None,
    counter_party: str | None,
) -> BankRule | None:
    """Return the first rule whose every non-null match clause matches."""
    import re

    for r in rules:
        if r.match_direction and r.match_direction != direction:
            continue
        if r.match_amount_min is not None and amount < r.match_amount_min:
            continue
        if r.match_amount_max is not None and amount > r.match_amount_max:
            continue
        if r.match_memo_regex:
            try:
                if not re.search(r.match_memo_regex, memo or "", re.IGNORECASE):
                    continue
            except re.error:
                # malformed regex → treat as non-matching, don't crash import
                continue
        if r.match_counter_party_regex:
            try:
                if not re.search(
                    r.match_counter_party_regex,
                    counter_party or "",
                    re.IGNORECASE,
                ):
                    continue
            except re.error:
                continue
        return r
    return None


# ---------------------------------------------------------------------------
# Top-level preview + commit
# ---------------------------------------------------------------------------


def preview_import(
    db: Session,
    *,
    bank_account_id: int,
    content: bytes,
    filename: str,
    bank_format: str | None = None,
) -> dict[str, Any]:
    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise ValueError(f"Bank account {bank_account_id} not found")
    if not bank.is_active:
        raise ValueError(f"Bank account {bank.name} is inactive")

    parsed = parse_statement(content=content, filename=filename, bank_format=bank_format)
    rules = load_active_rules(db)
    accounts_by_code = _accounts_by_code(db)

    # First pass: materialise rows + compute dedup keys.
    materialised: list[dict[str, Any]] = []
    for row in parsed["rows"]:
        shape = _row_to_txn_shape(row["raw"], parsed["mapping"])
        if shape.get("ambiguous"):
            materialised.append({
                "row_no": row["row_no"],
                "cells": row["cells"],
                "parsed": shape,
                "ok": False,
                "issue": "Row has both a debit and a credit — can't tell the direction",
            })
            continue
        if not shape["occurred_at"] or not shape["direction"] or not shape["amount"]:
            issue = (
                "Zero-amount rows are skipped; bank transactions must be non-zero"
                if _row_has_zero_amount(row["raw"], parsed["mapping"])
                else "Could not extract date / direction / amount"
            )
            materialised.append({
                "row_no": row["row_no"],
                "cells": row["cells"],
                "parsed": shape,
                "ok": False,
                "issue": issue,
            })
            continue
        dk = compute_dedup_key(
            bank_account_id=bank_account_id,
            direction=shape["direction"],
            amount=shape["amount"],
            occurred_at=shape["occurred_at"],
            memo=shape["memo"],
        )
        materialised.append({
            "row_no": row["row_no"],
            "cells": row["cells"],
            "parsed": shape,
            "dedup_key": dk,
            "ok": True,
        })

    # Look up which dedup keys are already in the DB.
    seen_keys = existing_dedup_keys(
        db,
        bank_account_id=bank_account_id,
        keys=[m["dedup_key"] for m in materialised if m.get("ok")],
    )
    # Fingerprint index of EVERY existing txn on the account (incl. manual rows
    # with no dedup_key) so a row duplicating one of those is still flagged.
    existing_index = existing_txn_fingerprints(db, bank_account_id=bank_account_id)

    # Second pass: rule matching + dedup status. `batch_seen` catches rows that
    # duplicate an EARLIER row in the same file — commit already skips these
    # (seen.add per payload row), so the preview must show it too or the count
    # ("Will import N") won't match what commit actually creates.
    batch_seen: set[str] = set()
    for m in materialised:
        if not m.get("ok"):
            continue
        p = m["parsed"]
        dk = m["dedup_key"]
        is_batch_dup = dk in batch_seen
        if not is_batch_dup:
            batch_seen.add(dk)
        m["is_duplicate"] = (
            dk in seen_keys
            or is_batch_dup
            or fingerprint_is_duplicate(
                existing_index,
                direction=p["direction"],
                amount=p["amount"],
                occurred_at=p["occurred_at"],
                memo=p["memo"],
                counter_party=p["counter_party_name"],
            )
        )
        rule = match_rule(
            rules,
            direction=p["direction"],
            amount=Decimal(p["amount"]),
            memo=p["memo"],
            counter_party=p["counter_party_name"],
        )
        amount = Decimal(p["amount"])
        if rule is not None:
            rule_account = db.get(Account, rule.set_account_id)
            if (
                rule_account is None
                or not rule_account.active
                or not _account_allowed_for_direction(rule_account, p["direction"])
            ):
                # Bank rules are still automatic classification. Keep them on the
                # safe side of the cash direction; supplier refunds and other
                # contra cases can be categorised manually from reconciliation.
                rule = None

        if rule is not None:
            tax_code = _tax_code_value(rule.set_tax_code)
            m["suggested_account_id"] = rule.set_account_id
            m["suggested_tax_code"] = tax_code
            m["suggested_gst_amount"] = _suggested_gst_amount(amount, tax_code)
            m["suggestion_source"] = "rule"
            m["matched_rule_id"] = rule.id
            m["matched_rule_description"] = rule.description
        else:
            suggestion = heuristic_suggestion(
                accounts_by_code,
                direction=p["direction"],
                memo=p["memo"],
                counter_party=p["counter_party_name"],
            )
            if suggestion is not None:
                account, tax_code, keyword = suggestion
                m["suggested_account_id"] = account.id
                m["suggested_tax_code"] = tax_code
                m["suggested_gst_amount"] = _suggested_gst_amount(amount, tax_code)
                m["suggestion_source"] = "heuristic"
                m["matched_rule_id"] = None
                m["matched_rule_description"] = keyword
            else:
                m["suggested_account_id"] = None
                m["suggested_tax_code"] = "standard"
                m["suggested_gst_amount"] = None
                m["suggestion_source"] = None
                m["matched_rule_id"] = None
                m["matched_rule_description"] = None

    return {
        "bank_account_id": bank_account_id,
        "headers": parsed["headers"],
        "mapping": parsed["mapping"],
        "field_options": parsed["field_options"],
        "rows": materialised,
    }


def commit_import(
    db: Session,
    *,
    bank_account_id: int,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write the rows the user accepted.

    Each row in `rows` must carry:
      - occurred_at (str ISO)
      - direction ("in" | "out")
      - amount (Decimal-as-string, positive)
      - dedup_key (from preview)
      - account_id (int | None)
      - tax_code (TaxCode value)
      - memo (str | None)
      - counter_party_name (str | None)
      - gst_amount (Decimal-as-string, default 0)

    Rows that duplicate an existing transaction are silently skipped: by
    dedup_key (exact re-import) AND by fingerprint (amount+date+direction with
    memo/counter-party overlap) so a row duplicating a MANUALLY-entered
    transaction — which has no dedup_key — is enforced server-side too, not only
    flagged in the preview. Guards against double-counting bank cash / GST / P&L
    / BAS even if a caller submits a preview-flagged duplicate.
    """
    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise ValueError(f"Bank account {bank_account_id} not found")
    if not bank.is_active:
        raise ValueError(f"Bank account {bank.name} is inactive")

    seen = existing_dedup_keys(
        db,
        bank_account_id=bank_account_id,
        keys=[r["dedup_key"] for r in rows if r.get("dedup_key")],
    )
    existing_index = existing_txn_fingerprints(db, bank_account_id=bank_account_id)

    created = 0
    skipped = 0
    for r in rows:
        dk = r.get("dedup_key")
        if dk and dk in seen:
            skipped += 1
            continue
        try:
            direction = BankTxnDirection(r["direction"])
            tc = TaxCode(r.get("tax_code") or "standard")
        except (KeyError, ValueError) as e:
            raise ValueError(f"Row {r}: invalid direction or tax_code: {e}")

        gst_amount = Decimal(r.get("gst_amount") or "0")
        amount = Decimal(r["amount"])
        occurred_at = date.fromisoformat(r["occurred_at"])
        try:
            check_txn_date(occurred_at)
        except ValueError as e:
            raise ValueError(f"Row {r}: {e}")
        memo = r.get("memo") or None
        counter_party = r.get("counter_party_name") or None
        # Skip a row that duplicates a transaction that was ALREADY on the
        # account before this payload (including manual rows with no dedup_key) —
        # the same fingerprint the preview flags with, so "Will import N" matches
        # what commit creates. The index is built once from the DB and NOT
        # extended with rows created in this batch: two genuinely distinct
        # same-day/same-amount payments to the same payee (different memos, hence
        # different dedup_keys) are legitimately imported, not silently collapsed.
        if fingerprint_is_duplicate(
            existing_index,
            direction=direction.value,
            amount=amount,
            occurred_at=occurred_at,
            memo=memo,
            counter_party=counter_party,
        ):
            skipped += 1
            continue
        account_id = r.get("account_id")
        acc = None
        if account_id is not None:
            acc = db.get(Account, account_id)
            if acc is None:
                raise ValueError(f"Row {r}: account {account_id} not found")
            if not acc.active:
                raise ValueError(f"Row {r}: account {acc.code} is inactive")
            try:
                reject_income_category_for_matching_ar_payment(
                    db,
                    direction=direction,
                    amount=amount,
                    account=acc,
                    memo=memo,
                    counter_party_name=counter_party,
                )
                reject_expense_category_for_matching_ap_payment(
                    db,
                    direction=direction,
                    amount=amount,
                    account=acc,
                    memo=memo,
                    counter_party_name=counter_party,
                )
            except InvoicePaymentWouldDoubleCount as e:
                raise ValueError(f"Row {r}: {e}") from e
        # Sanity: forbid GST on non-standard/capital codes.
        if tc not in (TaxCode.STANDARD, TaxCode.CAPITAL) and gst_amount > 0:
            raise ValueError(
                f"Row {r}: tax_code={tc.value} forbids gst_amount > 0"
            )
        if gst_amount > amount:
            raise ValueError(
                f"Row {r}: gst_amount {gst_amount} > amount {amount}"
            )

        txn = BankTransaction(
            bank_account_id=bank_account_id,
            direction=direction,
            amount=amount,
            occurred_at=occurred_at,
            memo=memo,
            counter_party_name=counter_party,
            account_id=account_id,
            gst_amount=gst_amount,
            tax_code=tc,
            dedup_key=dk,
        )
        db.add(txn)
        created += 1
        if dk:
            seen.add(dk)  # don't double-add within the same payload

    db.commit()
    return {"created": created, "skipped_duplicates": skipped}
