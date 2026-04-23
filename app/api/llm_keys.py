"""LLM API 키 관리 API — llm_api_keys 테이블 CRUD + 레지스트리 동기화."""
from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.core.credential_vault import decrypt_value, encrypt_value
from app.core.db_pool import get_pool
from app.core.llm_key_provider import invalidate_key_cache
from app.services.model_registry import append_key_audit_log, invalidate_registry_cache, normalize_provider, sync_model_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-keys", tags=["llm-keys"])


class LlmKeyCreate(BaseModel):
    provider: str
    key_name: str
    value: str
    label: str = ""
    priority: int = Field(1, ge=1)
    notes: str = ""
    is_active: bool = True

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        normalized = normalize_provider(value)
        if not normalized:
            raise ValueError("provider is required")
        return normalized

    @field_validator("key_name")
    @classmethod
    def validate_key_name(cls, value: str) -> str:
        key_name = (value or "").strip()
        if not key_name:
            raise ValueError("key_name is required")
        return key_name

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("value is required")
        return value.strip()


class LlmKeyUpdate(BaseModel):
    value: str | None = None
    label: str | None = None
    priority: int | None = Field(None, ge=1)
    is_active: bool | None = None
    notes: str | None = None
    provider: str | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_provider(value)
        if not normalized:
            raise ValueError("provider is required")
        return normalized


def _mask(val: str) -> str:
    if len(val) <= 8:
        return "****"
    return val[:4] + "****" + val[-4:]


async def _validate_priority(
    conn: asyncpg.Connection,
    *,
    provider: str,
    priority: int,
    is_active: bool,
    key_id: int | None = None,
) -> None:
    if not is_active:
        return
    row = await conn.fetchrow(
        """
        SELECT id, key_name
        FROM llm_api_keys
        WHERE provider = $1
          AND priority = $2
          AND is_active = TRUE
          AND ($3::int IS NULL OR id <> $3)
        LIMIT 1
        """,
        provider,
        priority,
        key_id,
    )
    if row:
        raise HTTPException(
            status_code=409,
            detail=f"Active priority conflict: {provider} priority {priority} already used by {row['key_name']}",
        )


async def _run_registry_sync(reason: str) -> dict[str, Any] | None:
    invalidate_key_cache()
    invalidate_registry_cache()
    try:
        return await sync_model_registry(triggered_by="llm_keys_api", reason=reason)
    except Exception:
        logger.exception("llm_keys.registry_sync_failed", extra={"reason": reason})
        return {"ok": False, "error": "sync_failed"}


@router.get("")
async def list_llm_keys() -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, provider, key_name, encrypted_value, label, priority,
                   is_active, rate_limited_until, last_used_at, last_verified_at,
                   created_at, updated_at, notes
            FROM llm_api_keys
            ORDER BY provider, priority, id
            """
        )
    result = []
    for row in rows:
        try:
            plain = decrypt_value(row["encrypted_value"])
            masked = _mask(plain)
        except Exception:
            masked = "****"
        result.append(
            {
                "id": row["id"],
                "provider": normalize_provider(row["provider"]),
                "key_name": row["key_name"],
                "masked_value": masked,
                "label": row["label"],
                "priority": row["priority"],
                "is_active": row["is_active"],
                "rate_limited_until": row["rate_limited_until"].isoformat() if row["rate_limited_until"] else None,
                "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
                "last_verified_at": row["last_verified_at"].isoformat() if row["last_verified_at"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "notes": row["notes"],
            }
        )
    return result


@router.post("")
async def create_llm_key(body: LlmKeyCreate) -> dict[str, Any]:
    encrypted = encrypt_value(body.value)
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _validate_priority(
                    conn,
                    provider=body.provider,
                    priority=body.priority,
                    is_active=body.is_active,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO llm_api_keys (
                        provider, key_name, encrypted_value, label, priority, notes, is_active,
                        rate_limited_until, last_verified_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, NULL, NOW())
                    RETURNING id, provider, key_name, label, priority, is_active, created_at
                    """,
                    body.provider,
                    body.key_name,
                    encrypted,
                    body.label,
                    body.priority,
                    body.notes,
                    body.is_active,
                )
                await append_key_audit_log(
                    conn,
                    key_id=row["id"] if row else None,
                    provider=body.provider,
                    key_name=body.key_name,
                    event_type="create",
                    actor="llm_keys_api",
                    details={
                        "label": body.label,
                        "priority": body.priority,
                        "is_active": body.is_active,
                        "notes_length": len(body.notes or ""),
                    },
                )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Duplicate key_name") from exc
    if not row:
        raise HTTPException(status_code=500, detail="Insert failed")
    sync_result = await _run_registry_sync(f"create:{body.key_name}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result


@router.put("/{key_id}")
async def update_llm_key(key_id: int, body: LlmKeyUpdate) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT id, provider, key_name, label, priority, is_active, notes
                FROM llm_api_keys
                WHERE id = $1
                """,
                key_id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Key not found")

            provider = body.provider or normalize_provider(existing["provider"])
            priority = body.priority if body.priority is not None else existing["priority"]
            is_active = body.is_active if body.is_active is not None else existing["is_active"]
            await _validate_priority(
                conn,
                provider=provider,
                priority=priority,
                is_active=is_active,
                key_id=key_id,
            )

            updates: list[str] = ["updated_at=NOW()"]
            params: list[Any] = []
            idx = 1

            if body.value is not None:
                if not body.value.strip():
                    raise HTTPException(status_code=400, detail="value cannot be empty")
                updates.append(f"encrypted_value=${idx}")
                params.append(encrypt_value(body.value.strip()))
                idx += 1
                updates.append("last_verified_at=NULL")
                updates.append("rate_limited_until=NULL")
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
            if body.provider is not None:
                updates.append(f"provider=${idx}")
                params.append(body.provider)
                idx += 1

            params.append(key_id)
            row = await conn.fetchrow(
                f"""
                UPDATE llm_api_keys
                SET {', '.join(updates)}
                WHERE id=${idx}
                RETURNING id, provider, key_name, label, priority, is_active, updated_at
                """,
                *params,
            )
            await append_key_audit_log(
                conn,
                key_id=key_id,
                provider=provider,
                key_name=existing["key_name"],
                event_type="update",
                actor="llm_keys_api",
                details={
                    "changed_fields": sorted(body.model_dump(exclude_none=True).keys()),
                    "priority": priority,
                    "is_active": is_active,
                },
            )

    invalidate_key_cache(existing["key_name"])
    sync_result = await _run_registry_sync(f"update:{existing['key_name']}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result


@router.post("/{key_id}/activate")
async def activate_llm_key(key_id: int) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, provider, key_name, priority FROM llm_api_keys WHERE id=$1",
                key_id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Key not found")
            provider = normalize_provider(existing["provider"])
            await _validate_priority(
                conn,
                provider=provider,
                priority=existing["priority"],
                is_active=True,
                key_id=key_id,
            )
            row = await conn.fetchrow(
                """
                UPDATE llm_api_keys
                SET is_active = TRUE,
                    rate_limited_until = NULL,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id, provider, key_name, is_active, updated_at
                """,
                key_id,
            )
            await append_key_audit_log(
                conn,
                key_id=key_id,
                provider=provider,
                key_name=existing["key_name"],
                event_type="activate",
                actor="llm_keys_api",
                details={"priority": existing["priority"]},
            )

    invalidate_key_cache(existing["key_name"])
    sync_result = await _run_registry_sync(f"activate:{existing['key_name']}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result


@router.delete("/{key_id}")
async def deactivate_llm_key(key_id: int) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, provider, key_name FROM llm_api_keys WHERE id=$1",
                key_id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Key not found")
            row = await conn.fetchrow(
                """
                UPDATE llm_api_keys
                SET is_active = FALSE, updated_at = NOW()
                WHERE id = $1
                RETURNING id, provider, key_name, is_active, updated_at
                """,
                key_id,
            )
            await append_key_audit_log(
                conn,
                key_id=key_id,
                provider=normalize_provider(existing["provider"]),
                key_name=existing["key_name"],
                event_type="deactivate",
                actor="llm_keys_api",
                details={},
            )

    invalidate_key_cache(existing["key_name"])
    sync_result = await _run_registry_sync(f"deactivate:{existing['key_name']}")
    result = dict(row)
    if sync_result and not sync_result.get("ok", True):
        result["registry_sync"] = sync_result
    return result
