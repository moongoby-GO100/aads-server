"""
AADS 메모리 진화 모니터링 API
- GET /ops/memory/stats: 메모리 통계 (observations, session_notes, meta_memory)
- GET /ops/memory/entries: 메모리 항목 목록 (페이지네이션, 필터)
- DELETE /ops/memory/entries/{source}/{id}: 메모리 항목 삭제 (아카이브 백업)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/ops/memory/stats")
async def get_memory_stats():
    """메모리 진화 시스템 전체 통계 조회."""
    pool = get_pool()
    async with pool.acquire() as conn:
        # 기본 카운트 + 평균 confidence
        row = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM ai_observations) AS total_observations,
                (SELECT COUNT(*) FROM session_notes) AS total_session_notes,
                (SELECT COUNT(*) FROM ai_meta_memory WHERE category != 'semantic_cache') AS total_meta_memory,
                (SELECT COALESCE(AVG(confidence), 0) FROM ai_observations) AS avg_confidence
        """)

        # 오늘 학습된 observation 수
        today_row = await conn.fetchrow("""
            SELECT COUNT(*) AS cnt FROM ai_observations
            WHERE updated_at >= CURRENT_DATE
               OR created_at >= CURRENT_DATE
        """)

        # 카테고리별 분포
        categories = await conn.fetch("""
            SELECT
                category AS name,
                COUNT(*) AS count,
                COALESCE(AVG(confidence), 0) AS avg_confidence,
                MAX(COALESCE(updated_at, created_at))::text AS last_updated
            FROM ai_observations
            GROUP BY category
            ORDER BY count DESC
        """)

        # 최근 14일 일별 학습 추이
        daily_trend = await conn.fetch("""
            SELECT
                d::date::text AS date,
                COUNT(ao.id) AS count
            FROM generate_series(
                CURRENT_DATE - INTERVAL '13 days',
                CURRENT_DATE,
                '1 day'
            ) AS d
            LEFT JOIN ai_observations ao
                ON ao.created_at::date = d::date
            GROUP BY d
            ORDER BY d
        """)

        # 품질 등급 분포
        quality_row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE confidence >= 0.7) AS high,
                COUNT(*) FILTER (WHERE confidence >= 0.4 AND confidence < 0.7) AS medium,
                COUNT(*) FILTER (WHERE confidence < 0.4) AS low
            FROM ai_observations
        """)

        # 프로젝트별 분포
        projects = await conn.fetch("""
            SELECT
                COALESCE(project, 'common') AS project,
                COUNT(*) AS count
            FROM ai_observations
            GROUP BY project
            ORDER BY count DESC
        """)

    return {
        "total_observations": row["total_observations"],
        "total_session_notes": row["total_session_notes"],
        "total_meta_memory": row["total_meta_memory"],
        "avg_confidence": round(float(row["avg_confidence"]), 3),
        "today_learned": today_row["cnt"],
        "categories": [
            {
                "name": c["name"],
                "count": c["count"],
                "avg_confidence": round(float(c["avg_confidence"]), 3),
                "last_updated": c["last_updated"],
            }
            for c in categories
        ],
        "daily_trend": [
            {"date": d["date"], "count": d["count"]}
            for d in daily_trend
        ],
        "quality_distribution": {
            "high": quality_row["high"],
            "medium": quality_row["medium"],
            "low": quality_row["low"],
        },
        "projects": [
            {"project": p["project"], "count": p["count"]}
            for p in projects
        ],
    }


@router.get("/ops/memory/entries")
async def get_memory_entries(
    category: Optional[str] = Query(None, description="카테고리 필터"),
    project: Optional[str] = Query(None, description="프로젝트 필터"),
    search: Optional[str] = Query(None, description="키/값 검색어"),
    page: int = Query(1, ge=1, description="페이지 번호"),
    per_page: int = Query(20, ge=1, le=100, description="페이지당 항목 수"),
):
    """ai_observations + ai_meta_memory 통합 조회 (페이지네이션)."""
    pool = get_pool()
    offset = (page - 1) * per_page

    # 동적 WHERE 절 구성
    obs_conditions = []
    meta_conditions = []
    obs_params = []
    meta_params = []
    obs_idx = 1
    meta_idx = 1

    if category:
        obs_conditions.append(f"category = ${obs_idx}")
        obs_params.append(category)
        obs_idx += 1
        meta_conditions.append(f"category = ${meta_idx}")
        meta_params.append(category)
        meta_idx += 1

    if project:
        obs_conditions.append(f"project = ${obs_idx}")
        obs_params.append(project)
        obs_idx += 1
        # ai_meta_memory에는 project 컬럼이 없을 수 있으므로 observations만 필터

    if search:
        obs_conditions.append(f"(key ILIKE ${obs_idx} OR value::text ILIKE ${obs_idx})")
        obs_params.append(f"%{search}%")
        obs_idx += 1
        meta_conditions.append(f"(key ILIKE ${meta_idx} OR value::text ILIKE ${meta_idx})")
        meta_params.append(f"%{search}%")
        meta_idx += 1

    # semantic_cache 항상 제외
    meta_conditions.append(f"category != 'semantic_cache'")

    obs_where = ("WHERE " + " AND ".join(obs_conditions)) if obs_conditions else ""
    meta_where = ("WHERE " + " AND ".join(meta_conditions)) if meta_conditions else ""

    async with pool.acquire() as conn:
        # UNION ALL로 통합 쿼리
        count_sql = f"""
            SELECT COUNT(*) AS total FROM (
                SELECT id FROM ai_observations {obs_where}
                UNION ALL
                SELECT id FROM ai_meta_memory {meta_where}
            ) sub
        """
        # 파라미터 합치기
        count_params = obs_params + meta_params
        total_row = await conn.fetchrow(count_sql, *count_params)
        total = total_row["total"]

        data_sql = f"""
            SELECT * FROM (
                SELECT
                    id,
                    'observations' AS source,
                    category,
                    key,
                    value::text AS value,
                    confidence,
                    project,
                    created_at::text AS created_at,
                    COALESCE(updated_at, created_at)::text AS updated_at
                FROM ai_observations {obs_where}
                UNION ALL
                SELECT
                    id,
                    'meta_memory' AS source,
                    category,
                    key,
                    value::text AS value,
                    NULL::float AS confidence,
                    NULL::text AS project,
                    created_at::text AS created_at,
                    COALESCE(updated_at, created_at)::text AS updated_at
                FROM ai_meta_memory {meta_where}
            ) combined
            ORDER BY updated_at DESC
            LIMIT ${len(count_params) + 1} OFFSET ${len(count_params) + 2}
        """
        data_params = count_params + [per_page, offset]
        items = await conn.fetch(data_sql, *data_params)

    return {
        "items": [
            {
                "id": item["id"],
                "source": item["source"],
                "category": item["category"],
                "key": item["key"],
                "value": item["value"],
                "confidence": item["confidence"],
                "project": item["project"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
            for item in items
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.post("/ops/memory/deduplicate")
async def deduplicate_memory():
    """메모리 중복 제거 실행 (CEO 수동 트리거용).

    같은 category+key+project 그룹에서 confidence 최대값만 유지,
    나머지는 memory_archive에 백업 후 삭제.
    """
    from app.core.memory_recall import deduplicate_observations
    result = await deduplicate_observations()
    logger.info("memory_deduplicate_triggered", result=result)
    return result


@router.delete("/ops/memory/entries/{source}/{entry_id}")
async def delete_memory_entry(source: str, entry_id: int):
    """메모리 항목 삭제 (삭제 전 memory_archive에 백업)."""
    if source not in ("observations", "meta_memory"):
        raise HTTPException(status_code=400, detail="source는 'observations' 또는 'meta_memory'만 허용")

    table = "ai_observations" if source == "observations" else "ai_meta_memory"
    pool = get_pool()

    async with pool.acquire() as conn:
        # 삭제 대상 조회
        row = await conn.fetchrow(f"SELECT * FROM {table} WHERE id = $1", entry_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"{table}에서 id={entry_id} 항목을 찾을 수 없음")

        # memory_archive에 백업
        await conn.execute("""
            INSERT INTO memory_archive (source_table, source_id, category, key, value, confidence, project, original_created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
            table,
            entry_id,
            row.get("category"),
            row.get("key"),
            str(row.get("value", "")),
            float(row["confidence"]) if row.get("confidence") is not None else None,
            row.get("project"),
            row.get("created_at"),
        )

        # 원본 삭제
        await conn.execute(f"DELETE FROM {table} WHERE id = $1", entry_id)

    logger.info("memory_entry_deleted", source=source, entry_id=entry_id)
    return {"status": "deleted", "source": source, "id": entry_id, "archived": True}


@router.get("/ops/memory/learning-health")
async def get_learning_health():
    """학습 헬스 상태 조회 — 대시보드 표시용.
    24시간 기준으로 대화량 vs 학습량 비교."""
    from app.core.memory_recall import check_learning_health
    return await check_learning_health(hours=24)
