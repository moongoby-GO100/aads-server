"""API routes for the Yeoljeong store-assistant tax/accounting module.

AADS 경로에서는 app/main.py 가 prefix /api/v1 로 이미 탑재한다. standalone(ACCT 114)에는 탑재하지 않는다.

권한 모델
---------
- 변경계(POST/PATCH/DELETE)는 owner/admin 만 허용한다(_require_manager).
- business_id 를 받는 모든 경로와 id 단건 경로는 JWT 테넌트가 그 사업자를 소유하는지 확인한다
  (_scope_business). 소유하지 않으면 404 — 역할이 높아도 테넌트 경계는 넘지 못한다.
- 세금신고서·분류규칙·전표 쓰기는 OBYS_JOURNAL_WRITE_FROZEN(기본 차단)을 따른다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.core.obys_tenant import require_legacy_obys_access
from app.services import obys_upload_service as upload_svc
from app.services import yeoljeong_accounting_service as svc

router = APIRouter(
    prefix="/yeoljeong-accounting",
    tags=["yeoljeong-accounting"],
    dependencies=[Depends(require_legacy_obys_access)],
)
logger = logging.getLogger(__name__)


def _actor_of(current_user: dict[str, Any]) -> str:
    return str(current_user.get("email") or current_user.get("user_id") or "unknown")


def _is_manager(user: dict[str, Any]) -> bool:
    """owner/admin 멤버십, 또는 내부 관리자 주체. 계약 쪽 _is_admin 과 같은 특권 주체 기준이다."""
    user_role = str(user.get("user_role") or "").strip().lower()
    if user.get("is_internal_admin") or user_role in {"ceo", "admin", "system"}:
        return True
    membership = user.get("current_membership")
    if not isinstance(membership, dict):
        return False
    return (
        str(membership.get("status") or "").strip().lower() == "active"
        and str(membership.get("tenant_id") or "").strip() == str(user.get("tenant_id") or "").strip()
        and str(membership.get("role") or "").strip().lower() in {"owner", "admin"}
    )


async def _require_manager(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    if not _is_manager(current_user):
        raise HTTPException(status_code=403, detail="이 작업은 소유자·관리자만 할 수 있습니다")
    return current_user


async def _scope_business(current_user: dict[str, Any], business_id: str | None) -> str:
    """JWT 테넌트가 소유한 사업자인지 확인한다. 아니면 404(존재 여부를 알리지 않는다)."""
    business = str(business_id or "").strip()
    if not business:
        raise HTTPException(status_code=404, detail="현재 테넌트의 사업자를 찾을 수 없습니다")
    tenant_id = upload_svc._tenant(current_user)
    conn = await upload_svc._connect()
    try:
        await upload_svc._require_business(conn, tenant_id, business)
    finally:
        await conn.close()
    return business


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
    # 원천세(withholding) 신고는 소득 구분이 필요하다: business_income / wage_income / daily_wage_income
    income_kind: str | None = None


class TaxReportUpdatePayload(BaseModel):
    # draft / file_generated / submitted(자기선언) / receipt_confirmed(접수번호·접수일시 필수)
    status: str | None = None
    total_sales: float | None = None
    total_purchases: float | None = None
    vat_payable: float | None = None
    tax_amount: float | None = None
    memo: str | None = None
    receipt_number: str | None = None
    receipt_received_at: datetime | None = None


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
    await _scope_business(current_user, business_id)
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
    current_user: dict = Depends(_require_manager),
) -> dict[str, Any]:
    await _scope_business(current_user, payload.business_id)
    entry = await svc.create_entry(payload.model_dump(), _actor_of(current_user))
    return {"entry": entry}


@router.patch("/entries/{entry_id}")
async def update_entry(
    entry_id: str,
    payload: EntryUpdatePayload,
    current_user: dict = Depends(_require_manager),
) -> dict[str, Any]:
    await _scope_business(current_user, await svc.entry_business_id(entry_id))
    entry = await svc.update_entry(entry_id, payload.model_dump(exclude_unset=True))
    if not entry:
        raise HTTPException(status_code=404, detail="accounting entry not found")
    return {"entry": entry}


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: str,
    current_user: dict = Depends(_require_manager),
) -> dict[str, bool]:
    await _scope_business(current_user, await svc.entry_business_id(entry_id))
    ok = await svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="accounting entry not found")
    return {"ok": True}


@router.post("/entries/auto-classify")
async def auto_classify_entries(
    payload: AutoClassifyPayload,
    current_user: dict = Depends(_require_manager),
) -> dict[str, Any]:
    await _scope_business(current_user, payload.business_id)
    return await svc.auto_classify(business_id=payload.business_id, limit=payload.limit)


@router.get("/summary")
async def get_summary(
    business_id: str = "biz-mia",
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    await _scope_business(current_user, business_id)
    return await svc.get_summary(business_id=business_id, date_from=date_from, date_to=date_to)


@router.get("/pnl")
async def get_pnl(
    business_id: str = "biz-mia",
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    await _scope_business(current_user, business_id)
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
    await _scope_business(current_user, business_id)
    rows = await svc.list_tax_reports(business_id=business_id, report_type=report_type)
    return {"tax_reports": rows, "count": len(rows)}


@router.post("/tax-reports")
async def create_tax_report(
    payload: TaxReportCreatePayload,
    current_user: dict = Depends(_require_manager),
) -> dict[str, Any]:
    await _scope_business(current_user, payload.business_id)
    report = await svc.create_tax_report(payload.model_dump())
    return {"tax_report": report}


@router.patch("/tax-reports/{report_id}")
async def update_tax_report(
    report_id: str,
    payload: TaxReportUpdatePayload,
    current_user: dict = Depends(_require_manager),
) -> dict[str, Any]:
    await _scope_business(current_user, await svc.tax_report_business_id(report_id))
    report = await svc.update_tax_report(report_id, payload.model_dump(exclude_unset=True))
    if not report:
        raise HTTPException(status_code=404, detail="tax report not found")
    return {"tax_report": report}


@router.get("/tax-reports/{report_id}/calculate")
async def calculate_tax_report(
    report_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    await _scope_business(current_user, await svc.tax_report_business_id(report_id))
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
    await _scope_business(current_user, business_id)
    rows = await svc.list_category_rules(business_id=business_id)
    return {"rules": rows, "count": len(rows)}


@router.post("/category-rules")
async def create_category_rule(
    payload: CategoryRuleCreatePayload,
    current_user: dict = Depends(_require_manager),
) -> dict[str, Any]:
    await _scope_business(current_user, payload.business_id)
    rule = await svc.create_category_rule(payload.model_dump())
    return {"rule": rule}


@router.delete("/category-rules/{rule_id}")
async def delete_category_rule(
    rule_id: str,
    current_user: dict = Depends(_require_manager),
) -> dict[str, bool]:
    await _scope_business(current_user, await svc.category_rule_business_id(rule_id))
    ok = await svc.delete_category_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="category rule not found")
    return {"ok": True}
