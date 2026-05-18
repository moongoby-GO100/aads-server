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


class ModelRoutingPreferenceInput(BaseModel):
    route_key: str = Field(..., pattern=r"^(image|edit_image|video|llm|music|audio|runner_llm)$")
    provider: str
    model_id: str
    display_order: int = Field(100, ge=0)
    is_enabled: bool = True
    is_default: bool = False
    notes: str | None = None


class ModelRoutingPreferencesUpdate(BaseModel):
    preferences: list[ModelRoutingPreferenceInput]


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


async def _seed_media_models(conn) -> None:
    """미디어 모델 시드 — ON CONFLICT DO NOTHING으로 멱등 실행."""
    await conn.execute("""
        INSERT INTO llm_models
          (provider, model_id, display_name, family, category,
           execution_model_id, is_active, discovery_source)
        VALUES
          ('openai','dall-e-3','DALL-E 3','dall-e','media_image','dall-e-3',true,'manual'),
          ('openai','dall-e-2','DALL-E 2','dall-e','media_image','dall-e-2',false,'manual'),
          ('black-forest-labs','flux-1.1-pro','Flux 1.1 Pro','flux','media_image','flux-1.1-pro',true,'manual'),
          ('black-forest-labs','flux-dev','Flux Dev','flux','media_image','flux-dev',false,'manual'),
          ('stability','stable-diffusion-3-5','Stable Diffusion 3.5','stable-diffusion','media_image','stable-diffusion-3-5',true,'manual'),
          ('stability','stable-diffusion-xl','Stable Diffusion XL','stable-diffusion','media_image','stable-diffusion-xl',false,'manual'),
          ('gemini','gemini-2.5-flash-image','Nano Banana','gemini-image','media_image','gemini-2.5-flash-image',true,'manual'),
          ('gemini','gemini-3.1-flash-image-preview','Nano Banana 2','gemini-image','media_image','gemini-3.1-flash-image-preview',true,'manual'),
          ('gemini','gemini-3-pro-image-preview','Nano Banana Pro','gemini-image','media_image','gemini-3-pro-image-preview',true,'manual'),
          ('google','imagen-4.0-generate-001','Imagen 4.0','imagen','media_image','imagen-4.0-generate-001',true,'manual'),
          ('google','imagen-4.0-fast-generate-001','Imagen 4 Fast','imagen','media_image','imagen-4.0-fast-generate-001',true,'manual'),
          ('google','imagen-4.0-ultra-generate-001','Imagen 4 Ultra','imagen','media_image','imagen-4.0-ultra-generate-001',true,'manual'),
          ('runway','gen-4-turbo','Runway Gen-4 Turbo','gen','media_video','gen-4-turbo',true,'manual'),
          ('runway','gen-3-alpha','Runway Gen-3 Alpha','gen','media_video','gen-3-alpha',false,'manual'),
          ('kling','kling-2.0','Kling 2.0','kling','media_video','kling-2.0',true,'manual'),
          ('pika','pika-2.2','Pika 2.2','pika','media_video','pika-2.2',true,'manual'),
          ('google','veo-3.0-generate-preview','Veo 3.0 Preview','veo','media_video','veo-3.0-generate-preview',true,'manual'),
          ('suno','suno-v4','Suno v4','suno','media_music','suno-v4',true,'manual'),
          ('udio','udio-3.0','Udio 3.0','udio','media_music','udio-3.0',true,'manual'),
          ('meta','musicgen-large','MusicGen Large','musicgen','media_music','musicgen-large',false,'manual'),
          ('meta','musicgen-melody','MusicGen Melody','musicgen','media_music','musicgen-melody',false,'manual'),
          ('elevenlabs','eleven-v3','ElevenLabs v3','eleven','media_audio','eleven-v3',true,'manual'),
          ('openai','tts-1-hd','OpenAI TTS HD','tts','media_audio','tts-1-hd',true,'manual'),
          ('openai','tts-1','OpenAI TTS','tts','media_audio','tts-1',false,'manual'),
          ('google','google-wavenet-tts','Google WaveNet TTS','wavenet','media_audio','google-wavenet-tts',false,'manual')
        ON CONFLICT (provider, model_id) DO NOTHING
    """)
    await conn.execute("""
        INSERT INTO model_routing_preferences
          (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_by)
        VALUES
          ('image','openai','dall-e-3',10,false,false,'OpenAI route disabled by default; enable only when API key is active','system'),
          ('image','gemini','gemini-2.5-flash-image',15,true,false,'Nano Banana','system'),
          ('image','gemini','gemini-3.1-flash-image-preview',18,true,false,'Nano Banana 2','system'),
          ('image','gemini','gemini-3-pro-image-preview',19,true,false,'Nano Banana Pro','system'),
          ('image','black-forest-labs','flux-1.1-pro',20,true,false,'Flux 1.1 Pro','system'),
          ('image','stability','stable-diffusion-3-5',30,true,false,'SD 3.5','system'),
          ('image','google','imagen-4.0-generate-001',40,true,true,'Imagen 4.0 default route','system'),
          ('image','google','imagen-4.0-fast-generate-001',41,true,false,'Imagen 4 Fast','system'),
          ('image','google','imagen-4.0-ultra-generate-001',42,true,false,'Imagen 4 Ultra','system'),
          ('video','runway','gen-4-turbo',10,true,false,'Gen-4 Turbo','system'),
          ('video','kling','kling-2.0',20,true,false,'Kling 2.0','system'),
          ('video','pika','pika-2.2',30,true,false,'Pika 2.2','system'),
          ('video','google','veo-3.0-generate-preview',40,true,false,'Veo 3.0','system'),
          ('music','suno','suno-v4',10,true,true,'Suno v4','system'),
          ('music','udio','udio-3.0',20,true,false,'Udio 3.0','system'),
          ('music','meta','musicgen-large',30,false,false,'MusicGen Large','system'),
          ('audio','elevenlabs','eleven-v3',10,true,true,'ElevenLabs v3','system'),
          ('audio','openai','tts-1-hd',20,true,false,'OpenAI TTS HD','system'),
          ('audio','google','google-wavenet-tts',30,false,false,'WaveNet TTS','system'),
          ('runner_llm','anthropic','claude-opus-4-7',10,true,true,'Runner 기본','system'),
          ('runner_llm','openai','gpt-5.5',20,true,false,'GPT-5.5 백업','system'),
          ('runner_llm','google','gemini-2.5-pro',30,true,false,'Gemini 2.5 Pro 백업','system')
        ON CONFLICT (route_key, provider, model_id) DO NOTHING
    """)


async def _ensure_model_routing_preferences_table() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_routing_preferences (
                route_key TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 100,
                is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by TEXT NOT NULL DEFAULT 'system',
                PRIMARY KEY (route_key, provider, model_id),
                CONSTRAINT model_routing_preferences_route_key_chk
                    CHECK (route_key IN ('image', 'edit_image', 'video', 'llm'))
            )
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_routing_preferences_one_default
            ON model_routing_preferences(route_key)
            WHERE is_default = TRUE
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_model_routing_preferences_order
            ON model_routing_preferences(route_key, is_default DESC, is_enabled DESC, display_order ASC)
            """
        )
        await conn.execute(
            'ALTER TABLE model_routing_preferences '
            'DROP CONSTRAINT IF EXISTS model_routing_preferences_route_key_chk'
        )
        await conn.execute(
            "ALTER TABLE model_routing_preferences "
            "ADD CONSTRAINT model_routing_preferences_route_key_chk "
            "CHECK (route_key IN "
            "('image','edit_image','video','llm','music','audio','runner_llm'))"
        )
        await conn.execute(
            'ALTER TABLE model_routing_preferences ADD COLUMN IF NOT EXISTS display_name TEXT'
        )
        await conn.execute(
            'ALTER TABLE model_routing_preferences ADD COLUMN IF NOT EXISTS family TEXT'
        )
        await conn.execute(
            'ALTER TABLE model_routing_preferences ADD COLUMN IF NOT EXISTS category TEXT'
        )
        await _seed_media_models(conn)


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


def _routing_availability(row: Any) -> str:
    if not row["is_enabled"]:
        return "disabled"
    if not row["registry_model_id"]:
        return "not_registered"
    if row["is_active"] or row["is_executable"]:
        return "available"
    status = str(row["verification_status"] or "").strip().lower()
    if status == "review_required":
        return "review_required"
    return "not_configured"


def _routing_preference_payload(row: Any) -> dict[str, Any]:
    metadata = _coerce_json_object(row["metadata"])
    capabilities = _coerce_json_object(row["capabilities"])
    pricing = _coerce_json_object(row["pricing"])
    availability = _routing_availability(row)
    note = str(row["notes"] or "").strip()
    if not note:
        note = str(
            metadata.get("routing_note")
            or metadata.get("availability_note")
            or metadata.get("discovery_requirement")
            or ""
        ).strip()
    if not note and availability == "not_registered":
        note = "llm_models registry row is missing"
    elif not note and availability == "not_configured":
        note = "provider key or executable runtime has not been verified"
    return {
        "route_key": row["route_key"],
        "provider": row["provider"],
        "model_id": row["model_id"],
        "display_name": row["display_name"] or row["model_id"],
        "execution_model_id": row["execution_model_id"],
        "family": row["family"],
        "category": row["category"],
        "display_order": row["display_order"],
        "is_enabled": row["is_enabled"],
        "is_default": row["is_default"],
        "is_active": row["is_active"],
        "is_selectable": row["is_selectable"],
        "is_executable": row["is_executable"],
        "availability": availability,
        "verification_status": row["verification_status"],
        "notes": note,
        "metadata": metadata,
        "capabilities": capabilities,
        "pricing": pricing,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "updated_by": row["updated_by"],
    }


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
        "runtime_executable_provider_count": sum(1 for row in summaries if row.get("runtime_executable")),
        "auto_discovery_supported_provider_count": sum(1 for row in summaries if row.get("auto_discovery_supported")),
        "template_runtime_only_providers": [
            row["provider"]
            for row in summaries
            if row.get("runtime_executable") and not row.get("auto_discovery_supported")
        ],
        "rate_limited_provider_count": sum(1 for row in summaries if row.get("status") == "rate_limited"),
        "review_required_providers": [row["provider"] for row in summaries if row.get("requires_admin_review")],
        "last_sync_at": last_sync["created_at"] if last_sync else None,
        "last_sync_reason": (last_sync["details"].get("reason") if last_sync else None),
        "last_sync_actor": (last_sync["actor"] if last_sync else None),
        "normalized_providers": (last_sync["details"].get("normalized_providers") if last_sync else {}) or {},
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


@router.get("/routing-preferences")
async def get_model_routing_preferences() -> dict[str, Any]:
    await _ensure_model_routing_preferences_table()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pref.route_key, pref.provider, pref.model_id,
                   pref.display_order, pref.is_enabled, pref.is_default,
                   pref.notes, pref.updated_at, pref.updated_by,
                   models.model_id AS registry_model_id,
                   models.display_name, models.family, models.category,
                   models.execution_model_id, models.is_active,
                   models.is_selectable, models.is_executable,
                   models.verification_status, models.metadata,
                   models.capabilities, models.pricing
            FROM model_routing_preferences AS pref
            LEFT JOIN LATERAL (
                SELECT m.model_id, m.display_name, m.family, m.category,
                       m.execution_model_id, m.is_active, m.is_selectable,
                       m.is_executable, m.verification_status, m.metadata,
                       m.capabilities, m.pricing, m.updated_at, m.id
                FROM llm_models AS m
                WHERE m.provider = pref.provider
                  AND (m.model_id = pref.model_id OR m.execution_model_id = pref.model_id)
                ORDER BY CASE WHEN m.model_id = pref.model_id THEN 0 ELSE 1 END,
                         m.updated_at DESC NULLS LAST, m.id DESC
                LIMIT 1
            ) AS models ON TRUE
            ORDER BY CASE pref.route_key
                         WHEN 'image' THEN 1
                         WHEN 'edit_image' THEN 2
                         WHEN 'video' THEN 3
                         WHEN 'llm' THEN 4
                         WHEN 'music' THEN 5
                         WHEN 'audio' THEN 6
                         WHEN 'runner_llm' THEN 7
                         ELSE 99
                     END,
                     pref.is_default DESC,
                     pref.is_enabled DESC,
                     pref.display_order ASC,
                     pref.provider ASC,
                     pref.model_id ASC
            """
        )
    preferences = [_routing_preference_payload(row) for row in rows]
    route_counts: dict[str, int] = {}
    default_models: dict[str, str] = {}
    for item in preferences:
        route_key = str(item["route_key"])
        route_counts[route_key] = route_counts.get(route_key, 0) + 1
        if item["is_default"]:
            default_models[route_key] = item["model_id"]
    return {
        "preferences": preferences,
        "total": len(preferences),
        "route_counts": route_counts,
        "default_models": default_models,
    }


@router.put("/routing-preferences")
async def update_model_routing_preferences(req: ModelRoutingPreferencesUpdate) -> dict[str, Any]:
    await _ensure_model_routing_preferences_table()
    await _ensure_chat_model_preferences_table()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in req.preferences:
                provider = (
                    normalize_provider(item.provider)
                    if item.route_key in ('llm', 'runner_llm')
                    else item.provider.strip().lower()
                )
                model_id = item.model_id.strip()
                if item.is_default:
                    await conn.execute(
                        """
                        UPDATE model_routing_preferences
                        SET is_default = FALSE, updated_at = NOW(), updated_by = 'settings_ui'
                        WHERE route_key = $1 AND is_default = TRUE
                        """,
                        item.route_key,
                    )
                await conn.execute(
                    """
                    INSERT INTO model_routing_preferences (
                        route_key, provider, model_id, display_order,
                        is_enabled, is_default, notes, updated_at, updated_by
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), 'settings_ui')
                    ON CONFLICT (route_key, provider, model_id)
                    DO UPDATE SET
                        display_order = EXCLUDED.display_order,
                        is_enabled = EXCLUDED.is_enabled,
                        is_default = EXCLUDED.is_default,
                        notes = EXCLUDED.notes,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by
                    """,
                    item.route_key,
                    provider,
                    model_id,
                    item.display_order,
                    item.is_enabled,
                    item.is_default,
                    item.notes or "",
                )
                if item.route_key == "llm" and item.is_default:
                    preference_key, pref_provider, pref_model = _build_chat_preference_key(
                        model_id,
                        provider=provider,
                    )
                    await conn.execute(
                        """
                        INSERT INTO chat_model_preferences (
                            preference_key, provider, model_id, display_order,
                            is_hidden, is_favorite, is_pinned, updated_at, updated_by
                        )
                        VALUES ($1, $2, $3, 0, FALSE, TRUE, TRUE, NOW(), 'settings_ui')
                        ON CONFLICT (preference_key)
                        DO UPDATE SET
                            provider = EXCLUDED.provider,
                            model_id = EXCLUDED.model_id,
                            display_order = EXCLUDED.display_order,
                            is_hidden = FALSE,
                            is_favorite = TRUE,
                            is_pinned = TRUE,
                            updated_at = NOW(),
                            updated_by = EXCLUDED.updated_by
                        """,
                        preference_key,
                        pref_provider,
                        pref_model,
                    )
    return await get_model_routing_preferences()


@router.post("/sync")
async def sync_llm_models() -> dict[str, Any]:
    return await sync_model_registry(triggered_by="llm_models_api", reason="manual_api")
