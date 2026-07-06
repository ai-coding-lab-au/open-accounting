"""Per-bank PDF statement parsers + a generic best-effort fallback.

Each parser turns a statement PDF into normalised rows:

    {"date": "<raw date text>", "description": str,
     "counter_party": str | None, "amount": "<signed decimal>"}

where `amount` is signed: ``+`` = money in (credit), ``-`` = money out (debit).
The dispatcher (``bank_pdf.parse_pdf``) turns these into the same
``(headers, data_rows)`` shape the CSV/XLSX importers produce, so the rest of the
import pipeline (mapping, dedup, suggestions, commit) is unchanged.

Accuracy for a specific bank comes from tuning its parser against a real
(de-identified) sample statement. Until a bank is tuned, it delegates to the
generic line parser, and the import-preview UI lets the user review/fix every
row before commit.
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .extract import extract_lines, extract_text

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# A money token: 1,234.56 / 1234.56 / -1,234.56 / (1,234.56) / $1,234.56,
# optional CR|DR. Digits may be comma-grouped OR ungrouped (some banks omit the
# thousands separator, e.g. "1000.00"). Two decimals are required so bare
# integers (e.g. a running row number) don't get mistaken for amounts.
_AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
# Boundaries so an amount isn't pulled out of a longer alphanumeric run: reject a
# preceding word char / decimal point (so `REF1000.00` and `4.50%`→`4.50` inside
# `4.50000` don't match) and a trailing digit (so `12.345` isn't truncated to
# `12.34`).
_MONEY_RE = re.compile(
    r"(?<![\w.])\(?-?\$?" + _AMOUNT + r"(?!\d)\)?(?:\s?(?:CR|DR))?",
    re.IGNORECASE,
)

# A leading date token in the common AU formats.
_DATE_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4}"
    r"|\d{1,2}-\d{1,2}-\d{2,4}"
    r"|\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4})"
)


def _money_to_signed(token: str) -> Decimal | None:
    """Parse one money token into a signed Decimal (CR/parens handling)."""
    s = token.strip()
    marker = None
    m = re.search(r"(CR|DR)$", s, re.IGNORECASE)
    if m:
        marker = m.group(1).upper()
        s = s[: m.start()].strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    if marker == "DR" or negative:
        value = -abs(value)
    elif marker == "CR":
        value = abs(value)
    return value


def _money_positions(text: str) -> list[tuple[int, Decimal]]:
    out: list[tuple[int, Decimal]] = []
    for m in _MONEY_RE.finditer(text):
        v = _money_to_signed(m.group(0))
        if v is not None:
            out.append((m.start(), v))
    return out


def parse_generic(content: bytes) -> list[dict]:
    """Best-effort parser for any text-based statement.

    A transaction line has a leading date and one or more money tokens. When a
    line carries a running balance (2+ money tokens), the transaction amount's
    sign is inferred from the balance movement — the most reliable cross-bank
    signal. Otherwise the token's own sign / CR-DR marker is used.
    """
    rows: list[dict] = []
    prev_balance: Decimal | None = None
    for line in extract_lines(content):
        dm = _DATE_RE.search(line)
        if not dm:
            continue
        rest = line[dm.end():]
        monies = _money_positions(rest)
        if not monies:
            continue

        first_pos = monies[0][0]
        description = rest[:first_pos].strip(" .\t-")

        if len(monies) >= 2:
            balance = monies[-1][1]
            amount = monies[-2][1]
            if prev_balance is not None and balance != prev_balance:
                delta = balance - prev_balance
                amount = abs(amount) if delta > 0 else -abs(amount)
            prev_balance = balance
        else:
            amount = monies[0][1]

        rows.append(
            {
                "date": dm.group(1),
                "description": description,
                "counter_party": None,
                "amount": f"{amount:.2f}",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Per-bank parsers.
#
# These currently delegate to the generic line parser. Each will be specialised
# (date format, column order, debit/credit vs signed, CR/DR markers) once a real
# de-identified sample for that bank is available — the registry + detection make
# that a localised change with no impact on the rest of the pipeline.
# ---------------------------------------------------------------------------


# --- Commonwealth Bank (CBA) --------------------------------------------------
#
# CBA transaction listings use fixed columns: Date | Transaction | Debit |
# Credit | Balance. A transaction spans several lines — the merchant/description
# is on the row that starts with the posting date ("01 Jan", no year), with
# "Card xxNNNN" / "Value Date: DD/MM/YYYY" sub-rows, and the amount lands in the
# Debit or Credit column (balance is "$X CR"). Direction comes from which column
# the amount sits in, which we recover from word x-positions (pdfplumber), not
# from collapsed plain text.

_MONEY_WORD = re.compile(r"^\$?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}$")
_DMY = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _group_lines(words: list[dict], tol: float = 3.0) -> list[list[dict]]:
    """Group extracted words into visual rows by their `top` coordinate."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"] / tol), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def _nearest(x: float, centers: dict[str, float]) -> str:
    return min(centers, key=lambda k: abs(centers[k] - x))


_STMT_PERIOD_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s*[-–—]\s*(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})"
)


def _parse_period(text: str):
    """Extract the 'Statement Period DD Mon YYYY - DD Mon YYYY' range."""
    m = _STMT_PERIOD_RE.search(text)
    if not m:
        return None
    try:
        start = date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        end = date(int(m.group(6)), _MONTHS[m.group(5)[:3].lower()], int(m.group(4)))
    except (ValueError, KeyError):
        return None
    return (start, end) if start <= end else None


def _year_for(month: int, day: int, period, fallback: int, value_date) -> int:
    """Resolve the year for a 'DD Mon' posting date. The statement period is
    authoritative (it can span a year boundary, e.g. 31 Dec 2025 - 30 Jun 2026);
    otherwise fall back to the transaction's Value Date, then a carried year."""
    if period is not None:
        start, end = period
        for yr in (start.year, end.year):
            try:
                cand = date(yr, month, day)
            except ValueError:
                continue
            if start <= cand <= end:
                return yr
        return start.year if month >= start.month else end.year
    if value_date is not None:
        vy, vm, _ = value_date
        return vy + 1 if (month < vm and (vm - month) > 6) else vy
    return fallback


def _parse_cba_geometric(content: bytes) -> list[dict]:
    import pdfplumber

    doc_year = None
    period = None
    out: list[dict] = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            # x_tolerance=1: CommBank statements render text with tight spacing,
            # so the default tolerance jams words together ("01Jan",
            # "CHEMISTWAREHOUSE…"). A small tolerance splits them into real words
            # while keeping the column x-positions intact.
            words = page.extract_words(
                use_text_flow=False, keep_blank_chars=False, x_tolerance=1
            )
            if not words:
                continue
            page_text = page.extract_text(x_tolerance=1) or ""
            if period is None:
                period = _parse_period(page_text)  # from the summary/header page
            if doc_year is None:
                m = _DMY.findall(page_text)
                if m:
                    doc_year = max(int(y) for _, _, y in m)

            lines = _group_lines(words)

            # Header row → column centres.
            centers: dict[str, float] | None = None
            body_start = 0
            for i, ln in enumerate(lines):
                texts = {w["text"].lower(): (w["x0"] + w["x1"]) / 2 for w in ln}
                if "debit" in texts and "credit" in texts and "balance" in texts:
                    centers = {
                        "date": min(w["x0"] for w in ln),
                        "txn": texts.get("transaction", 120.0),
                        "debit": texts["debit"],
                        "credit": texts["credit"],
                        "balance": texts["balance"],
                    }
                    body_start = i + 1
                    break
            if centers is None:
                continue

            # Money lands only in Debit/Credit/Balance; classify by nearest of
            # those three centres (never pull a merchant word into a column).
            amount_centers = {k: centers[k] for k in ("debit", "credit", "balance")}
            current: dict | None = None
            # Year to use for a date with no Value Date — carried from the last
            # value-dated transaction (statements are chronological).
            last_year = doc_year or date.today().year

            def flush(tx: dict | None) -> None:
                if not tx or tx["amount"] is None:
                    return
                out.append(
                    {
                        "date": tx["date"],
                        "description": " ".join(tx["desc"]).strip(),
                        "counter_party": None,
                        "amount": f"{tx['amount']:.2f}",
                    }
                )

            for ln in lines[body_start:]:
                first = ln[0]["text"]
                second = ln[1]["text"].lower() if len(ln) > 1 else ""
                starts_txn = bool(re.fullmatch(r"\d{1,2}", first)) and second[:3] in _MONTHS

                line_text = " ".join(w["text"] for w in ln)
                low = line_text.lower()
                dm = _DMY.search(line_text)
                value_date = None
                if dm and "value date" in low:
                    value_date = (int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))

                amount = None
                desc_words: list[str] = []
                line_has_money = False
                for idx, w in enumerate(ln):
                    t = w["text"]
                    if _MONEY_WORD.match(t):
                        line_has_money = True
                        col = _nearest((w["x0"] + w["x1"]) / 2, amount_centers)
                        val = _money_to_signed(t)
                        if val is None:
                            continue
                        if col == "debit":
                            amount = -abs(val)
                        elif col == "credit":
                            amount = abs(val)
                        # balance → ignore
                        continue
                    if starts_txn and idx < 2:
                        continue  # leading "DD" "Mon"
                    if (
                        t.upper() in ("CR", "DR")
                        or t == "*"
                        or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", t)
                    ):
                        continue
                    desc_words.append(t)

                # A sub-line (Card / Value Date, or any continuation carrying the
                # amount) never contributes to the description — only the start
                # line and genuinely wrapped description lines do.
                is_sub = low.startswith("card") or ("value" in low and "date" in low)

                if starts_txn:
                    flush(current)
                    current = {
                        "day": int(first),
                        "month": _MONTHS[second[:3]],
                        "date": None,
                        "value_date": value_date,
                        "desc": desc_words,
                        "amount": amount,
                    }
                elif current is not None:
                    if value_date is not None:
                        current["value_date"] = value_date
                    if not is_sub and not line_has_money:
                        current["desc"].extend(desc_words)
                    if amount is not None and current["amount"] is None:
                        current["amount"] = amount

                if current is not None and (
                    current["date"] is None or current["value_date"] is not None
                ):
                    yr = _year_for(
                        current["month"], current["day"], period, last_year, current["value_date"]
                    )
                    current["date"] = f"{yr}-{current['month']:02d}-{current['day']:02d}"
                    last_year = yr

            flush(current)

    return out


def parse_cba(content: bytes) -> list[dict]:
    """Commonwealth Bank statement parser (column-geometry based) with the
    generic line parser as a fallback if the layout isn't recognised."""
    try:
        rows = _parse_cba_geometric(content)
    except Exception:
        rows = []
    return rows if rows else parse_generic(content)


def parse_nab(content: bytes) -> list[dict]:
    return parse_generic(content)


def parse_anz(content: bytes) -> list[dict]:
    return parse_generic(content)


def parse_westpac(content: bytes) -> list[dict]:
    return parse_generic(content)


BANK_PARSERS = {
    "cba": parse_cba,
    "nab": parse_nab,
    "anz": parse_anz,
    "westpac": parse_westpac,
}
SUPPORTED_BANKS = tuple(BANK_PARSERS)


_FINGERPRINTS: dict[str, list[str]] = {
    "cba": [r"commonwealth\s+bank", r"commbank", r"\bnetbank\b"],
    "nab": [r"national\s+australia\s+bank", r"\bnab\b"],
    "westpac": [r"\bwestpac\b"],
    "anz": [r"australia and new zealand banking", r"\banz\b"],
}


def detect_bank(text: str) -> str | None:
    """Guess the bank from brand text in the statement; None if unrecognised."""
    t = text.lower()
    for bank, patterns in _FINGERPRINTS.items():
        if any(re.search(p, t) for p in patterns):
            return bank
    return None


def detect_bank_from_content(content: bytes) -> str | None:
    return detect_bank(extract_text(content))
