"""
ai_observations TTL 기반 가비지 컬렉션.
30일 미사용 + confidence 감쇠 → 임계값 미만 자동 삭제.
ceo_preference 카테고리는 GC 대상에서 제외.
"""
from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

GC_MAX_AGE_DAYS = int(os.getenv("MEMORY_GC_MAX_AGE_DAYS", "30"))
GC_DECAY_FACTOR = float(os.getenv("MEMORY_GC_DECAY_FACTOR", "0.9"))
GC_DELETE_THRESHOLD = float(os.getenv("MEMORY_GC_DELETE_THRESHOLD", "0.1"))

# GC 제외 카테고리 (CEO 선호, 핵심 규칙 등)
_PROTECTED_CATEGORIES = (
    "ceo_preference",
    "ceo_directive",
    "compaction_directive",
)


async def gc_observations(pool) -> dict:
    """ai_observations 가비지 컬렉션.

    1단계: max_age_days 이상 미갱신 항목의 confidence 감쇠 (× decay_factor)
    2단계: confidence < delete_threshold인 오래된 항목 삭제

    Returns:
        {"decayed": int, "deleted": int}
    """
    result = {"decayed": 0, "deleted": 0}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1단계: confidence 감쇠
                decayed = await conn.execute(
                    """
                    UPDATE ai_observations
                    SET confidence = confidence * $1
                    WHERE updated_at < NOW() - make_interval(days => $2)
                      AND confidence > $3
                      AND category NOT IN (SELECT unnest($4::text[]))
                    """,
                    GC_DECAY_FACTOR,
                    GC_MAX_AGE_DAYS,
                    GC_DELETE_THRESHOLD,
                    list(_PROTECTED_CATEGORIES),
                )
                result["decayed"] = int(decayed.split()[-1]) if decayed else 0

                # 2단계: 임계값 미만 삭제
                deleted = await conn.execute(
                    """
                    DELETE FROM ai_observations
                    WHERE confidence < $1
                      AND updated_at < NOW() - make_interval(days => $2)
                      AND category NOT IN (SELECT unnest($3::text[]))
                    """,
                    GC_DELETE_THRESHOLD,
                    GC_MAX_AGE_DAYS,
                    list(_PROTECTED_CATEGORIES),
                )
                result["deleted"] = int(deleted.split()[-1]) if deleted else 0

        logger.info("memory_gc_complete", **result)
    except Exception as e:
        logger.error("memory_gc_error", error=str(e))
    return result
