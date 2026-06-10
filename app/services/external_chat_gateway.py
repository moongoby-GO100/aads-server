from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncGenerator, Literal

import asyncpg
import structlog

from app.core.anthropic_client import call_llm_with_fallback
from app.core.db_pool import get_pool
from app.services import chat_service
from app.services.tenant_usage_limits import (
    check_tenant_usage_limit,
    reset_soft_bypass_usage_limits,
    set_soft_bypass_usage_limits,
)

logger = structlog.get_logger(__name__)

Provider = Literal["newtalk"]
ServiceKey = Literal["v1_old", "v1_new", "v2"]

DEFAULT_PROVIDER: Provider = "newtalk"
DEFAULT_SERVICES: tuple[ServiceKey, ...] = ("v1_old", "v1_new", "v2")
DEFAULT_WORKSPACE_NAME = "[NTV2] NewTalk V2"
DEFAULT_SYSTEM_PROMPT = (
    "You are AADS AI embedded in NewTalk. Help operators improve service, "
    "triage issues, explain workflows, and draft operational actions. "
    "Do not expose internal secrets or perform destructive actions."
)
DEFAULT_DIRECT_MODEL = "qwen-turbo"


@dataclass(frozen=True)
class ExternalChatSettings:
    enabled: bool
    kill_switch: bool
    admin_only: bool
    tokens: tuple[str, ...]
    hmac_secret: str
    tenant_id: str
    workspace_name: str
    model: str
    unlimited_first: bool
    allowed_origins: tuple[str, ...]


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())


def get_settings() -> ExternalChatSettings:
    tokens = _csv_env("AADS_EXTERNAL_CHAT_TOKENS")
    single_token = os.getenv("AADS_EXTERNAL_CHAT_TOKEN", "").strip()
    if single_token:
        tokens = (*tokens, single_token)
    enabled = _truthy(os.getenv("AADS_EXTERNAL_CHAT_ENABLED"), default=bool(tokens))
    return ExternalChatSettings(
        enabled=enabled,
        kill_switch=_truthy(os.getenv("AADS_EXTERNAL_CHAT_KILL_SWITCH")),
        admin_only=_truthy(os.getenv("AADS_EXTERNAL_CHAT_ADMIN_ONLY"), default=True),
        tokens=tuple(dict.fromkeys(tokens)),
        hmac_secret=os.getenv("AADS_EXTERNAL_CHAT_HMAC_SECRET", "").strip(),
        tenant_id=os.getenv("AADS_EXTERNAL_CHAT_TENANT_ID", "").strip(),
        workspace_name=os.getenv("AADS_EXTERNAL_CHAT_WORKSPACE_NAME", DEFAULT_WORKSPACE_NAME).strip()
        or DEFAULT_WORKSPACE_NAME,
        model=os.getenv("AADS_EXTERNAL_CHAT_MODEL", "").strip(),
        unlimited_first=_truthy(os.getenv("AADS_EXTERNAL_CHAT_UNLIMITED_FIRST"), default=True),
        allowed_origins=_csv_env("AADS_EXTERNAL_CHAT_ALLOWED_ORIGINS"),
    )


def _direct_model(settings: ExternalChatSettings) -> str:
    return (
        settings.model
        or os.getenv("AADS_EXTERNAL_CHAT_DIRECT_MODEL", "").strip()
        or DEFAULT_DIRECT_MODEL
    )


def verify_service_token(token: str, settings: ExternalChatSettings | None = None) -> bool:
    cfg = settings or get_settings()
    candidate = str(token or "").removeprefix("Bearer ").strip()
    return bool(candidate and any(hmac.compare_digest(candidate, known) for known in cfg.tokens))


def verify_hmac_signature(
    *,
    body: bytes,
    signature: str,
    timestamp: str = "",
    settings: ExternalChatSettings | None = None,
) -> bool:
    cfg = settings or get_settings()
    if not cfg.hmac_secret or not signature:
        return False
    signed_payload = body if not timestamp else timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(cfg.hmac_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(signature.strip(), expected)


async def ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_chat_sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider TEXT NOT NULL,
            service TEXT NOT NULL,
            external_user_id TEXT NOT NULL,
            display_name TEXT,
            aads_session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(provider, service, external_user_id, tenant_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_external_chat_sessions_aads_session
            ON external_chat_sessions(aads_session_id)
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_chat_usage_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_session_id UUID REFERENCES external_chat_sessions(id) ON DELETE SET NULL,
            provider TEXT NOT NULL,
            service TEXT NOT NULL,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            soft_bypass BOOLEAN NOT NULL DEFAULT FALSE,
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_external_chat_usage_events_provider_created
            ON external_chat_usage_events(provider, service, created_at DESC)
        """
    )


async def resolve_tenant_id(conn: asyncpg.Connection, settings: ExternalChatSettings) -> str:
    if settings.tenant_id:
        tenant_id = await conn.fetchval(
            "SELECT id::text FROM tenants WHERE id = $1::uuid AND deleted_at IS NULL",
            settings.tenant_id,
        )
        if tenant_id:
            return str(tenant_id)
        raise ValueError("external_chat_tenant_not_found")

    try:
        tenant_id = await conn.fetchval("SELECT public.aads_internal_tenant_id()::text")
    except asyncpg.PostgresError:
        tenant_id = None
    if not tenant_id:
        raise ValueError("external_chat_tenant_not_configured")
    return str(tenant_id)


async def resolve_workspace_id(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    workspace_name: str,
) -> str:
    row = await conn.fetchrow(
        """
        SELECT id::text
          FROM chat_workspaces
         WHERE tenant_id = $1::uuid
           AND name = $2
         LIMIT 1
        """,
        tenant_id,
        workspace_name,
    )
    if row:
        return str(row["id"])

    row = await conn.fetchrow(
        """
        INSERT INTO chat_workspaces
            (tenant_id, name, system_prompt, files, settings, color, icon)
        VALUES
            ($1::uuid, $2, $3, '[]'::jsonb, $4::jsonb, '#06B6D4', 'NT')
        RETURNING id::text
        """,
        tenant_id,
        workspace_name,
        DEFAULT_SYSTEM_PROMPT,
        json.dumps({"external_chat": True, "provider": DEFAULT_PROVIDER}),
    )
    return str(row["id"])


async def create_or_resume_session(
    *,
    provider: Provider,
    service: ServiceKey,
    external_user_id: str,
    display_name: str = "",
    metadata: dict[str, Any] | None = None,
    settings: ExternalChatSettings | None = None,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    if cfg.kill_switch:
        raise RuntimeError("external_chat_disabled_by_kill_switch")

    clean_user_id = external_user_id.strip()
    if not clean_user_id:
        raise ValueError("external_user_id_required")
    assert_admin_context(metadata or {}, cfg)

    pool = get_pool()
    async with pool.acquire() as conn:
        await ensure_schema(conn)
        tenant_id = await resolve_tenant_id(conn, cfg)
        existing = await conn.fetchrow(
            """
            SELECT id::text, aads_session_id::text, tenant_id::text, provider, service,
                   external_user_id, display_name, metadata, created_at, updated_at, last_seen_at
              FROM external_chat_sessions
             WHERE provider = $1
               AND service = $2
               AND external_user_id = $3
               AND tenant_id = $4::uuid
             LIMIT 1
            """,
            provider,
            service,
            clean_user_id,
            tenant_id,
        )
        if existing:
            row = await conn.fetchrow(
                """
                UPDATE external_chat_sessions
                   SET display_name = COALESCE(NULLIF($2, ''), display_name),
                       metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb,
                       updated_at = NOW(),
                       last_seen_at = NOW()
                 WHERE id = $1::uuid
                RETURNING id::text, aads_session_id::text, tenant_id::text, provider, service,
                          external_user_id, display_name, metadata, created_at, updated_at, last_seen_at
                """,
                existing["id"],
                display_name.strip(),
                json.dumps(metadata or {}),
            )
            return _session_row_to_dict(row)

        workspace_id = await resolve_workspace_id(
            conn,
            tenant_id=tenant_id,
            workspace_name=cfg.workspace_name,
        )
        title = f"NewTalk {service} - {clean_user_id[:80]}"
        chat_session = await chat_service.create_session(
            {
                "workspace_id": workspace_id,
                "title": title,
                "current_model": cfg.model or None,
                "role_key": "Ops",
            },
            tenant_id=tenant_id,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO external_chat_sessions
                (provider, service, external_user_id, display_name, aads_session_id,
                 tenant_id, metadata)
            VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7::jsonb)
            RETURNING id::text, aads_session_id::text, tenant_id::text, provider, service,
                      external_user_id, display_name, metadata, created_at, updated_at, last_seen_at
            """,
            provider,
            service,
            clean_user_id,
            display_name.strip() or None,
            str(chat_session["id"]),
            tenant_id,
            json.dumps(metadata or {}),
        )
        return _session_row_to_dict(row)


async def get_external_session(external_session_id: str) -> dict[str, Any] | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await ensure_schema(conn)
        row = await conn.fetchrow(
            """
            SELECT id::text, aads_session_id::text, tenant_id::text, provider, service,
                   external_user_id, display_name, metadata, created_at, updated_at, last_seen_at
              FROM external_chat_sessions
             WHERE id = $1::uuid
            """,
            uuid.UUID(external_session_id),
        )
    return _session_row_to_dict(row) if row else None


async def list_messages(external_session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    session = await get_external_session(external_session_id)
    if not session:
        raise ValueError("external_session_not_found")
    messages = await chat_service.list_messages(
        session["aads_session_id"],
        limit=limit,
        sort="asc",
        include_streaming=False,
        read_only=True,
        tenant_id=session["tenant_id"],
    )
    return [_public_message(m) for m in messages]


async def send_message(
    *,
    external_session_id: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    model_override: str | None = None,
    response_mode: str = "quality",
    settings: ExternalChatSettings | None = None,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    if cfg.kill_switch:
        raise RuntimeError("external_chat_disabled_by_kill_switch")
    session = await get_external_session(external_session_id)
    if not session:
        raise ValueError("external_session_not_found")
    clean_content = content.strip()
    if not clean_content:
        raise ValueError("content_required")
    assert_admin_context(session.get("metadata") or metadata or {}, cfg)

    usage_status = "not_checked"
    soft_bypass = False
    try:
        decision = await check_tenant_usage_limit(
            session["tenant_id"],
            operation="external_chat:send_message",
            projected_calls=1,
            raise_on_block=not cfg.unlimited_first,
        )
        usage_status = decision.status
        soft_bypass = decision.status == "soft_bypass"
    except Exception as exc:
        if not cfg.unlimited_first:
            raise
        usage_status = f"soft_bypass_error:{type(exc).__name__}"
        soft_bypass = True

    await record_usage_event(
        external_session_id=session["id"],
        provider=session["provider"],
        service=session["service"],
        tenant_id=session["tenant_id"],
        operation="send_message",
        status=usage_status,
        soft_bypass=soft_bypass,
        metadata=metadata or {},
    )

    if response_mode.strip().lower() in {"fast", "direct", "widget"}:
        assistant_content, model_used = await send_direct_message(
            session=session,
            content=clean_content,
            metadata=metadata or {},
            settings=cfg,
        )
        return {
            "external_session_id": session["id"],
            "aads_session_id": session["aads_session_id"],
            "assistant_message": assistant_content,
            "usage_status": usage_status,
            "soft_bypass": soft_bypass,
            "stream": {
                "direct": True,
                "done": {"type": "done", "model": model_used},
                "errors": [],
            },
        }

    token = set_soft_bypass_usage_limits(cfg.unlimited_first)
    try:
        assistant_content, stream_meta = await collect_chat_stream(
            chat_service.send_message_stream(
                session_id=session["aads_session_id"],
                content=clean_content,
                attachments=[],
                model_override=model_override or cfg.model or None,
                response_mode=response_mode,
                tenant_id=session["tenant_id"],
            )
        )
    finally:
        reset_soft_bypass_usage_limits(token)

    return {
        "external_session_id": session["id"],
        "aads_session_id": session["aads_session_id"],
        "assistant_message": assistant_content,
        "usage_status": usage_status,
        "soft_bypass": soft_bypass,
        "stream": stream_meta,
    }


async def send_direct_message(
    *,
    session: dict[str, Any],
    content: str,
    metadata: dict[str, Any],
    settings: ExternalChatSettings,
) -> tuple[str, str]:
    model = _direct_model(settings)
    service = str(session.get("service") or "")
    display_name = str(session.get("display_name") or "NewTalk admin")
    system = (
        f"{DEFAULT_SYSTEM_PROMPT}\n\n"
        "Respond in Korean unless the user asks otherwise. "
        "Keep the answer concise and operational. "
        "If an action is risky or requires unavailable credentials, say so directly."
    )
    prompt = (
        f"Provider: {session.get('provider')}\n"
        f"Service: {service}\n"
        f"Admin: {display_name}\n"
        f"Metadata: {json.dumps(metadata, ensure_ascii=False)[:1000]}\n\n"
        f"User message:\n{content}"
    )
    assistant_content = await call_llm_with_fallback(
        prompt,
        model=model,
        max_tokens=700,
        system=system,
        tenant_id=session["tenant_id"],
    )
    assistant_content = (assistant_content or "").strip()
    if not assistant_content:
        raise RuntimeError("external_chat_empty_response")

    pool = get_pool()
    async with pool.acquire() as conn:
        user_msg = await chat_service._save_message(
            conn,
            uuid.UUID(session["aads_session_id"]),
            "user",
            content,
            model_used=model,
            intent="external_chat",
            tools_called=[],
        )
        await chat_service._save_message(
            conn,
            uuid.UUID(session["aads_session_id"]),
            "assistant",
            assistant_content,
            model_used=model,
            intent="external_chat",
            cost=Decimal("0"),
            tokens_in=0,
            tokens_out=0,
            tools_called=[],
            reply_to_id=uuid.UUID(str(user_msg["id"])) if user_msg else None,
        )
    return assistant_content, model


async def collect_chat_stream(stream: AsyncGenerator[str, None]) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    done: dict[str, Any] = {}
    errors: list[str] = []
    async for chunk in stream:
        for line in str(chunk).splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "delta":
                parts.append(str(event.get("content") or ""))
            elif event_type == "done":
                done = event
            elif event_type == "error":
                errors.append(str(event.get("content") or event.get("message") or "stream_error"))
    return "".join(parts).strip(), {"done": done, "errors": errors}


async def record_usage_event(
    *,
    external_session_id: str,
    provider: str,
    service: str,
    tenant_id: str,
    operation: str,
    status: str,
    soft_bypass: bool,
    metadata: dict[str, Any],
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await ensure_schema(conn)
        await conn.execute(
            """
            INSERT INTO external_chat_usage_events
                (external_session_id, provider, service, tenant_id, operation,
                 status, soft_bypass, metadata)
            VALUES ($1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8::jsonb)
            """,
            external_session_id,
            provider,
            service,
            tenant_id,
            operation,
            status,
            bool(soft_bypass),
            json.dumps(metadata or {}),
        )


def widget_config(provider: Provider, service: ServiceKey) -> dict[str, Any]:
    cfg = get_settings()
    return {
        "provider": provider,
        "service": service,
        "enabled": cfg.enabled and not cfg.kill_switch,
        "features": {
            "history": True,
            "message_send": True,
            "attachments": False,
            "streaming": False,
            "unlimited_first": cfg.unlimited_first,
        },
        "policy": {
            "usage_mode": "soft_telemetry" if cfg.unlimited_first else "hard_limit",
            "admin_only": cfg.admin_only,
            "requires_server_proxy": True,
            "supported_services": list(DEFAULT_SERVICES),
        },
    }


def assert_admin_context(metadata: dict[str, Any] | str | None, settings: ExternalChatSettings | None = None) -> None:
    cfg = settings or get_settings()
    if not cfg.admin_only:
        return
    if metadata_has_admin_context(metadata or {}):
        return
    raise PermissionError("external_chat_admin_required")


def metadata_has_admin_context(metadata: dict[str, Any] | str | None) -> bool:
    metadata = _metadata_dict(metadata)
    if _bool_like(metadata.get("aads_admin_context")):
        return True
    if _bool_like(metadata.get("is_admin")) or _bool_like(metadata.get("newtalk_is_admin")):
        return True

    role_values: list[str] = []
    for key in ("role", "roles", "newtalk_role", "newtalk_roles"):
        value = metadata.get(key)
        if isinstance(value, str):
            role_values.extend(part.strip().lower() for part in value.split(","))
        elif isinstance(value, (list, tuple, set)):
            role_values.extend(str(part).strip().lower() for part in value)
    return any(role in {"admin", "administrator", "owner", "super_admin"} for role in role_values)


def _bool_like(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _session_row_to_dict(row: asyncpg.Record | None) -> dict[str, Any]:
    if not row:
        return {}
    data = dict(row)
    data["metadata"] = _metadata_dict(data.get("metadata"))
    for key in ("created_at", "updated_at", "last_seen_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.astimezone(timezone.utc).isoformat()
    return data


def _public_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(message.get("id")),
        "role": message.get("role"),
        "content": message.get("content") or "",
        "created_at": message.get("created_at").isoformat()
        if hasattr(message.get("created_at"), "isoformat")
        else message.get("created_at"),
    }
