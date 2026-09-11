"""
AADS LLM 폴백 엔진 — DB 기반 폴백 체인 관리.

llm_fallback_chains 테이블에서 route_key별 폴백 순서를 조회하고,
쿨다운/활성 상태를 반영하여 실행 순서를 반환한다.

사용:
    from app.core.llm_fallback_engine import get_fallback_steps, FallbackStep
    steps = await get_fallback_steps("background")
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60
_cache: dict[str, tuple[list[dict], float]] = {}


@dataclass
class FallbackStep:
    step_order: int
    provider: str
    model_id: str
    backend: str
    is_free: bool
    max_retries: int
    timeout_seconds: int
    conditions: dict = field(default_factory=dict)


def invalidate_fallback_cache(route_key: str | None = None) -> None:
    if route_key:
        _cache.pop(route_key, None)
    else:
        _cache.clear()
    logger.info("fallback_cache_invalidated: route_key=%s", route_key or "all")


async def _load_chain_from_db(route_key: str) -> list[dict]:
    now = time.time()
    cached = _cache.get(route_key)
    if cached and cached[1] > now:
        return cached[0]

    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT step_order, provider, model_id, backend, is_free,
                       max_retries, timeout_seconds, conditions
                FROM llm_fallback_chains
                WHERE route_key = $1 AND is_enabled = TRUE
                ORDER BY step_order ASC
                """,
                route_key,
            )
        result = [dict(r) for r in rows]
        _cache[route_key] = (result, now + _CACHE_TTL_SECONDS)
        return result
    except Exception as e:
        logger.warning("fallback_chain_db_load_failed: route_key=%s error=%s", route_key, str(e)[:120])
        cached_stale = _cache.get(route_key)
        if cached_stale:
            return cached_stale[0]
        return []


async def get_fallback_steps(route_key: str) -> list[FallbackStep]:
    rows = await _load_chain_from_db(route_key)
    steps = []
    for row in rows:
        conditions = row.get("conditions") or {}
        if isinstance(conditions, str):
            import json
            try:
                conditions = json.loads(conditions)
            except Exception:
                conditions = {}
        steps.append(FallbackStep(
            step_order=row["step_order"],
            provider=row["provider"],
            model_id=row["model_id"],
            backend=row["backend"],
            is_free=row.get("is_free", False),
            max_retries=row.get("max_retries", 3),
            timeout_seconds=row.get("timeout_seconds", 120),
            conditions=conditions,
        ))
    return steps


async def get_bg_fallback_models() -> list[str]:
    """백그라운드 LLM용 무료 폴백 모델 목록 반환 (DB 우선, 환경변수 폴백)."""
    steps = await get_fallback_steps("background")
    free_models = [s.model_id for s in steps if s.is_free and s.model_id != "user_selected"]
    if free_models:
        return free_models

    import os
    env_models = os.getenv("LLM_BG_FALLBACK_MODELS", "groq-llama-70b,groq-gpt-oss-120b")
    return [m.strip() for m in env_models.split(",") if m.strip()]


async def update_chain_step(
    route_key: str,
    step_order: int,
    *,
    is_enabled: Optional[bool] = None,
    model_id: Optional[str] = None,
    max_retries: Optional[int] = None,
) -> bool:
    """Admin API용: 폴백 체인 단일 스텝 업데이트."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        sets = []
        params = [route_key, step_order]
        idx = 3
        if is_enabled is not None:
            sets.append(f"is_enabled = ${idx}")
            params.append(is_enabled)
            idx += 1
        if model_id is not None:
            sets.append(f"model_id = ${idx}")
            params.append(model_id)
            idx += 1
        if max_retries is not None:
            sets.append(f"max_retries = ${idx}")
            params.append(max_retries)
            idx += 1
        if not sets:
            return False
        sets.append("updated_at = NOW()")
        sql = f"UPDATE llm_fallback_chains SET {', '.join(sets)} WHERE route_key = $1 AND step_order = $2"
        async with pool.acquire() as conn:
            await conn.execute(sql, *params)
        invalidate_fallback_cache(route_key)
        return True
    except Exception as e:
        logger.error("fallback_chain_update_failed: %s", str(e)[:120])
        return False


async def reorder_chain(route_key: str, new_order: list[int]) -> bool:
    """Admin API용: 폴백 체인 순서 재배치."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for new_step, old_step in enumerate(new_order, start=1):
                    await conn.execute(
                        """
                        UPDATE llm_fallback_chains
                        SET step_order = -$3, updated_at = NOW()
                        WHERE route_key = $1 AND step_order = $2
                        """,
                        route_key, old_step, new_step,
                    )
                await conn.execute(
                    """
                    UPDATE llm_fallback_chains
                    SET step_order = -step_order
                    WHERE route_key = $1 AND step_order < 0
                    """,
                    route_key,
                )
        invalidate_fallback_cache(route_key)
        return True
    except Exception as e:
        logger.error("fallback_chain_reorder_failed: %s", str(e)[:120])
        return False


async def list_chains() -> dict[str, list[dict]]:
    """Admin API용: 전체 폴백 체인 목록."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT route_key, step_order, provider, model_id, backend,
                       is_free, is_enabled, max_retries, timeout_seconds, conditions
                FROM llm_fallback_chains
                ORDER BY route_key, step_order
                """
            )
        result: dict[str, list[dict]] = {}
        for row in rows:
            rk = row["route_key"]
            if rk not in result:
                result[rk] = []
            result[rk].append(dict(row))
        return result
    except Exception as e:
        logger.error("fallback_chain_list_failed: %s", str(e)[:120])
        return {}
