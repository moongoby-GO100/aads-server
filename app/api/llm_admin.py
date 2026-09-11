"""
AADS LLM Admin API — 폴백 체인, 키 관리, 인텐트 정책 Admin 전용 엔드포인트.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-admin", tags=["llm-admin"])


class ChainStepUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    model_id: Optional[str] = None
    max_retries: Optional[int] = None


class ChainReorderRequest(BaseModel):
    route_key: str
    new_order: list[int]


class KeyTestRequest(BaseModel):
    key_name: str


# ── 폴백 체인 관리 ──────────────────────────────────────────────────

@router.get("/chains")
async def get_chains():
    from app.core.llm_fallback_engine import list_chains
    chains = await list_chains()
    return {"chains": chains, "route_keys": list(chains.keys())}


@router.patch("/chains/{route_key}/{step_order}")
async def update_chain_step(route_key: str, step_order: int, body: ChainStepUpdate):
    from app.core.llm_fallback_engine import update_chain_step as _update
    ok = await _update(
        route_key, step_order,
        is_enabled=body.is_enabled,
        model_id=body.model_id,
        max_retries=body.max_retries,
    )
    if not ok:
        raise HTTPException(status_code=400, detail="update failed")
    return {"ok": True}


@router.post("/chains/reorder")
async def reorder_chain(body: ChainReorderRequest):
    from app.core.llm_fallback_engine import reorder_chain as _reorder
    ok = await _reorder(body.route_key, body.new_order)
    if not ok:
        raise HTTPException(status_code=400, detail="reorder failed")
    return {"ok": True}


@router.post("/chains/invalidate-cache")
async def invalidate_chain_cache():
    from app.core.llm_fallback_engine import invalidate_fallback_cache
    invalidate_fallback_cache()
    return {"ok": True, "message": "fallback chain cache invalidated"}


# ── 키 관리 ────────────────────────────────────────────────────────

@router.get("/keys")
async def get_keys():
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key_name, provider, label, priority, is_active,
                   rate_limited_until, last_used_at, last_verified_at, notes
            FROM llm_api_keys
            ORDER BY provider, priority
            """
        )
    keys = []
    for row in rows:
        r = dict(row)
        r["rate_limited_until"] = str(r["rate_limited_until"]) if r["rate_limited_until"] else None
        r["last_used_at"] = str(r["last_used_at"]) if r["last_used_at"] else None
        r["last_verified_at"] = str(r["last_verified_at"]) if r["last_verified_at"] else None
        keys.append(r)
    return {"keys": keys}


@router.patch("/keys/{key_name}/priority")
async def update_key_priority(key_name: str, priority: int):
    from app.core.db_pool import get_pool
    from app.core.llm_key_provider import invalidate_key_cache
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE llm_api_keys SET priority = $2, updated_at = NOW() WHERE key_name = $1",
            key_name, priority,
        )
    invalidate_key_cache(key_name)
    return {"ok": True}


@router.post("/keys/test")
async def test_key(body: KeyTestRequest):
    """키 실제 API 호출 테스트 — 간단한 ping 요청으로 인증 상태 확인."""
    import time
    from app.core.llm_key_provider import get_api_key
    from app.core.db_pool import get_pool

    key_value = await get_api_key(body.key_name)
    if not key_value:
        return {"ok": False, "error": "key not found or empty", "response_ms": 0}

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT provider FROM llm_api_keys WHERE key_name = $1", body.key_name
    )
    provider = (row["provider"] if row else "unknown").lower()

    t0 = time.monotonic()
    status = "healthy"
    error_msg = None

    try:
        if provider == "anthropic":
            from anthropic import AsyncAnthropic
            if key_value.startswith("sk-ant-oat"):
                client = AsyncAnthropic(auth_token=key_value)
            else:
                client = AsyncAnthropic(api_key=key_value)
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4,
                messages=[{"role": "user", "content": "ping"}],
            )
            status = "healthy"
        elif provider in ("gemini", "groq", "deepseek", "qwen", "kimi", "minimax"):
            import httpx
            litellm_url = "http://aads-litellm:4000"
            async with httpx.AsyncClient(timeout=15.0) as hc:
                r = await hc.get(f"{litellm_url}/health")
                if r.status_code == 200:
                    status = "healthy"
                else:
                    status = "degraded"
                    error_msg = f"litellm health: {r.status_code}"
        else:
            status = "unknown"
            error_msg = f"no test handler for provider: {provider}"
    except Exception as e:
        status = "failed"
        error_msg = str(e)[:200]

    response_ms = int((time.monotonic() - t0) * 1000)

    try:
        await pool.execute(
            """
            INSERT INTO llm_key_health_log (key_name, check_type, status, response_ms, error_message)
            VALUES ($1, 'manual_test', $2, $3, $4)
            """,
            body.key_name, status, response_ms, error_msg,
        )
    except Exception:
        pass

    return {
        "ok": status == "healthy",
        "key_name": body.key_name,
        "provider": provider,
        "status": status,
        "response_ms": response_ms,
        "error": error_msg,
    }


# ── 키 건강 이력 ──────────────────────────────────────────────────

@router.get("/keys/health-log")
async def get_key_health_log(key_name: Optional[str] = None, limit: int = 50):
    from app.core.db_pool import get_pool
    pool = get_pool()
    if key_name:
        rows = await pool.fetch(
            """
            SELECT key_name, check_type, status, response_ms, error_message, checked_at
            FROM llm_key_health_log
            WHERE key_name = $1
            ORDER BY checked_at DESC LIMIT $2
            """,
            key_name, min(limit, 100),
        )
    else:
        rows = await pool.fetch(
            """
            SELECT key_name, check_type, status, response_ms, error_message, checked_at
            FROM llm_key_health_log
            ORDER BY checked_at DESC LIMIT $1
            """,
            min(limit, 100),
        )
    return {"logs": [dict(r) for r in rows]}


# ── 인텐트 정책 관리 ─────────────────────────────────────────────

@router.get("/intent-policies")
async def get_intent_policies():
    from app.core.db_pool import get_pool
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT intent, default_model, cascade_downgrade, allowed_models FROM intent_policies ORDER BY intent"
    )
    return {"policies": [dict(r) for r in rows]}


class IntentPolicyUpdate(BaseModel):
    default_model: Optional[str] = None
    cascade_downgrade: Optional[bool] = None
    allowed_models: Optional[list[str]] = None


@router.patch("/intent-policies/{intent}")
async def update_intent_policy(intent: str, body: IntentPolicyUpdate):
    from app.core.db_pool import get_pool
    pool = get_pool()
    sets = []
    params = [intent]
    idx = 2
    if body.default_model is not None:
        sets.append(f"default_model = ${idx}")
        params.append(body.default_model)
        idx += 1
    if body.cascade_downgrade is not None:
        sets.append(f"cascade_downgrade = ${idx}")
        params.append(body.cascade_downgrade)
        idx += 1
    if body.allowed_models is not None:
        sets.append(f"allowed_models = ${idx}")
        params.append(body.allowed_models)
        idx += 1
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    sql = f"UPDATE intent_policies SET {', '.join(sets)} WHERE intent = $1"
    async with pool.acquire() as conn:
        await conn.execute(sql, *params)

    from app.services.model_selector import invalidate_intent_policy_cache
    invalidate_intent_policy_cache()

    return {"ok": True}
