"""
AADS-125: Strategy Report API
- POST /api/v1/strategy-reports        — 보고서 저장
- GET  /api/v1/strategy-reports        — 프로젝트별 목록
- GET  /api/v1/strategy-reports/{id}   — 단건 조회
- GET  /api/v1/strategy-reports/{id}/candidates — 후보 아이템만 조회
"""
import os
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = structlog.get_logger()
router = APIRouter()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads_dev_local@aads-postgres:5432/aads",
)

KST = timezone(timedelta(hours=9))


async def _get_conn():
    return await asyncpg.connect(DATABASE_URL, timeout=10)


# ─── Request/Response Models ─────────────────────────────────────────────────


class StrategyReportCreate(BaseModel):
    project_id: Optional[str] = None
    direction: str
    strategy_report: dict
    candidates: list = []
    recommendation: Optional[str] = None
    total_sources: int = 0
    cost_usd: float = 0.0
    model_used: Optional[str] = None


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/strategy-reports", status_code=200)
async def create_strategy_report(req: StrategyReportCreate):
    """전략 보고서 저장."""
    try:
        conn = await _get_conn()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_reports
                    (project_id, direction, strategy_report, candidates,
                     recommendation, total_sources, cost_usd, model_used)
                VALUES ($1::uuid, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8)
                RETURNING id, created_at
                """,
                req.project_id,
                req.direction,
                json.dumps(req.strategy_report, ensure_ascii=False),
                json.dumps(req.candidates, ensure_ascii=False),
                req.recommendation,
                req.total_sources,
                req.cost_usd,
                req.model_used,
            )
        finally:
            await conn.close()

        report_id = row["id"]
        created_at = row["created_at"]
        logger.info("strategy_report_created", id=report_id, direction=req.direction)
        return {
            "ok": True,
            "id": report_id,
            "direction": req.direction,
            "created_at": created_at.isoformat() if created_at else None,
        }
    except Exception as e:
        logger.error("strategy_report_create_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategy-reports")
async def list_strategy_reports(
    project_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """프로젝트별 전략 보고서 목록."""
    try:
        conn = await _get_conn()
        try:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, direction, recommendation,
                           total_sources, cost_usd, model_used, created_at
                    FROM strategy_reports
                    WHERE project_id = $1::uuid
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    project_id, limit, offset,
                )
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM strategy_reports WHERE project_id = $1::uuid",
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, direction, recommendation,
                           total_sources, cost_usd, model_used, created_at
                    FROM strategy_reports
                    ORDER BY created_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
                total = await conn.fetchval("SELECT COUNT(*) FROM strategy_reports")
        finally:
            await conn.close()

        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "project_id": str(r["project_id"]) if r["project_id"] else None,
                "direction": r["direction"],
                "recommendation": r["recommendation"],
                "total_sources": r["total_sources"],
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] else 0.0,
                "model_used": r["model_used"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })

        return {"ok": True, "items": items, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        logger.error("strategy_report_list_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategy-reports/{report_id}")
async def get_strategy_report(report_id: int):
    """단건 전략 보고서 조회."""
    try:
        conn = await _get_conn()
        try:
            row = await conn.fetchrow(
                """
                SELECT id, project_id, direction, strategy_report, candidates,
                       recommendation, total_sources, cost_usd, model_used, created_at
                FROM strategy_reports
                WHERE id = $1
                """,
                report_id,
            )
        finally:
            await conn.close()

        if not row:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        return {
            "ok": True,
            "id": row["id"],
            "project_id": str(row["project_id"]) if row["project_id"] else None,
            "direction": row["direction"],
            "strategy_report": json.loads(row["strategy_report"]) if row["strategy_report"] else {},
            "candidates": json.loads(row["candidates"]) if row["candidates"] else [],
            "recommendation": row["recommendation"],
            "total_sources": row["total_sources"],
            "cost_usd": float(row["cost_usd"]) if row["cost_usd"] else 0.0,
            "model_used": row["model_used"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("strategy_report_get_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategy-reports/{report_id}/candidates")
async def get_strategy_candidates(report_id: int):
    """전략 보고서의 후보 아이템만 조회."""
    try:
        conn = await _get_conn()
        try:
            row = await conn.fetchrow(
                "SELECT id, direction, candidates FROM strategy_reports WHERE id = $1",
                report_id,
            )
        finally:
            await conn.close()

        if not row:
            raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

        candidates = json.loads(row["candidates"]) if row["candidates"] else []
        return {
            "ok": True,
            "report_id": row["id"],
            "direction": row["direction"],
            "candidates": candidates,
            "count": len(candidates),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("strategy_candidates_get_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
