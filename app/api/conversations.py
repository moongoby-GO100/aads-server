"""
AADS Conversations API - 대화창 저장 내용 조회
데이터 소스: system_memory 테이블의 conversation:* 카테고리
"""
from fastapi import APIRouter, Query
from typing import Optional
import json
from app.memory.store import memory_store

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(
    project: Optional[str] = Query(None, description="프로젝트 필터: aads, kis, sf, sales, nas, ntv2, go100"),
    keyword: Optional[str] = Query(None, description="키워드 검색"),
    limit: int = Query(50, le=200),
    offset: int = Query(0)
):
    """
    저장된 대화 내용 조회.
    데이터 소스: system_memory 테이블의 conversation:* 카테고리
    """
    async with memory_store.pool.acquire() as conn:
        base_query = "SELECT key, value, category, importance, updated_at FROM system_memory WHERE category LIKE 'conversation:%'"
        params = []
        idx = 1

        if project:
            base_query += f" AND category = ${idx}"
            params.append(f"conversation:{project}")
            idx += 1

        if keyword:
            base_query += f" AND value::text ILIKE ${idx}"
            params.append(f"%{keyword}%")
            idx += 1

        base_query += f" ORDER BY updated_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
        params.extend([limit, offset])

        rows = await conn.fetch(base_query, *params)

        # 총 건수
        count_query = "SELECT COUNT(*) FROM system_memory WHERE category LIKE 'conversation:%'"
        count_params = []
        cidx = 1
        if project:
            count_query += f" AND category = ${cidx}"
            count_params.append(f"conversation:{project}")
            cidx += 1
        if keyword:
            count_query += f" AND value::text ILIKE ${cidx}"
            count_params.append(f"%{keyword}%")

        total = await conn.fetchval(count_query, *count_params)

        conversations = []
        for row in rows:
            raw = row["value"]
            val = raw if isinstance(raw, dict) else json.loads(raw)
            conversations.append({
                "id": row["key"],
                "project": row["category"].replace("conversation:", ""),
                "source": val.get("source", "unknown"),
                "snapshot": val.get("snapshot", "")[:500],  # 미리보기 500자
                "full_text": val.get("snapshot", ""),
                "logged_at": val.get("logged_at", ""),
                "char_count": val.get("char_count", 0),
                "updated_at": str(row["updated_at"])
            })

        return {
            "status": "ok",
            "total": total,
            "limit": limit,
            "offset": offset,
            "conversations": conversations
        }


@router.get("/stats")
async def conversation_stats():
    """프로젝트별 대화 건수 통계"""
    async with memory_store.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT category, COUNT(*) as count,
                   MAX(updated_at) as last_updated
            FROM system_memory
            WHERE category LIKE 'conversation:%'
            GROUP BY category
            ORDER BY count DESC
        """)

        stats = []
        total = 0
        for row in rows:
            count = row["count"]
            total += count
            stats.append({
                "project": row["category"].replace("conversation:", ""),
                "count": count,
                "last_updated": str(row["last_updated"])
            })

        return {
            "status": "ok",
            "total_conversations": total,
            "projects": stats
        }
