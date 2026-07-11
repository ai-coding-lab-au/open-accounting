"""Reports router (M3).

Read-only endpoints serving the four reports needed for monthly close /
quarterly compliance:

  GET /reports/bank-statement?bank_account_id=&year=&month=
  GET /reports/profit-loss?period_start=&period_end=
  GET /reports/bas?fy_year=&quarter=

Each also has a sibling .pdf endpoint (same path + /pdf suffix) that
renders the same payload to a downloadable A4 PDF.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...deps import get_company_db, get_current_company
from ...models.master import Company
from ...schemas.reports import (
    BalanceSheetOut,
    BASOut,
    BankStatementOut,
    GSTExposureOut,
    PnLOut,
    TrialBalanceOut,
)
from ...schemas._limits import SQLITE_INT_MAX
from ...services import gst as gst_svc
from ...services import reports as reports_svc
from ...services import report_render, signing_agents
from ...services import trial_balance as trial_balance_svc
from ...services.account_invariants import AccountInvariantError
from ...utils.http import safe_filename


router = APIRouter(prefix="/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# 1) Monthly bank statement
# ---------------------------------------------------------------------------


@router.get("/bank-statement", response_model=BankStatementOut)
def bank_statement(
    bank_account_id: int = Query(..., ge=1, le=SQLITE_INT_MAX),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return reports_svc.bank_statement(
            db, bank_account_id=bank_account_id, year=year, month=month
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/bank-statement/pdf")
def bank_statement_pdf(
    bank_account_id: int = Query(..., ge=1, le=SQLITE_INT_MAX),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        data = reports_svc.bank_statement(
            db, bank_account_id=bank_account_id, year=year, month=month
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    pdf_bytes = report_render.render_bank_statement_pdf(company=company, data=data, signing_agent=signing_agents.first_active_mara(db))
    ba_slug = safe_filename(data["bank_account_name"], default="account")
    filename = f"bank-statement-{ba_slug}-{year}-{month:02d}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 2) Profit & Loss
# ---------------------------------------------------------------------------


@router.get("/profit-loss", response_model=PnLOut)
def profit_loss(
    period_start: date = Query(...),
    period_end: date = Query(...),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return reports_svc.profit_and_loss(
            db, period_start=period_start, period_end=period_end
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/profit-loss/pdf")
def profit_loss_pdf(
    period_start: date = Query(...),
    period_end: date = Query(...),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        data = reports_svc.profit_and_loss(
            db, period_start=period_start, period_end=period_end
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    pdf_bytes = report_render.render_pnl_pdf(company=company, data=data, signing_agent=signing_agents.first_active_mara(db))
    filename = f"profit-loss-{period_start}_{period_end}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 5) Trial Balance (M2.2)
# ---------------------------------------------------------------------------


@router.get("/trial-balance", response_model=TrialBalanceOut)
def trial_balance(
    as_of: date | None = Query(default=None),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return trial_balance_svc.trial_balance(db, as_of=as_of)
    except AccountInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/trial-balance/pdf")
def trial_balance_pdf(
    as_of: date | None = Query(default=None),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        data = trial_balance_svc.trial_balance(db, as_of=as_of)
    except AccountInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    pdf_bytes = report_render.render_trial_balance_pdf(company=company, data=data, signing_agent=signing_agents.first_active_mara(db))
    as_of_str = (as_of or date.today()).isoformat()
    filename = f"trial-balance-{as_of_str}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 6) Balance Sheet (M2.2)
# ---------------------------------------------------------------------------


@router.get("/balance-sheet", response_model=BalanceSheetOut)
def balance_sheet(
    as_of: date | None = Query(default=None),
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        return trial_balance_svc.balance_sheet(db, as_of=as_of)
    except AccountInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/balance-sheet/pdf")
def balance_sheet_pdf(
    as_of: date | None = Query(default=None),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    try:
        data = trial_balance_svc.balance_sheet(db, as_of=as_of)
    except AccountInvariantError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    pdf_bytes = report_render.render_balance_sheet_pdf(company=company, data=data, signing_agent=signing_agents.first_active_mara(db))
    as_of_str = data["as_of"].isoformat() if hasattr(data["as_of"], "isoformat") else str(data["as_of"])
    filename = f"balance-sheet-{as_of_str}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 4) BAS — quarterly GST return
# ---------------------------------------------------------------------------


@router.get("/bas", response_model=BASOut)
def bas(
    fy_year: int = Query(..., ge=2000, le=2100, description="AU FY ending year, e.g. 2026 = Jul 2025 → Jun 2026"),
    quarter: int = Query(..., ge=1, le=4),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return reports_svc.bas(db, fy_year=fy_year, quarter=quarter, gst_registered=company.gst_registered)


@router.get("/bas/pdf")
def bas_pdf(
    fy_year: int = Query(..., ge=2000, le=2100),
    quarter: int = Query(..., ge=1, le=4),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    data = reports_svc.bas(db, fy_year=fy_year, quarter=quarter, gst_registered=company.gst_registered)
    pdf_bytes = report_render.render_bas_pdf(company=company, data=data, signing_agent=signing_agents.first_active_mara(db))
    filename = f"gst-summary-FY{fy_year}-Q{quarter}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 7) GST exposure (M2.3) — richer breakdown than BAS placeholder.
# ---------------------------------------------------------------------------


@router.get("/gst-exposure", response_model=GSTExposureOut)
def gst_exposure(
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    fy_year: int | None = Query(default=None, ge=2000, le=2100),
    quarter: int | None = Query(default=None, ge=1, le=4),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    """Either pass (period_start, period_end) for an arbitrary window or
    (fy_year, quarter) for the standard AU BAS quarter."""
    if fy_year is not None and quarter is not None:
        data = gst_svc.gst_exposure_for_quarter(
            db,
            fy_year=fy_year,
            quarter=quarter,
            gst_registered=company.gst_registered,
        )
        data["gst_registered"] = company.gst_registered
        return data
    if period_start is None or period_end is None:
        raise HTTPException(
            400,
            "Provide either (period_start, period_end) or (fy_year, quarter).",
        )
    try:
        data = gst_svc.gst_exposure(
            db,
            period_start=period_start,
            period_end=period_end,
            gst_registered=company.gst_registered,
        )
        data["gst_registered"] = company.gst_registered
        return data
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/gst-exposure/pdf")
def gst_exposure_pdf(
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    fy_year: int | None = Query(default=None, ge=2000, le=2100),
    quarter: int | None = Query(default=None, ge=1, le=4),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    if fy_year is not None and quarter is not None:
        data = gst_svc.gst_exposure_for_quarter(
            db,
            fy_year=fy_year,
            quarter=quarter,
            gst_registered=company.gst_registered,
        )
        filename = f"gst-tax-code-analysis-FY{fy_year}-Q{quarter}.pdf"
    else:
        if period_start is None or period_end is None:
            raise HTTPException(
                400,
                "Provide either (period_start, period_end) or (fy_year, quarter).",
            )
        try:
            data = gst_svc.gst_exposure(
                db,
                period_start=period_start,
                period_end=period_end,
                gst_registered=company.gst_registered,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        filename = f"gst-tax-code-analysis-{period_start}_{period_end}.pdf"
    pdf_bytes = report_render.render_gst_exposure_pdf(company=company, data=data, signing_agent=signing_agents.first_active_mara(db))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
