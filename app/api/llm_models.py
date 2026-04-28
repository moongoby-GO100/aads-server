"""LLM 모델 레지스트리 조회/동기화 API."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.db_pool import get_pool
from app.services.model_registry import (
    list_provider_summaries,
    list_registered_models,
    normalize_provider,
    sync_model_registry,
)

router = APIRouter(prefix="/llm-models", tags=["llm-models"])


class ChatModelPreferenceInput(BaseModel):
    model_id: str
    provider: str | None = None
    preference_key: str | None = None
    display_order: int = Field(0, ge=0)
    is_hidden: bool = False
    is_favorite: bool = False
    is_pinned: bool = False


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


async def _ensure_chat_model_preferences_table() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_model_preferences (
                preference_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'legacy',
                model_id TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
                is_favorite BOOLEAN NOT NULL DEFAULT FALSE,
                is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by TEXT
            )
            """
        )
        await conn.execute("ALTER TABLE chat_model_preferences ADD COLUMN IF NOT EXISTS provider TEXT")
        await conn.execute("ALTER TABLE chat_model_preferences ADD COLUMN IF NOT EXISTS preference_key TEXT")
        await conn.execute(
            """
            UPDATE chat_model_preferences
            SET provider = CASE
                    WHEN model_id IN ('mixture', 'auto') THEN 'auto'
                    WHEN provider IS NULL OR provider = '' THEN 'legacy'
                    ELSE provider
                END
            WHERE provider IS NULL OR provider = ''
            """
        )
        await conn.execute(
            """
            UPDATE chat_model_preferences
            SET preference_key = CASE
                    WHEN model_id IN ('mixture', 'auto') THEN 'mixture'
                    WHEN preference_key IS NULL OR preference_key = '' THEN provider || ':' || model_id
                    ELSE preference_key
                END
            WHERE preference_key IS NULL OR preference_key = ''
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_model_preferences_order
            ON chat_model_preferences(is_pinned DESC, is_favorite DESC, display_order ASC, provider ASC, model_id ASC)
            """
        )


def _build_chat_preference_key(model_id: str, provider: str | None = None, preference_key: str | None = None) -> tuple[str, str, str]:
    normalized_model = model_id.strip()
    normalized_provider = (provider or "").strip().lower()
    normalized_key = (preference_key or "").strip()
    if normalized_key:
        if normalized_key in {"mixture", "auto"}:
            return "mixture", "auto", "mixture"
        if not normalized_provider and ":" in normalized_key:
            normalized_provider, normalized_model = normalized_key.split(":", 1)
        return normalized_key, normalized_provider or "legacy", normalized_model
    if normalized_model in {"mixture", "auto"}:
        return "mixture", "auto", "mixture"
    if ":" in normalized_model and not normalized_provider:
        normalized_provider, normalized_model = normalized_model.split(":", 1)
    normalized_provider = normalized_provider or "legacy"
    return f"{normalized_provider}:{normalized_model}", normalized_provider, normalized_model


async def _fetch_last_registry_sync() -> dict[str, Any] | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT actor, details, created_at
            FROM llm_key_audit_logs
            WHERE event_type = 'registry_sync'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
    if not row:
        return None
    return {
        "actor": row["actor"],
        "details": _coerce_json_object(row["details"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.get("")
async def list_llm_models(
    provider: str | None = Query(None),
    active_only: bool = Query(False),
) -> dict[str, Any]:
    models = await list_registered_models(provider=provider, active_only=active_only)
    return {"models": models, "total": len(models)}


@router.get("/providers/summary")
async def get_provider_summary() -> dict[str, Any]:
    summaries = await list_provider_summaries()
    last_sync = await _fetch_last_registry_sync()
    return {
        "providers": summaries,
        "total": len(summaries),
        "active_provider_count": sum(1 for row in summaries if row.get("active_model_count", 0) > 0),
        "rate_limited_provider_count": sum(1 for row in summaries if row.get("status") == "rate_limited"),
        "review_required_providers": [row["provider"] for row in summaries if row.get("requires_admin_review")],
        "last_sync_at": last_sync["created_at"] if last_sync else None,
        "last_sync_reason": (last_sync["details"].get("reason") if last_sync else None),
        "last_sync_actor": (last_sync["actor"] if last_sync else None),
        "normalized_providers": {},
    }


@router.get("/discovery-runs")
async def list_model_discovery_runs(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, provider, status, discovered_count, active_count, error, details, triggered_by, reason, created_at
            FROM llm_model_discovery_runs
            ORDER BY created_at DESC, id DESC
            LIMIT $1
            """,
            limit,
        )
    runs = [
        {
            "id": row["id"],
            "provider": row["provider"],
            "status": row["status"],
            "discovered_count": row["discovered_count"],
            "active_count": row["active_count"],
            "error": row["error"],
            "details": _coerce_json_object(row["details"]),
            "triggered_by": row["triggered_by"],
            "reason": row["reason"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]
    return {"runs": runs, "total": len(runs)}


@router.get("/providers/{provider}/timeline")
async def get_provider_timeline(provider: str, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    normalized = normalize_provider(provider)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, provider, key_name, event_type, actor, details, created_at
            FROM llm_key_audit_logs
            WHERE provider = $1
               OR event_type = 'registry_sync'
            ORDER BY created_at DESC
            LIMIT $2
            """,
            normalized,
            limit,
        )
    timeline = [
        {
            "id": row["id"],
            "provider": row["provider"],
            "key_name": row["key_name"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "details": _coerce_json_object(row["details"]),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]
    return {"timeline": timeline, "total": len(timeline)}


@router.get("/chat-preferences")
async def get_chat_model_preferences() -> dict[str, Any]:
    await _ensure_chat_model_preferences_table()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT preference_key, provider, model_id, display_order, is_hidden, is_favorite, is_pinned, updated_at, updated_by
            FROM chat_model_preferences
            ORDER BY is_pinned DESC, is_favorite DESC, display_order ASC, provider ASC, model_id ASC
            """
        )
    preferences = [
        {
            "preference_key": row["preference_key"],
            "provider": row["provider"],
            "model_id": row["model_id"],
            "display_order": row["display_order"],
            "is_hidden": row["is_hidden"],
            "is_favorite": row["is_favorite"],
            "is_pinned": row["is_pinned"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "updated_by": row["updated_by"],
        }
        for row in rows
    ]
    return {"preferences": preferences, "total": len(preferences)}


@router.put("/chat-preferences")
async def update_chat_model_preferences(items: list[ChatModelPreferenceInput]) -> dict[str, Any]:
    await _ensure_chat_model_preferences_table()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in items:
                preference_key, provider, model_id = _build_chat_preference_key(
                    item.model_id,
                    provider=item.provider,
                    preference_key=item.preference_key,
                )
                await conn.execute(
                    """
                    INSERT INTO chat_model_preferences (
                        preference_key, provider, model_id, display_order, is_hidden, is_favorite, is_pinned, updated_at, updated_by
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), 'settings_ui')
                    ON CONFLICT (preference_key)
                    DO UPDATE SET
                        provider = EXCLUDED.provider,
                        model_id = EXCLUDED.model_id,
                        display_order = EXCLUDED.display_order,
                        is_hidden = EXCLUDED.is_hidden,
                        is_favorite = EXCLUDED.is_favorite,
                        is_pinned = EXCLUDED.is_pinned,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by
                    """,
                    preference_key,
                    provider,
                    model_id,
                    item.display_order,
                    item.is_hidden,
                    item.is_favorite,
                    item.is_pinned,
                )
        rows = await conn.fetch(
            """
            SELECT preference_key, provider, model_id, display_order, is_hidden, is_favorite, is_pinned, updated_at, updated_by
            FROM chat_model_preferences
            ORDER BY is_pinned DESC, is_favorite DESC, display_order ASC, provider ASC, model_id ASC
            """
        )
    preferences = [
        {
            "preference_key": row["preference_key"],
            "provider": row["provider"],
            "model_id": row["model_id"],
            "display_order": row["display_order"],
            "is_hidden": row["is_hidden"],
            "is_favorite": row["is_favorite"],
            "is_pinned": row["is_pinned"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "updated_by": row["updated_by"],
        }
        for row in rows
    ]
    return {"ok": True, "preferences": preferences, "total": len(preferences)}


@router.post("/sync")
async def sync_llm_models() -> dict[str, Any]:
    return await sync_model_registry(triggered_by="llm_models_api", reason="manual_api")
