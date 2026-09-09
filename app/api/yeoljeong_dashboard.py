"""Dashboard aggregation API for the Yeoljeong store assistant."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from app.auth import get_current_user
from app.services import yeoljeong_dashboard_service as dash_svc

router = APIRouter(prefix="/yeoljeong-dashboard", tags=["yeoljeong-dashboard"])
logger = logging.getLogger(__name__)


@router.get("/kpis")
async def get_kpis(
    business_id: str = Query("", description="사업자 ID 필터"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"kpis": await run_in_threadpool(dash_svc.get_kpis, business_id)}


@router.get("/sales-trend")
async def get_sales_trend(
    period: str = Query("daily", description="daily|weekly|monthly"),
    days: int = Query(30, ge=1, le=365, description="조회 일수"),
    business_id: str = Query("", description="사업자 ID 필터"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"trend": await run_in_threadpool(dash_svc.get_sales_trend, period, days, business_id)}


@router.get("/tasks")
async def get_tasks(
    business_id: str = Query("", description="사업자 ID 필터"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"tasks": await run_in_threadpool(dash_svc.get_tasks, business_id)}


@router.get("/settlement-summary")
async def get_settlement_summary(
    business_id: str = Query("", description="사업자 ID 필터"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"summary": await run_in_threadpool(dash_svc.get_settlement_summary, business_id)}


@router.get("/expense-summary")
async def get_expense_summary(
    business_id: str = Query("", description="사업자 ID 필터"),
    date_from: str = Query("", description="시작일 YYYY-MM-DD"),
    date_to: str = Query("", description="종료일 YYYY-MM-DD"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"expenses": await run_in_threadpool(dash_svc.get_expense_summary, business_id, date_from, date_to)}
