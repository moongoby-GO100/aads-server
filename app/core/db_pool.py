"""
AADS DB Connection Pool — 중앙 관리 모듈
asyncpg.create_pool()로 커넥션 풀을 공유하여 per-call connect() 제거.
모든 모듈(chat_service, memory_recall, compaction_service, context_builder)에서
get_pool() → pool.acquire()로 사용.
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg
import structlog

logger = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "5"))
_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "20"))


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else url


async def init_pool() -> asyncpg.Pool:
    """앱 시작 시 호출 — 커넥션 풀 생성."""
    global _pool
    if _pool is not None:
        return _pool
    dsn = _db_url()
    if not dsn:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다")
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        timeout=10,
        command_timeout=30,  # #10: API 타임아웃(100s)보다 짧게, statement 단위 30s
    )
    logger.info(
        "db_pool_initialized",
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        dsn_host=dsn.split("@")[-1].split("/")[0] if "@" in dsn else "unknown",
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    """풀 인스턴스 반환. init_pool() 호출 전이면 RuntimeError."""
    if _pool is None:
        raise RuntimeError("DB pool이 초기화되지 않았습니다. init_pool()을 먼저 호출하세요.")
    return _pool


def get_pool_stats() -> dict:
    """풀 사용률 통계 반환. P4: 커넥션 풀 고갈 사전 경고용."""
    if _pool is None:
        return {"available": False}
    size = _pool.get_size()
    free = _pool.get_idle_size()
    used = size - free
    max_size = _pool.get_max_size()
    usage_pct = (used / max_size * 100) if max_size > 0 else 0
    return {
        "available": True,
        "size": size,
        "free": free,
        "used": used,
        "max_size": max_size,
        "usage_pct": round(usage_pct, 1),
    }


async def execute_with_retry(query: str, *args, max_retries: int = 3):
    """R4: 데드락/직렬화 실패 시 자동 재시도 (최대 3회)."""
    import asyncio as _aio
    pool = get_pool()
    for attempt in range(max_retries):
        try:
            async with pool.acquire() as conn:
                return await conn.execute(query, *args)
        except asyncpg.DeadlockDetectedError as e:
            logger.warning("db_deadlock_retry", attempt=attempt + 1, max=max_retries, error=str(e)[:100])
            if attempt == max_retries - 1:
                raise
            await _aio.sleep(0.1 * (2 ** attempt))
        except asyncpg.SerializationError as e:
            logger.warning("db_serialization_retry", attempt=attempt + 1, error=str(e)[:100])
            if attempt == max_retries - 1:
                raise
            await _aio.sleep(0.1 * (2 ** attempt))


async def close_pool() -> None:
    """앱 종료 시 호출 — 풀 정리."""
    global _pool
    if _pool is not None:
        await _pool.close()
        logger.info("db_pool_closed")
        _pool = None
