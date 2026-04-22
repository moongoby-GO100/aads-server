"""LLM API 키 관리 API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.db_pool import get_pool
from app.core.llm_key_provider import store_api_key

router = APIRouter(prefix="/llm-keys", tags=["llm-keys"])


def _mask(val: str) -> str:
    if not val:
        return ""
    return val[:8] + "****" + val[-4:] if len(val) > 12 else val[:4] + "****"


class LlmKeyCreate(BaseModel):
    key_name: str
    provider: str
    plaintext_value: str = Field(..., min_length=4)
    label: str = ""
    priority: int = Field(1, ge=1, le=99)


class LlmKeyUpdate(BaseModel):
    label: str | None = None
    priority: int | None = Field(None, ge=1, le=99)
    is_active: bool | None = None


@router.get("")
async def list_llm_keys() -> dict[str, Any]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, provider, key_name, label, priority, is_active,
               last_used_at, last_verified_at, rate_limited_until, updated_at
        FROM llm_api_keys
        ORDER BY provider, priority, id
        """
    )
    items = [dict(row) for row in rows]
    return {"keys": items, "count": len(items)}


@router.post("")
async def create_llm_key(body: LlmKeyCreate) -> dict[str, Any]:
    await store_api_key(
        key_name=body.key_name,
        plaintext_value=body.plaintext_value,
        provider=body.provider,
        label=body.label,
        priority=body.priority,
    )
    return {"status": "created", "key_name": body.key_name}


@router.put("/{key_name}")
async def update_llm_key(key_name: str, body: LlmKeyUpdate) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="수정할 필드가 없습니다")

    pool = get_pool()
    sets = ", ".join(f"{key} = ${index + 2}" for index, key in enumerate(updates))
    values = list(updates.values())
    result = await pool.fetchrow(
        f"UPDATE llm_api_keys SET {sets}, updated_at = NOW() WHERE key_name = $1 RETURNING id",
        key_name,
        *values,
    )
    if not result:
        raise HTTPException(status_code=404, detail="키를 찾을 수 없습니다")
    return {"status": "updated", "key_name": key_name}


@router.delete("/{key_name}")
async def delete_llm_key(key_name: str) -> dict[str, Any]:
    pool = get_pool()
    result = await pool.fetchrow(
        "UPDATE llm_api_keys SET is_active = FALSE, updated_at = NOW() WHERE key_name = $1 RETURNING id",
        key_name,
    )
    if not result:
        raise HTTPException(status_code=404, detail="키를 찾을 수 없습니다")
    return {"status": "deactivated", "key_name": key_name}
