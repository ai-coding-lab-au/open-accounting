"""Bank auto-categorisation rules CRUD (M3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...deps import PathId, get_company_db, get_current_company
from ...models.company import Account, BankRule, TaxCode
from ...models.master import Company
from ...schemas.bank_import import BankRuleCreate, BankRuleOut, BankRuleUpdate


router = APIRouter(prefix="/bank-rules", tags=["bank-rules"])


def _check_account(db: Session, account_id: int) -> None:
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(400, f"Account {account_id} not found")
    if not a.active:
        raise HTTPException(400, f"Account {a.code} is inactive")


def _check_amount_range(
    minv,
    maxv,
) -> None:
    if minv is not None and maxv is not None and minv > maxv:
        raise HTTPException(400, "match_amount_min must be <= match_amount_max")


@router.get("", response_model=list[BankRuleOut])
def list_rules(
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return (
        db.query(BankRule)
        .order_by(BankRule.priority.asc(), BankRule.id.asc())
        .all()
    )


@router.post("", response_model=BankRuleOut, status_code=201)
def create_rule(
    payload: BankRuleCreate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    _check_account(db, payload.set_account_id)
    _check_amount_range(payload.match_amount_min, payload.match_amount_max)
    rule = BankRule(
        priority=payload.priority,
        is_active=payload.is_active,
        description=payload.description,
        match_direction=payload.match_direction,
        match_amount_min=payload.match_amount_min,
        match_amount_max=payload.match_amount_max,
        match_memo_regex=payload.match_memo_regex,
        match_counter_party_regex=payload.match_counter_party_regex,
        set_account_id=payload.set_account_id,
        set_tax_code=TaxCode(payload.set_tax_code),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/{rule_id}", response_model=BankRuleOut)
def update_rule(
    rule_id: PathId,
    payload: BankRuleUpdate,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    rule = db.get(BankRule, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")

    if payload.set_account_id is not None:
        _check_account(db, payload.set_account_id)
        rule.set_account_id = payload.set_account_id

    new_min = (
        payload.match_amount_min if payload.match_amount_min is not None
        else rule.match_amount_min
    )
    new_max = (
        payload.match_amount_max if payload.match_amount_max is not None
        else rule.match_amount_max
    )
    _check_amount_range(new_min, new_max)

    for field in (
        "priority", "is_active", "description",
        "match_direction", "match_amount_min", "match_amount_max",
        "match_memo_regex", "match_counter_party_regex",
    ):
        v = getattr(payload, field)
        if v is not None:
            setattr(rule, field, v)
    if payload.set_tax_code is not None:
        rule.set_tax_code = TaxCode(payload.set_tax_code)

    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: PathId,
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    rule = db.get(BankRule, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    db.delete(rule)
    db.commit()
    return None
