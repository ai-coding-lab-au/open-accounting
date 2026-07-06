"""Schemas for bank statement import + auto-categorisation rules (M3)."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._money import Money


# ---------------------------------------------------------------------------
# Bank rule
# ---------------------------------------------------------------------------


_TAX_CODE_PATTERN = r"^(standard|gst_free|input_taxed|capital|none)$"
_DIRECTION_PATTERN = r"^(in|out)$"

# Money column shape: NUMERIC(16, 2) — matches the posted-money fields elsewhere.
_MONEY_MAX = Decimal("99999999999999.99")


# Catastrophic-backtracking (ReDoS) signature: a quantifier applied to a group
# whose body is itself quantified — e.g. `(a+)+`, `(a*)*`, `(.+)+`, `(a+)*$`.
# `match_rule` runs these per row at import time with no timeout, so a single
# such rule can hang the whole import (operator self-DoS). A timing probe can't
# interrupt a C-level `re` call (it never yields the GIL), so we detect the
# structure statically instead. This is a conservative heuristic: it rejects the
# classic nested-quantifier shape, not every possible pathological regex.
_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^()]*[+*][^()]*\)\s*[+*]"  # group containing +/* ... immediately quantified
)

# Backreferences (e.g. `(a+)\1`) can drive exponential backtracking and are
# never needed for bank-memo matching, so we reject them outright. Matches
# `\1`..`\99` and `\g<...>` while ignoring a backslash that is itself escaped.
_BACKREFERENCE_RE = re.compile(r"(?<!\\)(?:\\\\)*\\(?:[1-9][0-9]?|g<)")


def _has_quantified_alternation_group(pattern: str) -> bool:
    """Reject a quantified group `(...)[*+]` whose body contains a top-level
    alternation `|` — e.g. `(a|a)*$`, `(\\d|\\d)+$`. Proving the branches are
    disjoint is hard, and overlapping/duplicate branches under a quantifier
    backtrack catastrophically, so we conservatively reject *any* alternation
    inside a group-quantifier. A top-level `|` (e.g. `(?i)rent|lease`) and an
    unquantified group (e.g. `(rent|lease|mortgage)`) are NOT affected — only
    a group immediately followed by `*` or `+`.

    Scans for balanced parenthesised groups, tracking which `|` are at the
    group's own nesting depth, and flags the group if it's immediately
    quantified by `*`/`+`. Character classes `[...]` are skipped so a `|` or
    `(` inside one is not mistaken for structure.
    """
    depth = 0
    # For each currently-open group, whether it has seen a top-level `|`.
    stack: list[bool] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2  # escaped char — skip the next as a literal
            continue
        if c == "[":
            # Skip the character class verbatim.
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":  # literal ] as first member
                i += 1
            while i < n and pattern[i] != "]":
                if pattern[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "(":
            stack.append(False)
            depth += 1
        elif c == ")":
            had_alt = stack.pop() if stack else False
            depth -= 1
            # Is this group immediately quantified by * or + (optionally lazy)?
            j = i + 1
            if had_alt and j < n and pattern[j] in "*+":
                return True
        elif c == "|":
            if stack:
                stack[-1] = True
        i += 1
    return False


def _is_catastrophic(pattern: str) -> bool:
    return (
        _NESTED_QUANTIFIER_RE.search(pattern) is not None
        or _has_quantified_alternation_group(pattern)
        or _BACKREFERENCE_RE.search(pattern) is not None
    )


def _validate_regex(v: str | None) -> str | None:
    """Reject regexes that Python's `re` can't compile, so a malformed rule
    can't be saved (it would silently never match — see bank_import.py).

    Also reject patterns with catastrophic backtracking: `match_rule` runs them
    per row at import time with no timeout, so a single bad rule could hang the
    whole import (operator self-DoS)."""
    if v is None:
        return v
    try:
        re.compile(v)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    if _is_catastrophic(v):
        raise ValueError(
            "Regular expression is too slow (catastrophic backtracking); "
            "simplify it (avoid nested quantifiers like (a+)+, a quantified "
            "alternation like (a|b)+, or backreferences like \\1)."
        )
    return v


class BankRuleBase(BaseModel):
    priority: int = Field(default=100, ge=0)
    is_active: bool = True
    description: str = Field(min_length=1, max_length=200)

    match_direction: str | None = Field(default=None, pattern=_DIRECTION_PATTERN)
    match_amount_min: Decimal | None = Field(default=None, ge=0, le=_MONEY_MAX, decimal_places=2)
    match_amount_max: Decimal | None = Field(default=None, ge=0, le=_MONEY_MAX, decimal_places=2)
    match_memo_regex: str | None = Field(default=None, max_length=500)
    match_counter_party_regex: str | None = Field(default=None, max_length=500)

    set_account_id: int
    set_tax_code: str = Field(default="standard", pattern=_TAX_CODE_PATTERN)

    _check_regex = field_validator(
        "match_memo_regex", "match_counter_party_regex"
    )(_validate_regex)


class BankRuleCreate(BankRuleBase):
    pass


class BankRuleUpdate(BaseModel):
    priority: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    description: str | None = Field(default=None, min_length=1, max_length=200)
    match_direction: str | None = Field(default=None, pattern=_DIRECTION_PATTERN)
    match_amount_min: Decimal | None = Field(default=None, ge=0, le=_MONEY_MAX, decimal_places=2)
    match_amount_max: Decimal | None = Field(default=None, ge=0, le=_MONEY_MAX, decimal_places=2)
    match_memo_regex: str | None = Field(default=None, max_length=500)
    match_counter_party_regex: str | None = Field(default=None, max_length=500)
    set_account_id: int | None = None
    set_tax_code: str | None = Field(default=None, pattern=_TAX_CODE_PATTERN)

    _check_regex = field_validator(
        "match_memo_regex", "match_counter_party_regex"
    )(_validate_regex)


class BankRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    priority: int
    is_active: bool
    description: str
    match_direction: str | None
    match_amount_min: Money | None
    match_amount_max: Money | None
    match_memo_regex: str | None
    match_counter_party_regex: str | None
    set_account_id: int
    set_tax_code: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Bank import preview + commit
# ---------------------------------------------------------------------------


class BankImportRowParsed(BaseModel):
    occurred_at: str | None
    memo: str | None
    counter_party_name: str | None
    direction: str | None
    amount: str | None


class BankImportPreviewRow(BaseModel):
    row_no: int
    cells: list[str]
    parsed: BankImportRowParsed
    ok: bool
    issue: str | None = None
    dedup_key: str | None = None
    is_duplicate: bool | None = None
    suggested_account_id: int | None = None
    suggested_tax_code: str | None = None
    suggested_gst_amount: str | None = None
    suggestion_source: str | None = None
    matched_rule_id: int | None = None
    matched_rule_description: str | None = None


class BankImportPreviewOut(BaseModel):
    bank_account_id: int
    headers: list[str]
    mapping: dict[str, int | None]
    field_options: list[str]
    rows: list[BankImportPreviewRow]


class BankImportCommitRow(BaseModel):
    occurred_at: str
    direction: str = Field(pattern=_DIRECTION_PATTERN)
    amount: str
    dedup_key: str | None = None
    account_id: int | None = None
    tax_code: str = Field(default="standard", pattern=_TAX_CODE_PATTERN)
    memo: str | None = None
    counter_party_name: str | None = None
    gst_amount: str = "0"


class BankImportCommitIn(BaseModel):
    rows: list[BankImportCommitRow]


class BankImportCommitOut(BaseModel):
    created: int
    skipped_duplicates: int
