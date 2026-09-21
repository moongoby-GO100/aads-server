"""ACCT 매출 상세 화면 API.

매출 계정은 코드 범위를 추측하지 않고, 회사 범위 안의 실제 ``journal_line`` 중
대변이면서 계정과목명에 '매출'이 포함된 계정으로 확정한다. 모든 원장 조회는
JWT 소유 사업자에서 얻은 tenant/company와 ``source_file.company_id``를 함께
검증한다.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.api.acct_purchase import _authorized_acct_scope, _fetch_acct_journals, _lit
from app.auth import get_current_user

router = APIRouter(prefix="/acct-sales", tags=["acct-sales"])
_UI_PATH = Path(__file__).resolve().parents[1] / "static" / "acct" / "sales.html"


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _date(value: Optional[str], label: str) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} 날짜 형식이 올바르지 않습니다") from exc


async def _scope(current_user: dict, business_id: str, company_id: Optional[int]) -> tuple[int, int]:
    scope = await _authorized_acct_scope(current_user, business_id)
    if scope is None:
        raise HTTPException(status_code=403, detail="사업자 범위가 필요합니다")
    tenant_id, mapped_company_id = scope
    if company_id is not None and company_id != mapped_company_id:
        raise HTTPException(status_code=403, detail="요청한 회사는 현재 사업자에 매핑되지 않습니다")
    return tenant_id, mapped_company_id


def _conditions(
    company_id: int,
    date_from: Optional[str],
    date_to: Optional[str],
    account_code: Optional[str],
    keyword: Optional[str],
) -> str:
    conditions = [
        f"e.company_id = {company_id}",
        f"sf.company_id = {company_id}",
        "l.side = 'credit'",
        "l.account_name ILIKE '%매출%'",
    ]
    if date_from:
        conditions.append(f"e.entry_date >= {_lit(date_from)}::date")
    if date_to:
        conditions.append(f"e.entry_date <= {_lit(date_to)}::date")
    if account_code:
        conditions.append(f"l.account_code = {_lit(account_code)}")
    if keyword:
        value = _lit(keyword)
        conditions.append(
            f"(coalesce(to_jsonb(e)->>'description', to_jsonb(e)->>'memo', '') "
            f"ILIKE '%' || {value} || '%' OR coalesce(to_jsonb(l)->>'memo', '') "
            f"ILIKE '%' || {value} || '%')"
        )
    return " AND ".join(conditions)


@router.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    if not _UI_PATH.exists():
        raise HTTPException(status_code=404, detail="화면 파일을 찾을 수 없습니다")
    return FileResponse(_UI_PATH, media_type="text/html")


@router.get("/accounts")
async def accounts(
    business_id: str = Query(..., min_length=1, max_length=64),
    company_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    tenant_id, mapped_company_id = await _scope(current_user, business_id, company_id)
    rows = await _fetch_acct_journals(
        "SELECT l.account_code, l.account_name, count(*) AS line_count, "
        "sum(l.amount) AS amount FROM journal_line l "
        "JOIN journal_entry e ON e.id = l.entry_id "
        "JOIN source_file sf ON sf.id = e.source_file_id AND sf.company_id = e.company_id "
        f"WHERE {_conditions(mapped_company_id, None, None, None, None)} "
        "GROUP BY l.account_code, l.account_name ORDER BY l.account_code",
        tenant_id,
    )
    return {"items": [{**row, "amount": _number(row.get("amount"))} for row in rows]}


@router.get("/transactions")
async def transactions(
    business_id: str = Query(..., min_length=1, max_length=64),
    company_id: Optional[int] = Query(None),
    entry_date_from: Optional[str] = Query(None),
    entry_date_to: Optional[str] = Query(None),
    account_code: Optional[str] = Query(None, max_length=80),
    keyword: Optional[str] = Query(None, max_length=80),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1000000),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    tenant_id, mapped_company_id = await _scope(current_user, business_id, company_id)
    start, end = _date(entry_date_from, "시작일"), _date(entry_date_to, "종료일")
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="시작일은 종료일보다 뒤일 수 없습니다")
    where = _conditions(mapped_company_id, start, end, account_code, keyword)
    base = (
        " FROM journal_line l JOIN journal_entry e ON e.id = l.entry_id "
        "JOIN source_file sf ON sf.id = e.source_file_id AND sf.company_id = e.company_id "
        f"WHERE {where}"
    )
    rows = await _fetch_acct_journals(
        "SELECT e.id::text AS entry_id, e.entry_date, "
        "coalesce(to_jsonb(e)->>'entry_no', to_jsonb(e)->>'journal_no', "
        "to_jsonb(e)->>'voucher_no', to_jsonb(e)->>'number') AS entry_no, "
        "coalesce(to_jsonb(e)->>'description', to_jsonb(e)->>'memo', "
        "to_jsonb(e)->>'narration') AS description, "
        "coalesce(to_jsonb(e)->>'status', to_jsonb(e)->>'state') AS status, "
        "sf.id AS source_file_id, l.id::text AS line_id, l.account_code, l.account_name, "
        "l.amount, l.tax_code, l.memo" + base +
        f" ORDER BY e.entry_date DESC, e.created_at DESC, l.id LIMIT {limit} OFFSET {offset}",
        tenant_id,
    )
    totals = await _fetch_acct_journals(
        "SELECT count(*) AS count, coalesce(sum(l.amount), 0) AS amount" + base,
        tenant_id,
    )
    total = totals[0] if totals else {}
    return {
        "items": [{**row, "amount": _number(row.get("amount"))} for row in rows],
        "count": len(rows), "offset": offset, "limit": limit,
        "totals": {"count": int(total.get("count") or 0), "amount": _number(total.get("amount"))},
    }


@router.get("/monthly")
async def monthly(
    business_id: str = Query(..., min_length=1, max_length=64),
    company_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, List[Dict[str, Any]]]:
    tenant_id, mapped_company_id = await _scope(current_user, business_id, company_id)
    rows = await _fetch_acct_journals(
        "SELECT to_char(e.entry_date, 'YYYY-MM') AS month, count(*) AS line_count, "
        "sum(l.amount) AS amount FROM journal_line l "
        "JOIN journal_entry e ON e.id = l.entry_id "
        "JOIN source_file sf ON sf.id = e.source_file_id AND sf.company_id = e.company_id "
        f"WHERE {_conditions(mapped_company_id, None, None, None, None)} "
        "GROUP BY 1 ORDER BY 1 DESC",
        tenant_id,
    )
    return {"items": [{**row, "amount": _number(row.get("amount"))} for row in rows]}
