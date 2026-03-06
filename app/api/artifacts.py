"""
AADS-128: 프로젝트 산출물 통합 API.

POST /api/v1/artifacts           — 산출물 저장 (에이전트 자동 호출)
GET  /api/v1/artifacts           — 유형별 조회 (?project_id=&type=)
GET  /api/v1/artifacts/{id}      — 단건 조회

artifact_type: strategy_report | prd | architecture | phase_plan | taskspec
               | code | test_result | deployment
"""
import json
import os
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()
logger = structlog.get_logger()


# ─── Pydantic 모델 ────────────────────────────────────────────────────────────

class CreateArtifactRequest(BaseModel):
    project_id: str
    artifact_type: str
    artifact_name: str
    content: dict
    source_agent: Optional[str] = None
    source_task: Optional[str] = None
    version: int = 1


class ArtifactResponse(BaseModel):
    id: int
    project_id: str
    artifact_type: str
    artifact_name: str
    content: dict
    source_agent: Optional[str]
    source_task: Optional[str]
    version: int
    created_at: str


# ─── DB 연결 헬퍼 ─────────────────────────────────────────────────────────────

def _db_url() -> str:
    return os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")


# ─── 엔드포인트 ───────────────────────────────────────────────────────────────

@router.post("/artifacts", response_model=ArtifactResponse, status_code=201,
             summary="산출물 저장", tags=["artifacts"])
async def create_artifact(req: CreateArtifactRequest):
    """에이전트 산출물을 project_artifacts 테이블에 저장."""
    db_url = _db_url()
    if not db_url:
        raise HTTPException(503, "Database not configured")

    try:
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO project_artifacts
                    (project_id, artifact_type, artifact_name, content,
                     source_agent, source_task, version)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                RETURNING id, project_id, artifact_type, artifact_name,
                          content::text, source_agent, source_task, version,
                          created_at::text
                """,
                req.project_id,
                req.artifact_type,
                req.artifact_name,
                json.dumps(req.content, ensure_ascii=False),
                req.source_agent,
                req.source_task,
                req.version,
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.error("create_artifact_failed", error=str(e))
        raise HTTPException(500, f"DB error: {e}")

    return ArtifactResponse(
        id=row["id"],
        project_id=row["project_id"],
        artifact_type=row["artifact_type"],
        artifact_name=row["artifact_name"],
        content=json.loads(row["content"]),
        source_agent=row["source_agent"],
        source_task=row["source_task"],
        version=row["version"],
        created_at=row["created_at"],
    )


@router.get("/artifacts", summary="산출물 목록 조회", tags=["artifacts"])
async def list_artifacts(
    project_id: Optional[str] = Query(None, description="프로젝트 UUID"),
    type: Optional[str] = Query(None, description="artifact_type 필터"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """project_id 및/또는 type으로 산출물 목록 조회."""
    db_url = _db_url()
    if not db_url:
        raise HTTPException(503, "Database not configured")

    conditions = []
    params: list = []
    idx = 1

    if project_id:
        conditions.append(f"project_id = ${idx}")
        params.append(project_id)
        idx += 1
    if type:
        conditions.append(f"artifact_type = ${idx}")
        params.append(type)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])

    try:
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            rows = await conn.fetch(
                f"""
                SELECT id, project_id, artifact_type, artifact_name,
                       content::text, source_agent, source_task, version, created_at::text
                FROM project_artifacts
                {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *params,
            )
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM project_artifacts {where}",
                *params[:-2],
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.error("list_artifacts_failed", error=str(e))
        raise HTTPException(500, f"DB error: {e}")

    return {
        "artifacts": [
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "artifact_type": r["artifact_type"],
                "artifact_name": r["artifact_name"],
                "content": json.loads(r["content"]),
                "source_agent": r["source_agent"],
                "source_task": r["source_task"],
                "version": r["version"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse,
            summary="산출물 단건 조회", tags=["artifacts"])
async def get_artifact(artifact_id: int):
    """artifact_id로 단건 산출물 조회."""
    db_url = _db_url()
    if not db_url:
        raise HTTPException(503, "Database not configured")

    try:
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = await conn.fetchrow(
                """
                SELECT id, project_id, artifact_type, artifact_name,
                       content::text, source_agent, source_task, version, created_at::text
                FROM project_artifacts
                WHERE id = $1
                """,
                artifact_id,
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.error("get_artifact_failed", error=str(e))
        raise HTTPException(500, f"DB error: {e}")

    if not row:
        raise HTTPException(404, f"Artifact {artifact_id} not found")

    return ArtifactResponse(
        id=row["id"],
        project_id=row["project_id"],
        artifact_type=row["artifact_type"],
        artifact_name=row["artifact_name"],
        content=json.loads(row["content"]),
        source_agent=row["source_agent"],
        source_task=row["source_task"],
        version=row["version"],
        created_at=row["created_at"],
    )
