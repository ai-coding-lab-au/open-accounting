from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...deps import get_company_db, get_current_company
from ...models.master import Company
from ...schemas.dashboard import DashboardSummary
from ...services.dashboard import dashboard_summary


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
def get_dashboard_summary(
    _: Company = Depends(get_current_company),
    db: Session = Depends(get_company_db),
):
    return dashboard_summary(db)
