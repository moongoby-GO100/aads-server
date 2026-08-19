"""Agent Vault service for managed browser credentials and autofill tokens."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from typing import Any
from urllib.parse import urlparse

from app.core.credential_vault import decrypt_value, encrypt_value
from app.core.db_pool import get_pool
from app.services.browser_permission_policy import mask_sensitive_value


def normalize_origin(url_or_origin: str) -> str:
    parsed = urlparse((url_or_origin or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if parsed.netloc:
        return f"https://{parsed.netloc.lower()}"
    text = (url_or_origin or "").strip().rstrip("/")
    if not text:
        raise ValueError("origin_required")
    return text.lower()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _tenant_uuid(tenant_id: str) -> uuid.UUID:
    if not tenant_id:
        raise ValueError("tenant_id_required")
    return uuid.UUID(str(tenant_id))


def _row_to_credential(row: Any, *, include_secret: bool = False) -> dict[str, Any]:
    item = dict(row)
    result = {
        "id": str(item["id"]),
        "tenant_id": str(item["tenant_id"]),
        "work_key": item["work_key"],
        "origin": item["origin"],
        "label": item["label"],
        "username": decrypt_value(item["username_enc"]),
        "password": "********",
        "metadata": item.get("metadata") or {},
        "is_active": item["is_active"],
        "last_used_at": item["last_used_at"].isoformat() if item.get("last_used_at") else None,
        "created_at": item["created_at"].isoformat() if item.get("created_at") else None,
        "updated_at": item["updated_at"].isoformat() if item.get("updated_at") else None,
    }
    if include_secret:
        result["password"] = decrypt_value(item["password_enc"])
    return result


async def list_agent_credentials(*, tenant_id: str, work_key: str | None = None, origin: str | None = None) -> list[dict[str, Any]]:
    conditions = ["tenant_id = $1", "is_active = TRUE"]
    args: list[Any] = [_tenant_uuid(tenant_id)]
    idx = 2
    if work_key:
        conditions.append(f"work_key = ${idx}")
        args.append(work_key)
        idx += 1
    if origin:
        conditions.append(f"origin = ${idx}")
        args.append(normalize_origin(origin))
    query = f"""
        SELECT *
          FROM agent_vault_credentials
         WHERE {' AND '.join(conditions)}
         ORDER BY work_key, origin, label
    """
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [_row_to_credential(row) for row in rows]


async def upsert_agent_credential(
    *,
    tenant_id: str,
    user_id: str,
    work_key: str,
    origin: str,
    label: str,
    username: str,
    password: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    origin_norm = normalize_origin(origin)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_vault_credentials (
                tenant_id, work_key, origin, label, username_enc, password_enc, metadata, created_by, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, NOW())
            ON CONFLICT (tenant_id, work_key, origin, label) DO UPDATE
               SET username_enc = EXCLUDED.username_enc,
                   password_enc = EXCLUDED.password_enc,
                   metadata = EXCLUDED.metadata,
                   is_active = TRUE,
                   updated_at = NOW()
            RETURNING *
            """,
            _tenant_uuid(tenant_id),
            work_key,
            origin_norm,
            label or "default",
            encrypt_value(username),
            encrypt_value(password),
            json.dumps(mask_sensitive_value(metadata or {}), ensure_ascii=False),
            user_id,
        )
        await write_access_log(
            conn=conn,
            tenant_id=tenant_id,
            credential_id=str(row["id"]),
            work_key=work_key,
            origin=origin_norm,
            action="credential_upsert",
            status="success",
            user_id=user_id,
            details={"label": label or "default"},
        )
    return _row_to_credential(row)


async def disable_agent_credential(*, tenant_id: str, credential_id: str, user_id: str) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE agent_vault_credentials
               SET is_active = FALSE, updated_at = NOW()
             WHERE id = $1 AND tenant_id = $2
             RETURNING id, work_key, origin
            """,
            uuid.UUID(credential_id),
            _tenant_uuid(tenant_id),
        )
        if not row:
            return False
        await write_access_log(
            conn=conn,
            tenant_id=tenant_id,
            credential_id=credential_id,
            work_key=row["work_key"],
            origin=row["origin"],
            action="credential_disable",
            status="success",
            user_id=user_id,
            details={},
        )
    return True


async def issue_autofill_token(
    *,
    tenant_id: str,
    credential_id: str,
    work_key: str,
    origin: str,
    user_id: str,
    ttl_seconds: int = 60,
) -> dict[str, Any]:
    origin_norm = normalize_origin(origin)
    token = secrets.token_urlsafe(32)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id
              FROM agent_vault_credentials
             WHERE id = $1
               AND tenant_id = $2
               AND work_key = $3
               AND origin = $4
               AND is_active = TRUE
            """,
            uuid.UUID(credential_id),
            _tenant_uuid(tenant_id),
            work_key,
            origin_norm,
        )
        if not row:
            raise ValueError("credential_not_found_for_origin")
        await conn.execute(
            """
            INSERT INTO agent_vault_autofill_tokens (
                token_hash, tenant_id, credential_id, work_key, origin, expires_at, created_by
            )
            VALUES ($1, $2, $3, $4, $5, NOW() + ($6::int * INTERVAL '1 second'), $7)
            """,
            _token_hash(token),
            _tenant_uuid(tenant_id),
            uuid.UUID(credential_id),
            work_key,
            origin_norm,
            max(1, min(ttl_seconds, 60)),
            user_id,
        )
        await write_access_log(
            conn=conn,
            tenant_id=tenant_id,
            credential_id=credential_id,
            work_key=work_key,
            origin=origin_norm,
            action="autofill_token_issue",
            status="success",
            user_id=user_id,
            details={"ttl_seconds": max(1, min(ttl_seconds, 60))},
        )
    return {"token": token, "expires_in": max(1, min(ttl_seconds, 60)), "origin": origin_norm}


async def redeem_autofill_token(*, token: str, origin: str, work_key: str, user_id: str) -> dict[str, Any]:
    origin_norm = normalize_origin(origin)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT t.token_hash,
                       t.tenant_id::text AS tenant_id,
                       t.credential_id::text AS credential_id,
                       c.username_enc,
                       c.password_enc
                  FROM agent_vault_autofill_tokens t
                  JOIN agent_vault_credentials c ON c.id = t.credential_id
                 WHERE t.token_hash = $1
                   AND t.work_key = $2
                   AND t.origin = $3
                   AND t.redeemed_at IS NULL
                   AND t.expires_at > NOW()
                   AND c.is_active = TRUE
                 FOR UPDATE
                """,
                _token_hash(token),
                work_key,
                origin_norm,
            )
            if not row:
                raise ValueError("autofill_token_invalid_or_expired")
            await conn.execute(
                "UPDATE agent_vault_autofill_tokens SET redeemed_at = NOW() WHERE token_hash = $1",
                row["token_hash"],
            )
            await conn.execute(
                "UPDATE agent_vault_credentials SET last_used_at = NOW(), updated_at = NOW() WHERE id = $1",
                uuid.UUID(row["credential_id"]),
            )
            await write_access_log(
                conn=conn,
                tenant_id=row["tenant_id"],
                credential_id=row["credential_id"],
                work_key=work_key,
                origin=origin_norm,
                action="autofill_token_redeem",
                status="success",
                user_id=user_id,
                details={},
            )
    return {
        "credential_id": row["credential_id"],
        "origin": origin_norm,
        "username": decrypt_value(row["username_enc"]),
        "password": decrypt_value(row["password_enc"]),
    }


async def write_access_log(
    *,
    conn: Any,
    tenant_id: str,
    credential_id: str | None,
    work_key: str,
    origin: str,
    action: str,
    status: str,
    user_id: str,
    details: dict[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO agent_vault_access_logs (
            tenant_id, credential_id, work_key, origin, action, status, actor_user_id, details
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        """,
        _tenant_uuid(tenant_id),
        uuid.UUID(credential_id) if credential_id else None,
        work_key,
        origin,
        action,
        status,
        user_id,
        json.dumps(mask_sensitive_value(details), ensure_ascii=False),
    )


async def list_access_logs(*, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, credential_id::text, work_key, origin, action, status, actor_user_id, details, created_at
              FROM agent_vault_access_logs
             WHERE tenant_id = $1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            _tenant_uuid(tenant_id),
            max(1, min(limit, 200)),
        )
    return [
        {
            **dict(row),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]
