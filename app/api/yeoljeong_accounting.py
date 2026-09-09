"""API routes for the Yeoljeong store-assistant tax/accounting module.

Not registered in main.py yet (per task spec) — wire up with:
    from app.api import yeoljeong_accounting
    app.include_router(yeoljeong_accounting.router, prefix="/api/v1")
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services import yeoljeong_accounting_service as svc

router = APIRouter(prefix="/yeoljeong-accounting", tags=["yeoljeong-accounting"])
logger = logging.getLogger(__name__)


def _actor_of(current_user: dict[str, Any]) -> str:
    return str(current_user.get("email") or current_user.get("user_id") or "unknown")


class EntryCreatePayload(BaseModel):
    business_id: str = "biz-mia"
    entry_date: date
    entry_type: str
    category: str = ""
    subcategory: str = ""
    description: str = ""
    debit_amount: float = 0
    credit_amount: float = 0
    counterparty: str = ""
    source_type: str = "manual"
    source_id: str = ""
    tax_type: str = "vat"
    vat_amount: float = 0
    receipt_id: str = ""
    verified: bool = False
    verified_by: str = ""
    memo: str = ""


class EntryUpdatePayload(BaseModel):
    entry_date: date | None = None
    entry_type: str | None = None
    category: str | None = None
    subcategory: str | None = None
    description: str | None = None
    debit_amount: float | None = None
    credit_amount: float | None = None
    counterparty: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    tax_type: str | None = None
    vat_amount: float | None = None
    receipt_id: str | None = None
    verified: bool | None = None
    verified_by: str | None = None
    memo: str | None = None


class AutoClassifyPayload(BaseModel):
    business_id: str = "biz-mia"
    limit: int = Field(default=500, ge=1, le=2000)


class TaxReportCreatePayload(BaseModel):
    business_id: str = "biz-mia"
    report_type: str
    period_start: date
    period_end: date
    memo: str = ""


class TaxReportUpdatePayload(BaseModel):
    status: str | None = None
    total_sales: float | None = None
    total_purchases: float | None = None
    vat_payable: float | None = None
    tax_amount: float | None = None
    memo: str | None = None


class CategoryRuleCreatePayload(BaseModel):
    business_id: str = "biz-mia"
    keyword: str
    category: str
    subcategory: str = ""
    tax_type: str = "vat"
    priority: int = 0


# ---------------------------------------------------------------------------
# 회계 내역 (/entries)
# ---------------------------------------------------------------------------
@router.get("/entries")
async def list_entries(
    business_id: str = "biz-mia",
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    entry_type: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await svc.list_entries(
        business_id=business_id,
        date_from=date_from,
        date_to=date_to,
        category=category,
        entry_type=entry_type,
    )
    return {"entries": rows, "count": len(rows)}


@router.post("/entries")
async def create_entry(
    payload: EntryCreatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    entry = await svc.create_entry(payload.model_dump(), _actor_of(current_user))
    return {"entry": entry}


@router.patch("/entries/{entry_id}")
async def update_entry(
    entry_id: str,
    payload: EntryUpdatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    entry = await svc.update_entry(entry_id, payload.model_dump(exclude_unset=True))
    if not entry:
        raise HTTPException(status_code=404, detail="accounting entry not found")
    return {"entry": entry}


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    ok = await svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="accounting entry not found")
    return {"ok": True}


@router.post("/entries/auto-classify")
async def auto_classify_entries(
    payload: AutoClassifyPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await svc.auto_classify(business_id=payload.business_id, limit=payload.limit)


@router.get("/summary")
async def get_summary(
    business_id: str = "biz-mia",
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await svc.get_summary(business_id=business_id, date_from=date_from, date_to=date_to)


@router.get("/pnl")
async def get_pnl(
    business_id: str = "biz-mia",
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await svc.get_pnl(business_id=business_id, date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# 세무신고 (/tax-reports)
# ---------------------------------------------------------------------------
@router.get("/tax-reports")
async def list_tax_reports(
    business_id: str = "biz-mia",
    report_type: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await svc.list_tax_reports(business_id=business_id, report_type=report_type)
    return {"tax_reports": rows, "count": len(rows)}


@router.post("/tax-reports")
async def create_tax_report(
    payload: TaxReportCreatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    report = await svc.create_tax_report(payload.model_dump())
    return {"tax_report": report}


@router.patch("/tax-reports/{report_id}")
async def update_tax_report(
    report_id: str,
    payload: TaxReportUpdatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    report = await svc.update_tax_report(report_id, payload.model_dump(exclude_unset=True))
    if not report:
        raise HTTPException(status_code=404, detail="tax report not found")
    return {"tax_report": report}


@router.get("/tax-reports/{report_id}/calculate")
async def calculate_tax_report(
    report_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    result = await svc.calculate_tax_report(report_id)
    if not result:
        raise HTTPException(status_code=404, detail="tax report not found")
    return {"calculation": result}


# ---------------------------------------------------------------------------
# 자동분류 규칙 (/category-rules)
# ---------------------------------------------------------------------------
@router.get("/category-rules")
async def list_category_rules(
    business_id: str = "biz-mia",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await svc.list_category_rules(business_id=business_id)
    return {"rules": rows, "count": len(rows)}


@router.post("/category-rules")
async def create_category_rule(
    payload: CategoryRuleCreatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    rule = await svc.create_category_rule(payload.model_dump())
    return {"rule": rule}


@router.delete("/category-rules/{rule_id}")
async def delete_category_rule(
    rule_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, bool]:
    ok = await svc.delete_category_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="category rule not found")
    return {"ok": True}
