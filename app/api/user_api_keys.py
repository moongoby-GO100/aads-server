"""BYOK(Bring Your Own Key) — 사용자별 AI API 키 등록/조회/삭제/검증.

SaaS 사용자가 본인의 OpenAI/Anthropic/Gemini 등 API 키를 등록하면
채팅 시 시스템 기본 키보다 우선 사용된다 (app.core.anthropic_client.call_llm_with_fallback).
키 원문은 Fernet 암호화 후 user_api_keys 테이블에 저장하며, API 응답에는 마스킹된 값만 노출한다.

암호화는 기존 e2e_credentials/credential_vault.py 패턴을 그대로 재사용한다
(app.core.credential_vault.encrypt_value/decrypt_value, VAULT_ENCRYPTION_KEY 기반 Fernet).
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import TenantRole, require_tenant_role
from app.core.credential_vault import decrypt_value, encrypt_value
from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/user/api-keys", tags=["user-api-keys"])

require_tenant_member = require_tenant_role(TenantRole.MEMBER)

_SUPPORTED_PROVIDERS = {"anthropic", "openai", "gemini", "dashscope"}
_USER_API_KEYS_TABLE_READY = False
_USER_API_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS user_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    encrypted_key TEXT NOT NULL,
    display_name TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_api_keys_user_provider
    ON user_api_keys(user_id, provider);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_user_active
    ON user_api_keys(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_tenant_provider
    ON user_api_keys(tenant_id, provider)
    WHERE is_active = TRUE;
"""


async def _ensure_user_api_keys_table() -> None:
    global _USER_API_KEYS_TABLE_READY
    if _USER_API_KEYS_TABLE_READY:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute(_USER_API_KEYS_DDL)
    _USER_API_KEYS_TABLE_READY = True


def _user_id(context: dict) -> str:
    return str(context["user"]["user_id"])


def _tenant_id(context: dict) -> Optional[str]:
    tenant = context.get("tenant") or {}
    tid = tenant.get("id")
    return str(tid) if tid else None


def _normalize_provider(value: str) -> str:
    provider = (value or "").strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"지원하지 않는 provider: {value}. 지원: {sorted(_SUPPORTED_PROVIDERS)}")
    return provider


def _mask_key(plain: str) -> str:
    """sk-...xxxx 형태로 마스킹. 원문은 절대 응답에 포함하지 않는다."""
    plain = plain or ""
    if len(plain) <= 8:
        return "****"
    return f"{plain[:5]}...{plain[-4:]}"


class UserApiKeyCreate(BaseModel):
    provider: str
    key: str
    display_name: str = ""

    @field_validator("provider")
    @classmethod
    def _v_provider(cls, value: str) -> str:
        return _normalize_provider(value)

    @field_validator("key")
    @classmethod
    def _v_key(cls, value: str) -> str:
        key = (value or "").strip()
        if not key:
            raise ValueError("key is required")
        return key


class UserApiKeyOut(BaseModel):
    id: str
    provider: str
    masked_key: str
    display_name: Optional[str] = None
    is_active: bool
    last_used_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _row_to_out(row: asyncpg.Record) -> dict[str, Any]:
    try:
        plain = decrypt_value(row["encrypted_key"])
        masked = _mask_key(plain)
    except Exception:
        masked = "****"
    return {
        "id": str(row["id"]),
        "provider": row["provider"],
        "masked_key": masked,
        "display_name": row["display_name"],
        "is_active": row["is_active"],
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("")
async def list_user_api_keys(
    context: dict = Depends(require_tenant_member),
) -> list[dict[str, Any]]:
    """현재 로그인한 사용자의 API 키 목록 (마스킹)."""
    await _ensure_user_api_keys_table()
    user_id = _user_id(context)
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, provider, encrypted_key, display_name, is_active,
               last_used_at, created_at, updated_at
        FROM user_api_keys
        WHERE user_id = $1
        ORDER BY provider
        """,
        user_id,
    )
    return [_row_to_out(row) for row in rows]


@router.post("")
async def create_user_api_key(
    body: UserApiKeyCreate,
    context: dict = Depends(require_tenant_member),
) -> dict[str, Any]:
    """API 키 등록 (Fernet 암호화 저장). 동일 provider 재등록 시 갱신(UPSERT)."""
    await _ensure_user_api_keys_table()
    user_id = _user_id(context)
    tenant_id = _tenant_id(context)
    encrypted = encrypt_value(body.key)

    pool = get_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO user_api_keys (user_id, tenant_id, provider, encrypted_key, display_name, is_active)
            VALUES ($1, $2::uuid, $3, $4, $5, TRUE)
            ON CONFLICT (user_id, provider)
            DO UPDATE SET
                encrypted_key = EXCLUDED.encrypted_key,
                display_name = EXCLUDED.display_name,
                tenant_id = EXCLUDED.tenant_id,
                is_active = TRUE,
                last_used_at = NULL,
                updated_at = NOW()
            RETURNING id, provider, encrypted_key, display_name, is_active,
                      last_used_at, created_at, updated_at
            """,
            user_id,
            tenant_id,
            body.provider,
            encrypted,
            (body.display_name or "").strip() or None,
        )
    except asyncpg.ForeignKeyViolationError as exc:
        raise HTTPException(status_code=400, detail="Invalid user or tenant") from exc

    logger.info("user_api_key_created user_id=%s provider=%s", user_id[:12], body.provider)
    return _row_to_out(row)


@router.delete("/{key_id}")
async def delete_user_api_key(
    key_id: str,
    context: dict = Depends(require_tenant_member),
) -> dict[str, Any]:
    """본인 소유 키만 삭제 가능 (user_id 검증)."""
    await _ensure_user_api_keys_table()
    user_id = _user_id(context)
    try:
        key_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key id")

    pool = get_pool()
    existing = await pool.fetchrow(
        "SELECT id FROM user_api_keys WHERE id = $1 AND user_id = $2",
        key_uuid,
        user_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Key not found")

    await pool.execute(
        "DELETE FROM user_api_keys WHERE id = $1 AND user_id = $2",
        key_uuid,
        user_id,
    )
    logger.info("user_api_key_deleted user_id=%s key_id=%s", user_id[:12], key_id)
    return {"ok": True, "id": key_id}


async def _test_anthropic_key(plain_key: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": plain_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        if resp.status_code in (200, 201):
            return True, "ok"
        if resp.status_code == 401:
            return False, "invalid_api_key"
        return False, f"http_{resp.status_code}"
    except Exception as exc:
        return False, f"error:{str(exc)[:120]}"


async def _test_openai_key(plain_key: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {plain_key}"},
            )
        if resp.status_code == 200:
            return True, "ok"
        if resp.status_code == 401:
            return False, "invalid_api_key"
        return False, f"http_{resp.status_code}"
    except Exception as exc:
        return False, f"error:{str(exc)[:120]}"


async def _test_gemini_key(plain_key: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": plain_key},
            )
        if resp.status_code == 200:
            return True, "ok"
        if resp.status_code in (400, 401, 403):
            return False, "invalid_api_key"
        return False, f"http_{resp.status_code}"
    except Exception as exc:
        return False, f"error:{str(exc)[:120]}"


@router.post("/{key_id}/test")
async def test_user_api_key(
    key_id: str,
    context: dict = Depends(require_tenant_member),
) -> dict[str, Any]:
    """등록된 키의 유효성을 실제 provider API로 검증."""
    await _ensure_user_api_keys_table()
    user_id = _user_id(context)
    try:
        key_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key id")

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, provider, encrypted_key FROM user_api_keys WHERE id = $1 AND user_id = $2",
        key_uuid,
        user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")

    try:
        plain_key = decrypt_value(row["encrypted_key"])
    except Exception:
        return {"ok": False, "reason": "decrypt_failed"}

    provider = row["provider"]
    if provider == "anthropic":
        ok, reason = await _test_anthropic_key(plain_key)
    elif provider == "openai":
        ok, reason = await _test_openai_key(plain_key)
    elif provider == "gemini":
        ok, reason = await _test_gemini_key(plain_key)
    else:
        return {"ok": False, "reason": f"provider_test_not_supported:{provider}"}

    if ok:
        await pool.execute(
            "UPDATE user_api_keys SET last_used_at = NOW() WHERE id = $1",
            key_uuid,
        )
    logger.info("user_api_key_tested user_id=%s provider=%s ok=%s", user_id[:12], provider, ok)
    return {"ok": ok, "reason": reason, "provider": provider}
