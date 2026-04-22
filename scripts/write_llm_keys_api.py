"""AADS-188: llm_keys.py 올바른 버전으로 교체."""
import shutil
from pathlib import Path

TARGET = Path("/root/aads/aads-server/app/api/llm_keys.py")
shutil.copy(TARGET, str(TARGET) + ".bak_aads188")

CONTENT = '''"""LLM API 키 관리 API — llm_api_keys 테이블 CRUD."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.credential_vault import decrypt_value, encrypt_value
from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-keys", tags=["llm-keys"])


class LlmKeyCreate(BaseModel):
    provider: str
    key_name: str
    value: str
    label: str = ""
    priority: int = 1
    notes: str = ""


class LlmKeyUpdate(BaseModel):
    value: str | None = None
    label: str | None = None
    priority: int | None = None
    is_active: bool | None = None
    notes: str | None = None


def _mask(val: str) -> str:
    if len(val) <= 8:
        return "****"
    return val[:4] + "****" + val[-4:]


@router.get("")
async def list_llm_keys() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, provider, key_name, encrypted_value, label, priority,
                      is_active, rate_limited_until, last_used_at, last_verified_at,
                      created_at, notes
               FROM llm_api_keys ORDER BY provider, priority"""
        )
    result = []
    for r in rows:
        try:
            plain = decrypt_value(r["encrypted_value"])
            masked = _mask(plain)
        except Exception:
            masked = "****"
        result.append({
            "id": r["id"],
            "provider": r["provider"],
            "key_name": r["key_name"],
            "masked_value": masked,
            "label": r["label"],
            "priority": r["priority"],
            "is_active": r["is_active"],
            "rate_limited_until": r["rate_limited_until"].isoformat() if r["rate_limited_until"] else None,
            "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            "last_verified_at": r["last_verified_at"].isoformat() if r["last_verified_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "notes": r["notes"],
        })
    return result


@router.post("")
async def create_llm_key(body: LlmKeyCreate) -> dict[str, Any]:
    encrypted = encrypt_value(body.value)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO llm_api_keys (provider, key_name, encrypted_value, label, priority, notes)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id, provider, key_name, label, priority, is_active, created_at""",
            body.provider, body.key_name, encrypted, body.label, body.priority, body.notes,
        )
    if not row:
        raise HTTPException(status_code=500, detail="Insert failed")
    return dict(row)


@router.put("/{key_id}")
async def update_llm_key(key_id: int, body: LlmKeyUpdate) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM llm_api_keys WHERE id=$1", key_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Key not found")

        updates: list[str] = ["updated_at=NOW()"]
        params: list[Any] = []
        idx = 1

        if body.value is not None:
            updates.append(f"encrypted_value=${idx}")
            params.append(encrypt_value(body.value))
            idx += 1
        if body.label is not None:
            updates.append(f"label=${idx}")
            params.append(body.label)
            idx += 1
        if body.priority is not None:
            updates.append(f"priority=${idx}")
            params.append(body.priority)
            idx += 1
        if body.is_active is not None:
            updates.append(f"is_active=${idx}")
            params.append(body.is_active)
            idx += 1
        if body.notes is not None:
            updates.append(f"notes=${idx}")
            params.append(body.notes)
            idx += 1

        params.append(key_id)
        sql = (
            f"UPDATE llm_api_keys SET {', '.join(updates)} WHERE id=${idx} "
            "RETURNING id, provider, key_name, label, priority, is_active, updated_at"
        )
        row = await conn.fetchrow(sql, *params)
    return dict(row)


@router.delete("/{key_id}")
async def deactivate_llm_key(key_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE llm_api_keys SET is_active=false, updated_at=NOW() WHERE id=$1 RETURNING id, key_name, is_active",
            key_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")
    return dict(row)
'''

TARGET.write_text(CONTENT)
print(f"✅ 완료: {TARGET} ({len(CONTENT)} bytes)")
print(f"   백업: {TARGET}.bak_aads188")
