"""
AADS-126: Project Plan 저장/조회 API.
POST /api/v1/project-plans
GET  /api/v1/project-plans?project_id={id}
GET  /api/v1/project-plans/{plan_id}
GET  /api/v1/project-plans/{plan_id}/prd
GET  /api/v1/project-plans/{plan_id}/architecture
PATCH /api/v1/project-plans/{plan_id}/approve
"""
import json
import structlog
from datetime import datetime, timezone
from typing import Optional, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = structlog.get_logger()
router = APIRouter()


# ─── Request / Response 스키마 ───────────────────────────────────────────────

class ProjectPlanCreate(BaseModel):
    project_id: str
    strategy_report_id: Optional[int] = None
    selected_candidate_id: str
    prd: dict
    architecture: dict
    phase_plan: list
    rejected_alternatives: list = []
    debate_rounds: int = 0
    consensus_reached: bool = False
    debate_log: list = []
    cost_usd: float = 0.0


class ProjectPlanResponse(BaseModel):
    id: int
    project_id: Optional[str]
    strategy_report_id: Optional[int]
    selected_candidate_id: str
    prd: dict
    architecture: dict
    phase_plan: list
    rejected_alternatives: list
    debate_rounds: int
    consensus_reached: bool
    debate_log: list
    cost_usd: float
    status: str
    created_at: str
    approved_at: Optional[str]


def _get_pool():
    from app.memory.store import memory_store
    return memory_store.pool


def _row_to_dict(row) -> dict:
    """asyncpg Record → dict 변환."""
    d = dict(row)
    for key in ("prd", "architecture", "phase_plan", "rejected_alternatives", "debate_log"):
        val = d.get(key)
        if isinstance(val, str):
            try:
                d[key] = json.loads(val)
            except Exception:
                pass
        elif val is None:
            d[key] = [] if key in ("phase_plan", "rejected_alternatives", "debate_log") else {}
    for ts_key in ("created_at", "approved_at"):
        val = d.get(ts_key)
        if val is not None and not isinstance(val, str):
            d[ts_key] = val.isoformat()
    if d.get("project_id") is not None:
        d["project_id"] = str(d["project_id"])
    return d


# ─── POST /api/v1/project-plans ──────────────────────────────────────────────

@router.post("/project-plans", status_code=201)
async def create_project_plan(body: ProjectPlanCreate):
    """기획서 저장."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO project_plans
                    (project_id, strategy_report_id, selected_candidate_id,
                     prd, architecture, phase_plan, rejected_alternatives,
                     debate_rounds, consensus_reached, debate_log, cost_usd)
                VALUES ($1::uuid, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb,
                        $8, $9, $10::jsonb, $11)
                RETURNING id, status, created_at
                """,
                body.project_id,
                body.strategy_report_id,
                body.selected_candidate_id,
                json.dumps(body.prd, ensure_ascii=False),
                json.dumps(body.architecture, ensure_ascii=False),
                json.dumps(body.phase_plan, ensure_ascii=False),
                json.dumps(body.rejected_alternatives, ensure_ascii=False),
                body.debate_rounds,
                body.consensus_reached,
                json.dumps(body.debate_log, ensure_ascii=False),
                body.cost_usd,
            )
        except Exception as e:
            logger.error("create_project_plan_error", error=str(e))
            raise HTTPException(status_code=500, detail=f"DB 저장 실패: {str(e)}")

    created_at = row["created_at"]
    if not isinstance(created_at, str):
        created_at = created_at.isoformat()

    logger.info("project_plan_created", plan_id=row["id"], project_id=body.project_id)
    return {
        "id": row["id"],
        "status": row["status"],
        "created_at": created_at,
        "message": "프로젝트 기획서가 저장되었습니다.",
    }


# ─── GET /api/v1/project-plans?project_id={id} ───────────────────────────────

@router.get("/project-plans")
async def list_project_plans(project_id: Optional[str] = Query(None)):
    """프로젝트별 기획서 목록 조회."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, strategy_report_id, selected_candidate_id,
                           debate_rounds, consensus_reached, cost_usd, status, created_at, approved_at
                    FROM project_plans
                    WHERE project_id = $1::uuid
                    ORDER BY created_at DESC
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, strategy_report_id, selected_candidate_id,
                           debate_rounds, consensus_reached, cost_usd, status, created_at, approved_at
                    FROM project_plans
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
        except Exception as e:
            logger.error("list_project_plans_error", error=str(e))
            raise HTTPException(status_code=500, detail=f"DB 조회 실패: {str(e)}")

    result = []
    for row in rows:
        d = dict(row)
        for ts_key in ("created_at", "approved_at"):
            val = d.get(ts_key)
            if val is not None and not isinstance(val, str):
                d[ts_key] = val.isoformat()
        if d.get("project_id") is not None:
            d["project_id"] = str(d["project_id"])
        result.append(d)

    return {"items": result, "total": len(result)}


# ─── GET /api/v1/project-plans/{plan_id} ─────────────────────────────────────

@router.get("/project-plans/{plan_id}")
async def get_project_plan(plan_id: int):
    """단건 조회 (PRD + 아키텍처 + Phase 전체)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT * FROM project_plans WHERE id = $1",
                plan_id,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB 조회 실패: {str(e)}")

    if not row:
        raise HTTPException(status_code=404, detail=f"project_plan id={plan_id} 없음")

    return _row_to_dict(row)


# ─── GET /api/v1/project-plans/{plan_id}/prd ─────────────────────────────────

@router.get("/project-plans/{plan_id}/prd")
async def get_project_plan_prd(plan_id: int):
    """PRD만 조회."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT id, prd FROM project_plans WHERE id = $1",
                plan_id,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB 조회 실패: {str(e)}")

    if not row:
        raise HTTPException(status_code=404, detail=f"project_plan id={plan_id} 없음")

    prd = row["prd"]
    if isinstance(prd, str):
        try:
            prd = json.loads(prd)
        except Exception:
            pass

    return {"plan_id": plan_id, "prd": prd}


# ─── GET /api/v1/project-plans/{plan_id}/architecture ────────────────────────

@router.get("/project-plans/{plan_id}/architecture")
async def get_project_plan_architecture(plan_id: int):
    """아키텍처만 조회."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT id, architecture FROM project_plans WHERE id = $1",
                plan_id,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB 조회 실패: {str(e)}")

    if not row:
        raise HTTPException(status_code=404, detail=f"project_plan id={plan_id} 없음")

    arch = row["architecture"]
    if isinstance(arch, str):
        try:
            arch = json.loads(arch)
        except Exception:
            pass

    return {"plan_id": plan_id, "architecture": arch}


# ─── PATCH /api/v1/project-plans/{plan_id}/approve ───────────────────────────

@router.patch("/project-plans/{plan_id}/approve")
async def approve_project_plan(plan_id: int):
    """CEO 승인 처리."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                UPDATE project_plans
                SET status = 'approved', approved_at = $1
                WHERE id = $2
                RETURNING id, status, approved_at
                """,
                now,
                plan_id,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DB 업데이트 실패: {str(e)}")

    if not row:
        raise HTTPException(status_code=404, detail=f"project_plan id={plan_id} 없음")

    approved_at = row["approved_at"]
    if not isinstance(approved_at, str):
        approved_at = approved_at.isoformat()

    logger.info("project_plan_approved", plan_id=plan_id)
    return {
        "id": row["id"],
        "status": row["status"],
        "approved_at": approved_at,
        "message": "프로젝트 기획서가 승인되었습니다.",
    }
