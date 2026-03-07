"""
AADS Managers API — 매니저 에이전트 목록 조회
AADS-146: /api/v1/managers 엔드포인트

엔드포인트:
  GET /api/v1/managers   — 매니저 에이전트 목록 (system_memory category: agents)
"""
from fastapi import APIRouter, HTTPException
from typing import Any, Dict, List, Optional
import json
import asyncpg
import os
from datetime import datetime, timezone, timedelta

from app.config import Settings

KST = timezone(timedelta(hours=9))
router = APIRouter()
_settings = Settings()


async def _get_conn():
    db_url = _settings.DATABASE_URL or os.getenv("DATABASE_URL", "")
    if not db_url:
        raise HTTPException(503, "DATABASE_URL not configured")
    return await asyncpg.connect(db_url)


@router.get("/managers")
async def get_managers() -> Dict[str, Any]:
    """
    매니저 에이전트 목록 반환.
    system_memory에서 category=agents 레코드를 읽어 반환.
    key 패턴: *_MGR → project_managers, 그 외 → core_agents
    """
    conn = None
    try:
        conn = await _get_conn()

        # agents 카테고리 전체 조회
        rows = await conn.fetch(
            """
            SELECT key, value, updated_at
            FROM system_memory
            WHERE category = 'agents'
            ORDER BY updated_at DESC NULLS LAST
            """,
        )

        project_managers: List[Dict[str, Any]] = []
        core_agents: List[Dict[str, Any]] = []

        for row in rows:
            key = row["key"]
            raw_value = row["value"]
            updated_at = row["updated_at"]

            try:
                value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            except Exception:
                value = {}

            entry = {
                "key": key,
                "updated_at": updated_at.isoformat() if updated_at else None,
                **({k: v for k, v in value.items()} if isinstance(value, dict) else {}),
            }

            if str(key).endswith("_MGR"):
                project_managers.append(entry)
            else:
                core_agents.append(entry)

        # importance 내림차순 정렬
        project_managers.sort(key=lambda x: x.get("importance", 0), reverse=True)

        return {
            "status": "ok",
            "timestamp": datetime.now(KST).isoformat(),
            "total": len(project_managers) + len(core_agents),
            "project_managers": project_managers,
            "core_agents": core_agents,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"managers 조회 실패: {e}") from e
    finally:
        if conn:
            await conn.close()


@router.get("/managers/{agent_id}")
async def get_manager(agent_id: str) -> Dict[str, Any]:
    """특정 매니저 에이전트 상세 조회."""
    conn = None
    try:
        conn = await _get_conn()
        row = await conn.fetchrow(
            "SELECT key, value, updated_at FROM system_memory WHERE category='agents' AND key=$1",
            agent_id,
        )
        if not row:
            raise HTTPException(404, f"매니저 없음: {agent_id}")

        try:
            value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        except Exception:
            value = {}

        return {
            "status": "ok",
            "agent_id": agent_id,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            **({k: v for k, v in value.items()} if isinstance(value, dict) else {}),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"매니저 조회 실패: {e}") from e
    finally:
        if conn:
            await conn.close()
