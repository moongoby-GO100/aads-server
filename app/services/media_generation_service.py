"""
Common media generation service for image, image edit, and video jobs.

The service keeps the legacy image response shape while recording every media
request through the shared media_generation_jobs contract when the DB pool is
available. Video providers are intentionally graceful at P0: unsupported or
unconfigured adapters return structured job errors instead of raising.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import httpx
import logging

try:
    from app.config import settings as app_settings
except Exception:
    app_settings = SimpleNamespace(OPENAI_API_KEY="", GOOGLE_API_KEY="")

logger = logging.getLogger(__name__)

IMAGE_MODELS = (
    "gpt-image-2",
    "gpt-image-1",
    "dall-e-3",
    "nano-banana-2",
    "imagen-4.0-generate-001",
    "imagen-4.0-fast-generate-001",
    "imagen-4.0-ultra-generate-001",
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
    "kling-v1",
    "kling-v1-5",
    "kling-v2",
    "kling-v2-1",
    "kling-v2-new",
    "genspark-image-ui",
)
VIDEO_MODELS = (
    "sora-2",
    "sora-2-pro",
    "veo-3.1-generate-preview",
    "kling-2.0",
    "kling-v1",
    "kling-v1-5",
    "kling-v1-6",
    "kling-v2",
    "kling-v2-1",
    "kling-v2-5",
    "kling-v3",
    "genspark-video-ui",
)
LLM_ROUTING_MODELS = (
    "gpt-5.5",
    "claude-opus-4-8",
    "gemini-3.1-pro-preview",
)

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
VALID_JOB_KINDS = {"image", "edit_image", "video", "music", "model_3d"}
VALID_JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}


def _is_imagen_4_model(model_id: str) -> bool:
    return str(model_id or "").strip().lower().startswith("imagen-4.0-")


def _canonical_media_model_id(model_id: str) -> str:
    normalized = str(model_id or "").strip().lower()
    aliases = {
        "nano-banana-2": "gemini-3.1-flash-image-preview",
        "nano_banana_2": "gemini-3.1-flash-image-preview",
        "nano banana 2": "gemini-3.1-flash-image-preview",
        "gemini-image-proxy": "gemini-3.1-flash-image-preview",
        "gemini-3.1-pro-image-preview": "gemini-3-pro-image-preview",
        "gemini-3.1-pro-preview-image": "gemini-3-pro-image-preview",
        "gemini-3-pro-image": "gemini-3-pro-image-preview",
        "genspark": "genspark-image-ui",
        "genspark-ui": "genspark-image-ui",
        "genspark_image_ui": "genspark-image-ui",
        "genspark-video": "genspark-video-ui",
        "genspark_video_ui": "genspark-video-ui",
    }
    return aliases.get(normalized, str(model_id or "").strip())


@dataclass(frozen=True)
class MediaRoute:
    kind: str
    provider: str
    model_id: str
    configured: bool
    supported: bool
    enabled: bool = True
    availability: str = "available"
    source: str = "fallback"
    reason: str = ""


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _as_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=_json_default)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if value is None:
        return {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _app_static_dir() -> Path:
    configured = os.getenv("AADS_MEDIA_STATIC_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "static"


def _safe_job_filename(job_id: str, ext: str) -> str:
    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(job_id or "media"))
    safe_ext = ext if ext.startswith(".") else f".{ext}"
    return f"{safe_job_id}{safe_ext}"


def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        pass
    if host.lower() in {"localhost"} or host.lower().endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    resolved = {info[4][0] for info in infos if info and info[4]}
    if not resolved:
        return False
    try:
        return all(ipaddress.ip_address(addr).is_global for addr in resolved)
    except ValueError:
        return False


def _secret_value(settings_obj: Any, name: str) -> str:
    value = getattr(settings_obj, name, "") or ""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    return str(value or "")


def _sanitize_prompt(prompt: str) -> str:
    text = str(prompt or "")[:1000]
    text = re.sub(r"\b(brand|logo|trademark|celebrity|famous)\b", "", text, flags=re.I)
    text = " ".join(text.split())
    return text or "digital art, clean background"


def _normalize_job_row(row: Any) -> dict[str, Any]:
    if not row:
        return {}
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        try:
            data = dict(row)
        except Exception:
            data = {key: row[key] for key in getattr(row, "keys", lambda: [])()}
    for key in ("input_refs", "result_metadata"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = {}
            data[key] = parsed if isinstance(parsed, dict) else {}
        elif value is None:
            data[key] = {}
    return data


def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(job)
    for key in ("created_at", "updated_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, (datetime, date)):
            data[key] = value.isoformat()
    return data


class MediaGenerationService:
    def __init__(
        self,
        *,
        settings_obj: Any = app_settings,
        pool_provider: Callable[[], Any] | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        self.settings = settings_obj
        self._pool_provider = pool_provider
        self.output_dir = Path(
            output_dir or os.getenv("AADS_MEDIA_OUTPUT_DIR", "/tmp/aads-media")
        )

    @staticmethod
    def recognize_model(model_id: str) -> dict[str, str]:
        model = _canonical_media_model_id(model_id)
        lowered = model.lower()
        if lowered in {"gpt-image-2", "gpt-image-1", "dall-e-3"}:
            return {"kind": "image", "provider": "openai", "model_id": model}
        if _is_imagen_4_model(lowered):
            return {"kind": "image", "provider": "google", "model_id": model}
        if lowered in {"gemini-3.1-flash-image-preview", "gemini-3-pro-image-preview", "gemini-2.5-flash-image"}:
            return {"kind": "image", "provider": "gemini", "model_id": model}
        if lowered in {"sora-2", "sora-2-pro"}:
            return {"kind": "video", "provider": "openai", "model_id": model}
        if lowered == "veo-3.1-generate-preview":
            return {"kind": "video", "provider": "google", "model_id": model}
        if lowered.startswith("kling-") or lowered.startswith("kling-v"):
            return {"kind": "video", "provider": "kling", "model_id": model}
        if lowered == "genspark-image-ui":
            return {"kind": "image", "provider": "genspark_ui", "model_id": model}
        if lowered == "genspark-video-ui":
            return {"kind": "video", "provider": "genspark_ui", "model_id": model}
        if lowered == "gpt-5.5":
            return {"kind": "llm", "provider": "codex", "model_id": model}
        if lowered == "claude-opus-4-8":
            return {"kind": "llm", "provider": "anthropic", "model_id": model}
        if lowered == "gemini-3.1-pro-preview":
            return {"kind": "llm", "provider": "gemini", "model_id": model}
        return {"kind": "", "provider": "", "model_id": model}

    def _get_pool_or_none(self) -> Any | None:
        try:
            if self._pool_provider is not None:
                return self._pool_provider()
            from app.core.db_pool import get_pool

            return get_pool()
        except Exception:
            return None

    async def _fetch_model_route(self, model_id: str) -> dict[str, Any] | None:
        pool = self._get_pool_or_none()
        if not pool or not model_id:
            return None
        try:
            async with pool.acquire() as conn:
                select_columns = """
                    provider, model_id, execution_model_id, execution_backend,
                    is_active, is_selectable, is_executable,
                    verification_status, metadata, capabilities
                """
                row = await conn.fetchrow(
                    f"""
                    SELECT {select_columns}
                    FROM llm_models
                    WHERE (model_id = $1 OR execution_model_id = $1)
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    model_id,
                )
                if row:
                    return _normalize_job_row(row)

                if _is_imagen_4_model(model_id):
                    row = await conn.fetchrow(
                        f"""
                        SELECT {select_columns}
                        FROM llm_models
                        WHERE provider = 'google'
                          AND (
                              capabilities->>'prefix_family' = 'imagen-4.0-*'
                              OR model_id LIKE 'imagen-4.0-%'
                          )
                        ORDER BY CASE WHEN model_id = 'imagen-4.0-generate-001' THEN 0 ELSE 1 END,
                                 is_active DESC, is_executable DESC,
                                 updated_at DESC NULLS LAST, id DESC
                        LIMIT 1
                        """
                    )
                    if row:
                        data = _normalize_job_row(row)
                        data["matched_by_prefix"] = True
                        data["requested_model_id"] = model_id
                        return data
                return None
        except Exception:
            return None

    async def _fetch_default_route(self, kind: str) -> dict[str, Any] | None:
        pool = self._get_pool_or_none()
        if not pool:
            return None
        route_key = str(kind or "").strip()
        if not route_key:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT pref.route_key, pref.provider, pref.model_id,
                           pref.display_order, pref.is_enabled, pref.is_default,
                           pref.notes, pref.updated_at, pref.updated_by,
                           models.execution_model_id, models.execution_backend,
                           models.is_active, models.is_selectable, models.is_executable,
                           models.verification_status, models.metadata, models.capabilities
                    FROM model_routing_preferences AS pref
                    LEFT JOIN llm_models AS models
                      ON models.provider = pref.provider
                     AND (models.model_id = pref.model_id OR models.execution_model_id = pref.model_id)
                    WHERE pref.route_key = $1
                    ORDER BY pref.is_default DESC, pref.is_enabled DESC,
                             pref.display_order ASC, pref.updated_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    route_key,
                )
                return _normalize_job_row(row) if row else None
        except Exception:
            return None

    def _provider_configured(self, provider: str) -> bool:
        normalized = str(provider or "").lower()
        if normalized in {"pc_local", "local_pc", "local", "ceo_pc", "pc_agent"}:
            return True
        if normalized in {"genspark_ui", "genspark", "genspark_web"}:
            return True
        if normalized == "openai":
            return bool(_secret_value(self.settings, "OPENAI_API_KEY"))
        if normalized == "google":
            return bool(_secret_value(self.settings, "GOOGLE_API_KEY"))
        if normalized == "gemini":
            return bool(
                os.getenv("LITELLM_BASE_URL")
                or os.getenv("LITELLM_MASTER_KEY")
                or _secret_value(self.settings, "GOOGLE_API_KEY")
            )
        if normalized == "kling":
            return bool(
                os.getenv("KLING_ACCESS_KEY")
                and os.getenv("KLING_SECRET_KEY")
            )
        return False

    async def _kling_configured(self) -> bool:
        access_key, secret_key = await self._get_kling_credentials()
        return bool(access_key and secret_key)

    def _default_model_for(self, kind: str, provider: str | None = None) -> str:
        if provider in {"genspark_ui", "genspark", "genspark_web"}:
            return "genspark-image-ui" if kind == "image" else "genspark-video-ui"
        if kind == "video":
            if provider == "google":
                return "veo-3.1-generate-preview"
            return "sora-2"
        if provider == "openai":
            return "gpt-image-2"
        if provider == "gemini":
            return "gemini-3.1-flash-image-preview"
        return "imagen-4.0-generate-001"

    @staticmethod
    def _db_route_enabled(row: Mapping[str, Any] | None) -> bool:
        if not row:
            return True
        if row.get("is_enabled") is False:
            return False
        if row.get("is_selectable") is False:
            return False
        metadata = _as_dict(row.get("metadata"))
        if metadata.get("disabled") is True:
            return False
        return True

    @staticmethod
    def _db_route_note(row: Mapping[str, Any] | None) -> str:
        if not row:
            return ""
        metadata = _as_dict(row.get("metadata"))
        return str(
            row.get("notes")
            or metadata.get("routing_note")
            or metadata.get("availability_note")
            or metadata.get("discovery_requirement")
            or row.get("verification_status")
            or ""
        ).strip()

    async def resolve_route(
        self,
        kind: str,
        *,
        model_id: str | None = None,
        provider: str | None = None,
    ) -> MediaRoute:
        normalized_kind = "image" if kind == "edit_image" else str(kind or "").strip()
        requested_model = _canonical_media_model_id(model_id or "")
        requested_provider = str(provider or "").strip().lower()
        explicit_request = bool(requested_model or requested_provider)
        source = "explicit" if explicit_request else "fallback"
        enabled = True
        route_note = ""

        local_item: dict[str, Any] | None = None
        local_provider_requested = requested_provider in {"pc_local", "local_pc", "local", "ceo_pc", "pc_agent"}
        genspark_provider_requested = requested_provider in {"genspark_ui", "genspark", "genspark_web"}
        try:
            from app.services.local_model_manager import LOCAL_PROVIDER_ALIASES, local_model_manager

            local_provider_requested = requested_provider in LOCAL_PROVIDER_ALIASES
            if local_provider_requested or requested_model:
                local_item = local_model_manager.resolve_media_model(
                    kind=normalized_kind,
                    model_id=requested_model,
                    provider=requested_provider,
                )
        except Exception:
            local_item = None

        if local_item:
            requested_provider = "pc_local"
            requested_model = str(local_item.get("model") or requested_model).strip()
            source = "local_queue"
        elif local_provider_requested:
            return MediaRoute(
                kind=kind,
                provider="pc_local",
                model_id=requested_model or normalized_kind,
                configured=True,
                supported=False,
                enabled=True,
                availability="queued_model_not_found",
                source="local_queue",
                reason="requested local model is not present in scripts/local_model_install_queue.json",
            )

        db_route = await self._fetch_model_route(requested_model) if requested_model and not local_item else None
        if db_route:
            requested_provider = str(requested_provider or db_route.get("provider") or "").strip().lower()
            if not db_route.get("matched_by_prefix"):
                requested_model = str(
                    db_route.get("execution_model_id") or db_route.get("model_id") or requested_model
                ).strip()
            enabled = self._db_route_enabled(db_route)
            route_note = self._db_route_note(db_route)
        elif not explicit_request:
            default_route = await self._fetch_default_route(str(kind or "").strip() or normalized_kind)
            if default_route:
                requested_provider = str(default_route.get("provider") or "").strip().lower()
                requested_model = str(
                    default_route.get("execution_model_id")
                    or default_route.get("model_id")
                    or ""
                ).strip()
                enabled = self._db_route_enabled(default_route)
                route_note = self._db_route_note(default_route)
                source = "db_default"

        inferred = self.recognize_model(requested_model)
        if not requested_provider:
            requested_provider = inferred.get("provider", "")
        if not requested_model and normalized_kind == "image" and not requested_provider:
            if _secret_value(self.settings, "GOOGLE_API_KEY"):
                requested_provider = "google"
            elif _secret_value(self.settings, "OPENAI_API_KEY"):
                requested_provider = "openai"

        if not requested_model:
            requested_model = self._default_model_for(normalized_kind, requested_provider or None)
            inferred = self.recognize_model(requested_model)
            requested_provider = requested_provider or inferred.get("provider", "")

        if requested_provider in {"local", "local_pc", "ceo_pc", "pc_agent"}:
            requested_provider = "pc_local"
        if requested_provider in {"genspark", "genspark_web"}:
            requested_provider = "genspark_ui"
        if not requested_provider:
            requested_provider = "google" if _secret_value(self.settings, "GOOGLE_API_KEY") else "openai"
        if genspark_provider_requested and requested_model not in {"genspark-image-ui", "genspark-video-ui"}:
            requested_model = self._default_model_for(normalized_kind, "genspark_ui")

        configured = self._provider_configured(requested_provider)
        if requested_provider == "kling" and not configured:
            configured = await self._kling_configured()
        supported = self._route_supported(kind, requested_provider, requested_model)
        reason = ""
        if not enabled:
            reason = route_note or f"{requested_provider}:{requested_model} is disabled by DB routing configuration"
            availability = "disabled"
        elif not configured:
            reason = f"{requested_provider or 'provider'} credentials are not configured"
            availability = "not_configured"
        elif not supported:
            reason = f"{requested_provider}:{requested_model} is not available in the P0 adapter"
            availability = "adapter_unavailable"
        else:
            availability = "available"
        if not explicit_request and kind == "image" and availability != "available" and availability != "disabled":
            for fallback_provider, fallback_model in (
                ("google", "imagen-4.0-generate-001"),
                ("google", "imagen-4.0-fast-generate-001"),
                ("google", "imagen-4.0-ultra-generate-001"),
                ("gemini", "gemini-3.1-flash-image-preview"),
                ("gemini", "gemini-2.5-flash-image"),
                ("gemini", "gemini-3-pro-image-preview"),
            ):
                if self._provider_configured(fallback_provider) and self._route_supported(
                    kind, fallback_provider, fallback_model
                ):
                    requested_provider = fallback_provider
                    requested_model = fallback_model
                    configured = True
                    supported = True
                    enabled = True
                    availability = "available"
                    source = "builtin_fallback"
                    reason = ""
                    break
        return MediaRoute(
            kind=kind,
            provider=requested_provider,
            model_id=requested_model,
            configured=configured,
            supported=supported,
            enabled=enabled,
            availability=availability,
            source=source,
            reason=reason,
        )

    def _route_supported(self, kind: str, provider: str, model_id: str) -> bool:
        model_id = _canonical_media_model_id(model_id)
        if kind == "image":
            if provider == "pc_local":
                return True
            if provider == "genspark_ui":
                return model_id == "genspark-image-ui"
            if provider == "kling":
                return model_id in {
                    "kling-v1",
                    "kling-v1-5",
                    "kling-v2",
                    "kling-v2-1",
                    "kling-v2-new",
                    "kling-2.0",
                }
            if provider == "gemini" and model_id in {
                "gemini-3.1-flash-image-preview",
                "gemini-3-pro-image-preview",
                "gemini-2.5-flash-image",
            }:
                return True
            return provider in {"openai", "google"} and (
                model_id in {"gpt-image-2", "gpt-image-1", "dall-e-3"}
                or _is_imagen_4_model(model_id)
            )
        if kind == "edit_image":
            if provider == "pc_local":
                return True
            if provider == "genspark_ui":
                return model_id == "genspark-image-ui"
            return provider == "openai" and model_id in {"gpt-image-2", "gpt-image-1"}
        if kind == "video":
            if provider == "pc_local":
                return True
            if provider == "genspark_ui":
                return model_id == "genspark-video-ui"
            if provider == "kling":
                return model_id in {
                    "kling-2.0",
                    "kling-v1",
                    "kling-v1-5",
                    "kling-v1-6",
                    "kling-v2",
                    "kling-v2-1",
                    "kling-v2-5",
                    "kling-v3",
                }
            return False
        if kind in {"music", "model_3d"}:
            return provider == "pc_local"
        return False

    async def _insert_job(
        self,
        *,
        kind: str,
        provider: str,
        model_id: str,
        prompt: str,
        input_refs: dict[str, Any] | None = None,
        status: str = "queued",
        requested_by: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in VALID_JOB_KINDS:
            raise ValueError(f"invalid media job kind: {kind}")
        if status not in VALID_JOB_STATUSES:
            raise ValueError(f"invalid media job status: {status}")
        job_id = f"media-{uuid.uuid4().hex[:16]}"
        fallback = {
            "id": None,
            "job_id": job_id,
            "kind": kind,
            "provider": provider,
            "model_id": model_id,
            "prompt": prompt,
            "input_refs": input_refs or {},
            "status": status,
            "result_uri": None,
            "result_path": None,
            "result_metadata": {},
            "error_message": None,
            "requested_by": requested_by,
            "session_id": session_id,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "completed_at": None,
            "storage": "ephemeral",
        }
        pool = self._get_pool_or_none()
        if not pool:
            return fallback
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO media_generation_jobs (
                        job_id, kind, provider, model_id, prompt, input_refs, status,
                        requested_by, session_id, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, NOW(), NOW())
                    RETURNING id, job_id, kind, provider, model_id, prompt, input_refs, status,
                              result_uri, result_path, result_metadata, error_message,
                              requested_by, session_id, created_at, updated_at, completed_at
                    """,
                    job_id,
                    kind,
                    provider,
                    model_id,
                    prompt,
                    _as_json(input_refs),
                    status,
                    requested_by,
                    session_id,
                )
                data = _normalize_job_row(row)
                data["storage"] = "db"
                return data
        except Exception as exc:
            fallback["storage_error"] = str(exc)
            return fallback

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        result_uri: str | None = None,
        result_path: str | None = None,
        result_metadata: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if status not in VALID_JOB_STATUSES:
            raise ValueError(f"invalid media job status: {status}")
        pool = self._get_pool_or_none()
        if not pool:
            return {
                "job_id": job_id,
                "status": status,
                "result_uri": result_uri,
                "result_path": result_path,
                "result_metadata": result_metadata or {},
                "error_message": error_message,
                "storage": "ephemeral",
            }
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE media_generation_jobs
                    SET status = $2,
                        result_uri = COALESCE($3, result_uri),
                        result_path = COALESCE($4, result_path),
                        result_metadata = COALESCE($5::jsonb, result_metadata, '{}'::jsonb),
                        error_message = $6,
                        updated_at = NOW(),
                        completed_at = CASE
                            WHEN $2 = ANY($7::text[]) THEN COALESCE(completed_at, NOW())
                            ELSE completed_at
                        END
                    WHERE job_id = $1
                    RETURNING id, job_id, kind, provider, model_id, prompt, input_refs, status,
                              result_uri, result_path, result_metadata, error_message,
                              requested_by, session_id, created_at, updated_at, completed_at
                    """,
                    job_id,
                    status,
                    result_uri,
                    result_path,
                    _as_json(result_metadata),
                    error_message,
                    list(TERMINAL_STATUSES),
                )
                data = _normalize_job_row(row)
                data["storage"] = "db"
                return data
        except Exception as exc:
            return {"job_id": job_id, "status": status, "error": str(exc), "storage": "unavailable"}

    def _save_data_uri_media(
        self,
        *,
        job_id: str,
        data_uri: str,
        kind: str,
    ) -> dict[str, Any] | None:
        if not data_uri.startswith("data:") or "," not in data_uri:
            return None
        header, payload = data_uri.split(",", 1)
        if ";base64" not in header or not payload:
            return None

        content_type = header[5:].split(";", 1)[0] or "application/octet-stream"
        ext = mimetypes.guess_extension(content_type) or ".bin"
        body = base64.b64decode(payload, validate=True)

        static_root = _app_static_dir().resolve()
        media_dir = (static_root / "media" / "generated" / kind).resolve()
        media_dir.mkdir(parents=True, exist_ok=True)
        target = (media_dir / _safe_job_filename(job_id, ext)).resolve()
        target.relative_to(media_dir)
        target.write_bytes(body)

        public_path = "/" + target.relative_to(static_root).as_posix()
        return {
            "url": f"/static{public_path}",
            "path": str(target),
            "bytes": len(body),
            "content_type": content_type,
        }

    def _externalize_media_result(
        self,
        *,
        job_id: str,
        kind: str,
        result: dict[str, Any],
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        uri = str(result.get("url") or "").strip()
        if not uri.startswith("data:"):
            return result, metadata, None
        saved = self._save_data_uri_media(job_id=job_id, data_uri=uri, kind=kind)
        if not saved:
            return result, metadata, None
        updated_result = dict(result)
        # /static is owned by the Next.js dashboard at the public ingress, so
        # returning the filesystem-style URL makes successfully generated
        # images render as 404 in chat.  The gallery endpoint streams the
        # stored file through the API ingress and is the stable public URL.
        updated_result["url"] = f"/api/v1/image/gallery/{job_id}/image"
        updated_metadata = {
            **metadata,
            "storage": "static_file",
            "storage_url": saved["url"],
            "bytes": saved["bytes"],
            "content_type": saved["content_type"],
            "base64_externalized": True,
        }
        return updated_result, updated_metadata, saved["path"]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        pool = self._get_pool_or_none()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, job_id, kind, provider, model_id, prompt, input_refs, status,
                           result_uri, result_path, result_metadata, error_message,
                           requested_by, session_id, created_at, updated_at, completed_at
                    FROM media_generation_jobs
                    WHERE job_id = $1
                    """,
                    job_id,
                )
                data = _normalize_job_row(row)
                if data:
                    data["storage"] = "db"
                return data or None
        except Exception:
            return None

    async def _fetch_next_genspark_ui_job(self, job_id: str | None = None) -> dict[str, Any] | None:
        pool = self._get_pool_or_none()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, job_id, kind, provider, model_id, prompt, input_refs, status,
                           result_uri, result_path, result_metadata, error_message,
                           requested_by, session_id, created_at, updated_at, completed_at
                    FROM media_generation_jobs
                    WHERE provider = 'genspark_ui'
                      AND status = 'queued'
                      AND ($1::text IS NULL OR job_id = $1)
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    job_id or None,
                )
                data = _normalize_job_row(row)
                if data:
                    data["storage"] = "db"
                return data or None
        except Exception as exc:
            logger.warning("genspark_ui_fetch_job_failed job_id=%s error=%s", job_id, exc)
            return None

    async def _acquire_genspark_page(
        self,
        *,
        work_key: str,
        target_url: str,
        browser_session_id: str | None = None,
    ) -> Any:
        from app.api.ceo_chat_tools import _acquire_pw_context

        ctx, err = await _acquire_pw_context(browser_session_id or "", "" if browser_session_id else work_key, target_url)
        if err:
            raise RuntimeError(err)
        pages = getattr(ctx, "pages", [])
        page = pages[-1] if pages else await ctx.new_page()
        if target_url and hasattr(page, "goto"):
            goto_timeout_ms = int(float(os.getenv("AADS_GENSPARK_UI_GOTO_TIMEOUT_SECONDS", "25")) * 1000)
            await page.goto(target_url, timeout=goto_timeout_ms, wait_until="domcontentloaded")
        return page

    async def _read_genspark_page_text(self, page: Any) -> str:
        try:
            return str(await page.locator("body").first.aria_snapshot())
        except Exception:
            try:
                return str(await page.evaluate("() => document.body ? document.body.innerText : ''"))
            except Exception:
                return ""

    @staticmethod
    def _looks_like_genspark_auth_gate(page_text: str) -> bool:
        text = (page_text or "").lower()
        return any(
            marker in text
            for marker in (
                "로그인",
                "가입하기",
                "sign in",
                "sign up",
                "login",
            )
        )

    async def _submit_prompt_to_genspark(self, page: Any, prompt: str) -> dict[str, Any]:
        script = """
        (prompt) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 20 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const candidates = [
            ...document.querySelectorAll('textarea'),
            ...document.querySelectorAll('[contenteditable="true"]'),
            ...document.querySelectorAll('input[type="text"], input:not([type])')
          ].filter(visible);
          const el = candidates[candidates.length - 1];
          if (!el) return {ok: false, error: 'PROMPT_INPUT_NOT_FOUND'};
          el.focus();
          if (el.isContentEditable) {
            el.textContent = prompt;
            el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: prompt}));
          } else {
            el.value = prompt;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
          }
          el.dataset.aadsGensparkPrompt = '1';
          return {ok: true, selector: '[data-aads-genspark-prompt="1"]'};
        }
        """
        result = await page.evaluate(script, prompt)
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "error": str((result or {}).get("error") or "PROMPT_INPUT_NOT_FOUND")}

        selector = str(result.get("selector") or '[data-aads-genspark-prompt="1"]')
        try:
            if hasattr(page, "press_key"):
                await page.press_key("Enter", selector)
            else:
                await page.keyboard.press("Enter")
        except Exception:
            try:
                await page.evaluate(
                    """(selector) => {
                      const el = document.querySelector(selector);
                      if (!el) return false;
                      el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
                      el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
                      return true;
                    }""",
                    selector,
                )
            except Exception as exc:
                return {"ok": False, "error": f"PROMPT_SUBMIT_FAILED: {exc}"}
        return {"ok": True, "selector": selector}

    async def _extract_genspark_media_candidate(self, page: Any) -> dict[str, Any]:
        script = """
        async () => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width >= 120 && r.height >= 120 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const nodes = [...document.querySelectorAll('img, video, source')]
            .filter((el) => {
              const src = el.currentSrc || el.src || el.getAttribute('src') || '';
              const area = (el.naturalWidth || el.videoWidth || el.clientWidth || 0) *
                           (el.naturalHeight || el.videoHeight || el.clientHeight || 0);
              return src && !src.includes('data:image/svg') && visible(el) && area >= 40000;
            })
            .sort((a, b) => {
              const aa = (a.naturalWidth || a.videoWidth || a.clientWidth || 0) *
                         (a.naturalHeight || a.videoHeight || a.clientHeight || 0);
              const bb = (b.naturalWidth || b.videoWidth || b.clientWidth || 0) *
                         (b.naturalHeight || b.videoHeight || b.clientHeight || 0);
              return bb - aa;
            });
          for (const el of nodes) {
            const src = el.currentSrc || el.src || el.getAttribute('src') || '';
            if (src.startsWith('data:')) return {ok: true, data_uri: src, tag: el.tagName.toLowerCase()};
            if (src.startsWith('blob:')) {
              try {
                const resp = await fetch(src);
                const blob = await resp.blob();
                const dataUri = await new Promise((resolve, reject) => {
                  const reader = new FileReader();
                  reader.onloadend = () => resolve(reader.result);
                  reader.onerror = reject;
                  reader.readAsDataURL(blob);
                });
                return {ok: true, data_uri: dataUri, tag: el.tagName.toLowerCase()};
              } catch (e) {
                continue;
              }
            }
            if (src.startsWith('http')) return {ok: true, url: src, tag: el.tagName.toLowerCase()};
          }
          return {ok: false, error: 'MEDIA_NOT_FOUND'};
        }
        """
        result = await page.evaluate(script)
        return result if isinstance(result, dict) else {"ok": False, "error": "MEDIA_NOT_FOUND"}

    async def _save_remote_media_url(self, *, job_id: str, url: str, kind: str) -> dict[str, Any]:
        if not _is_public_http_url(url):
            raise ValueError("remote media URL is not allowed")
        max_bytes = int(float(os.getenv("AADS_GENSPARK_MAX_DOWNLOAD_MB", "80")) * 1024 * 1024)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        body = response.content
        if len(body) > max_bytes:
            raise ValueError(f"download exceeds limit: {len(body)} bytes")
        content_type = response.headers.get("content-type", "").split(";", 1)[0] or "application/octet-stream"
        ext = mimetypes.guess_extension(content_type) or Path(url.split("?", 1)[0]).suffix or ".bin"
        static_root = _app_static_dir().resolve()
        media_dir = (static_root / "media" / "generated" / kind).resolve()
        media_dir.mkdir(parents=True, exist_ok=True)
        target = (media_dir / _safe_job_filename(job_id, ext)).resolve()
        target.relative_to(media_dir)
        target.write_bytes(body)
        public_path = "/" + target.relative_to(static_root).as_posix()
        return {
            "url": f"/static{public_path}",
            "path": str(target),
            "bytes": len(body),
            "content_type": content_type,
        }

    async def process_genspark_ui_job(
        self,
        *,
        job_id: str | None = None,
        browser_session_id: str | None = None,
        browser_work_key: str | None = None,
        target_url: str | None = None,
        timeout_seconds: int = 240,
    ) -> dict[str, Any]:
        job = await self._fetch_next_genspark_ui_job(job_id)
        if not job:
            return {
                "status": "idle",
                "message": "queued genspark_ui media job not found",
                "job_id": job_id,
            }

        refs = _as_dict(job.get("input_refs"))
        metadata = _as_dict(job.get("result_metadata"))
        automation = _as_dict(metadata.get("ui_automation"))
        session_id = str(browser_session_id or automation.get("browser_session_id") or "").strip()
        work_key = str(browser_work_key or automation.get("work_key") or "genspark-media-fallback").strip()
        url = str(target_url or automation.get("target_url") or "https://www.genspark.ai/").strip()
        job_kind = str(job.get("kind") or "image")
        effective_timeout = max(30, int(timeout_seconds or 240))
        step_timeout = max(5.0, min(45.0, float(os.getenv("AADS_GENSPARK_UI_STEP_TIMEOUT_SECONDS", "25"))))
        deadline = time.monotonic() + effective_timeout

        def remaining_timeout() -> float:
            return max(1.0, deadline - time.monotonic())

        async def run_step(label: str, awaitable: Any, *, cap: float | None = None) -> Any:
            limit = min(cap or step_timeout, remaining_timeout())
            if limit <= 1.0:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise TimeoutError(f"{label}_TIMEOUT")
            try:
                return await asyncio.wait_for(awaitable, timeout=limit)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"{label}_TIMEOUT") from exc

        await self.update_job_status(
            str(job["job_id"]),
            "running",
            result_metadata={
                **metadata,
                "ui_automation": {
                    **automation,
                    "state": "running",
                    "browser_session_id": session_id or None,
                    "work_key": work_key,
                    "target_url": url,
                    "started_at": datetime.utcnow().isoformat() + "Z",
                },
            },
        )

        try:
            page = await run_step(
                "GENSPARK_PAGE_ACQUIRE",
                self._acquire_genspark_page(
                    work_key=work_key,
                    target_url=url,
                    browser_session_id=session_id or None,
                ),
            )
            page_text = await run_step("GENSPARK_PAGE_READ", self._read_genspark_page_text(page))
            if self._looks_like_genspark_auth_gate(page_text):
                updated = await self.update_job_status(
                    str(job["job_id"]),
                    "queued",
                    result_metadata={
                        **metadata,
                        "ui_automation": {
                            **automation,
                            "state": "auth_required",
                            "browser_session_id": session_id or None,
                            "work_key": work_key,
                            "target_url": url,
                            "last_error": "GENSPARK_LOGIN_REQUIRED",
                        },
                    },
                    error_message="Genspark login required in Browser Bridge/PC Agent session",
                )
                public = _public_job(updated)
                public.update({"automation_state": "auth_required", "requires_login": True})
                return public

            submitted = await run_step(
                "GENSPARK_PROMPT_SUBMIT",
                self._submit_prompt_to_genspark(page, str(job.get("prompt") or "")),
            )
            if not submitted.get("ok"):
                raise RuntimeError(str(submitted.get("error") or "PROMPT_SUBMIT_FAILED"))

            candidate: dict[str, Any] = {"ok": False, "error": "MEDIA_NOT_FOUND"}
            while time.monotonic() < deadline:
                await run_step("GENSPARK_WAIT", page.wait_for_timeout(5000), cap=6.0)
                candidate = await run_step(
                    "GENSPARK_MEDIA_EXTRACT",
                    self._extract_genspark_media_candidate(page),
                    cap=20.0,
                )
                if candidate.get("ok"):
                    break
            if not candidate.get("ok"):
                raise RuntimeError(str(candidate.get("error") or "MEDIA_NOT_FOUND"))

            if candidate.get("data_uri"):
                saved = self._save_data_uri_media(
                    job_id=str(job["job_id"]),
                    data_uri=str(candidate["data_uri"]),
                    kind=job_kind,
                )
                if not saved:
                    raise RuntimeError("MEDIA_SAVE_FAILED")
            else:
                saved = await self._save_remote_media_url(
                    job_id=str(job["job_id"]),
                    url=str(candidate["url"]),
                    kind=job_kind,
                )

            result_uri = f"/api/v1/image/gallery/{job['job_id']}/image" if job_kind in {"image", "edit_image"} else saved["url"]
            updated = await self.update_job_status(
                str(job["job_id"]),
                "succeeded",
                result_uri=result_uri,
                result_path=saved["path"],
                result_metadata={
                    **metadata,
                    "ui_automation": {
                        **automation,
                        "state": "succeeded",
                        "browser_session_id": session_id or None,
                        "work_key": work_key,
                        "target_url": url,
                        "completed_at": datetime.utcnow().isoformat() + "Z",
                    },
                    "storage": "static_file",
                    "storage_url": saved["url"],
                    "bytes": saved["bytes"],
                    "content_type": saved["content_type"],
                    "browser_candidate": {k: v for k, v in candidate.items() if k != "data_uri"},
                },
            )
            public = _public_job(updated)
            public.update({"automation_state": "succeeded", "result_path": saved["path"]})
            return public
        except Exception as exc:
            updated = await self.update_job_status(
                str(job["job_id"]),
                "queued",
                result_metadata={
                    **metadata,
                    "ui_automation": {
                        **automation,
                        "state": "retryable_error",
                        "browser_session_id": session_id or None,
                        "work_key": work_key,
                        "target_url": url,
                        "last_error": str(exc),
                    },
                },
                error_message=str(exc),
            )
            public = _public_job(updated)
            public.update({"automation_state": "retryable_error", "error": str(exc)})
            return public

    def _job_error(
        self,
        *,
        code: str,
        message: str,
        job: Mapping[str, Any],
        route: MediaRoute | None = None,
    ) -> dict[str, Any]:
        return {
            "error": code,
            "message": message,
            "job_id": job.get("job_id"),
            "kind": job.get("kind"),
            "status": "failed",
            "provider": (route.provider if route else job.get("provider")),
            "model_id": (route.model_id if route else job.get("model_id")),
            "availability": (route.availability if route else "unknown"),
            "route_source": (route.source if route else "unknown"),
        }

    async def _mark_failed(
        self,
        job: Mapping[str, Any],
        *,
        code: str,
        message: str,
        route: MediaRoute | None = None,
    ) -> dict[str, Any]:
        metadata = {"error_code": code}
        if route:
            metadata.update(
                {
                    "provider": route.provider,
                    "model_id": route.model_id,
                    "availability": route.availability,
                    "route_source": route.source,
                }
            )
        await self.update_job_status(
            str(job.get("job_id") or ""),
            "failed",
            result_metadata=metadata,
            error_message=message,
        )
        return self._job_error(code=code, message=message, job=job, route=route)

    async def _prepare_local_media_job(
        self,
        *,
        job: Mapping[str, Any],
        kind: str,
        prompt: str,
        input_refs: dict[str, Any] | None,
        route: MediaRoute,
    ) -> dict[str, Any]:
        from app.services.local_model_manager import local_model_manager

        dispatch = await local_model_manager.dispatch_media_job(
            job=job,
            kind=kind,
            prompt=prompt,
            input_refs=input_refs or {},
        )
        metadata = {
            "provider": route.provider,
            "model_id": route.model_id,
            "route_source": route.source,
            "local_dispatch": dispatch,
            "local_job_state": "queued_or_prepared",
        }
        updated = await self.update_job_status(
            str(job.get("job_id") or ""),
            "queued",
            result_metadata=metadata,
        )
        public = _public_job(updated or job)
        public.update(
            {
                "job_id": job.get("job_id"),
                "kind": kind,
                "status": "queued",
                "provider": route.provider,
                "model_id": route.model_id,
                "availability": "queued_or_prepared",
                "local_dispatch": dispatch,
            }
        )
        return public

    async def _prepare_genspark_ui_job(
        self,
        *,
        job: Mapping[str, Any],
        kind: str,
        prompt: str,
        input_refs: dict[str, Any] | None,
        route: MediaRoute,
    ) -> dict[str, Any]:
        refs = dict(input_refs or {})
        work_key = str(refs.get("browser_work_key") or "genspark-media-fallback").strip()
        download_dir = str(
            refs.get("download_dir")
            or os.getenv("AADS_GENSPARK_DOWNLOAD_DIR")
            or "/tmp/aads-media/genspark-downloads"
        ).strip()
        metadata = {
            "provider": route.provider,
            "model_id": route.model_id,
            "route_source": route.source,
            "ui_automation": {
                "service": "genspark",
                "state": "queued_requires_agent",
                "work_key": work_key,
                "target_url": str(refs.get("target_url") or "https://www.genspark.ai/"),
                "download_dir": download_dir,
                "requires_logged_in_browser": True,
                "stores_result_via": "media_generation_jobs.result_path/result_uri",
                "policy": "use normal logged-in UI only; do not bypass captcha, paywalls, or rate limits",
            },
            "input_refs": refs,
        }
        updated = await self.update_job_status(
            str(job.get("job_id") or ""),
            "queued",
            result_metadata=metadata,
        )
        public = _public_job(updated or job)
        public.update(
            {
                "job_id": job.get("job_id"),
                "kind": kind,
                "status": "queued",
                "provider": route.provider,
                "model_id": route.model_id,
                "availability": "queued_requires_agent",
                "automation_state": "queued_requires_agent",
                "message": "Genspark UI fallback job queued. A connected PC Agent/Browser Bridge session is required to generate, download, and store the result.",
            }
        )
        return public

    async def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        *,
        model_id: str | None = None,
        provider: str | None = None,
        requested_by: str | None = None,
        session_id: str | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        reference_images: list[str] | None = None,
    ) -> dict[str, Any]:
        if not str(prompt or "").strip():
            raise ValueError("프롬프트를 입력하세요")
        route = await self.resolve_route("image", model_id=model_id, provider=provider)
        job = await self._insert_job(
            kind="image",
            provider=route.provider,
            model_id=route.model_id,
            prompt=prompt,
            input_refs={
                "size": size,
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "reference_images": reference_images or [],
            },
            status="queued" if route.provider in {"pc_local", "kling", "genspark_ui"} else "running",
            requested_by=requested_by,
            session_id=session_id,
        )
        if not route.enabled:
            return await self._mark_failed(
                job,
                code="MODEL_DISABLED",
                message=route.reason,
                route=route,
            )
        if not route.configured:
            return await self._mark_failed(
                job,
                code="NOT_CONFIGURED",
                message=route.reason,
                route=route,
            )
        if not route.supported:
            return await self._mark_failed(
                job,
                code="PROVIDER_UNAVAILABLE",
                message=route.reason,
                route=route,
            )
        if route.provider == "pc_local":
            return await self._prepare_local_media_job(
                job=job,
                kind="image",
                prompt=prompt,
                input_refs={"size": size},
                route=route,
            )
        if route.provider == "genspark_ui":
            return await self._prepare_genspark_ui_job(
                job=job,
                kind="image",
                prompt=prompt,
                input_refs={
                    "size": size,
                    "aspect_ratio": aspect_ratio,
                    "image_size": image_size,
                    "reference_images": reference_images or [],
                },
                route=route,
            )
        if route.provider == "kling":
            try:
                return await self._submit_kling_image_job(
                    job=job,
                    prompt=prompt,
                    route=route,
                    aspect_ratio=aspect_ratio,
                    reference_images=reference_images,
                    input_refs={"size": size, "aspect_ratio": aspect_ratio},
                )
            except Exception as exc:
                return await self._mark_failed(
                    job,
                    code="PROVIDER_UNAVAILABLE",
                    message=str(exc),
                    route=route,
                )
        try:
            if route.source in {"explicit", "db_default"}:
                result = await self._generate_image_with_route(prompt, size, route, aspect_ratio=aspect_ratio, image_size=image_size, reference_images=reference_images)
            else:
                from app.services.image_service import image_service

                result = await image_service.generate(prompt, size)
            result = dict(result)
            result.setdefault("provider", route.provider)
            result.setdefault("prompt", prompt)
            metadata = {
                "provider": result.get("provider"),
                "model_id": route.model_id,
                "size": size,
            }
            result, metadata, result_path = self._externalize_media_result(
                job_id=str(job.get("job_id") or ""),
                kind="image",
                result=result,
                metadata=metadata,
            )
            await self.update_job_status(
                str(job.get("job_id") or ""),
                "succeeded",
                result_uri=result.get("url"),
                result_path=result_path,
                result_metadata=metadata,
            )
            result.update(
                {
                    "job_id": job.get("job_id"),
                    "kind": "image",
                    "status": "succeeded",
                    "model_id": route.model_id,
                }
            )
            return result
        except Exception as exc:
            return await self._mark_failed(
                job,
                code="PROVIDER_UNAVAILABLE",
                message=str(exc),
                route=route,
            )

    async def _generate_image_with_route(
        self,
        prompt: str,
        size: str,
        route: MediaRoute,
        *,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        reference_images: list[str] | None = None,
    ) -> dict[str, Any]:
        sanitized = _sanitize_prompt(prompt)
        if route.provider == "openai":
            return await self._generate_openai_image(sanitized, prompt, size, route.model_id)
        if route.provider == "google":
            return await self._generate_google_image(sanitized, prompt, route.model_id, aspect_ratio=aspect_ratio, image_size=image_size)
        if route.provider == "gemini":
            return await self._generate_gemini_native_image(sanitized, prompt, route.model_id, aspect_ratio=aspect_ratio, image_size=image_size, reference_images=reference_images)
        raise ValueError(route.reason or "provider unavailable")

    async def _generate_openai_image(
        self,
        sanitized: str,
        original: str,
        size: str,
        model_id: str,
    ) -> dict[str, Any]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=_secret_value(self.settings, "OPENAI_API_KEY"))
        resp = await client.images.generate(
            model=model_id,
            prompt=sanitized,
            size=size,
            n=1,
        )
        if resp.data and getattr(resp.data[0], "b64_json", None):
            b64 = resp.data[0].b64_json
            return {"url": f"data:image/png;base64,{b64}", "provider": model_id, "prompt": original}
        url = resp.data[0].url if resp.data else None
        if not url:
            raise ValueError("No image generated from OpenAI")
        async with httpx.AsyncClient(timeout=30.0) as client_http:
            image_resp = await client_http.get(url)
            image_resp.raise_for_status()
        b64 = base64.b64encode(image_resp.content).decode()
        return {"url": f"data:image/png;base64,{b64}", "provider": model_id, "prompt": original}

    async def _generate_google_image(
        self,
        sanitized: str,
        original: str,
        model_id: str,
        *,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        img_config = {"number_of_images": 1, "aspect_ratio": aspect_ratio or "1:1"}
        if image_size and image_size in ("1K", "2K"):
            img_config["image_size"] = image_size
        client = genai.Client(api_key=_secret_value(self.settings, "GOOGLE_API_KEY"))
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_images(
                model=model_id,
                prompt=sanitized,
                config=types.GenerateImagesConfig(**img_config),
            ),
        )
        if not response.generated_images:
            raise ValueError("No images returned from Google Imagen")
        image_bytes = response.generated_images[0].image.image_bytes
        b64 = base64.b64encode(image_bytes).decode()
        return {"url": f"data:image/png;base64,{b64}", "provider": model_id, "prompt": original}

    async def _generate_gemini_native_image(
        self,
        sanitized: str,
        original: str,
        model_id: str,
        *,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        reference_images: list[str] | None = None,
    ) -> dict[str, Any]:
        model_id = _canonical_media_model_id(model_id)
        from google import genai
        from google.genai import types

        gen_config: dict[str, Any] = {"response_modalities": ["IMAGE"]}
        img_cfg: dict[str, str] = {}
        if aspect_ratio:
            img_cfg["aspect_ratio"] = aspect_ratio
        if image_size and image_size in ("512", "1K", "2K", "4K"):
            img_cfg["image_size"] = image_size
        if img_cfg:
            gen_config["image_config"] = types.ImageConfig(**img_cfg)

        contents: list = [sanitized]
        if reference_images:
            logger.info("gemini_native_ref_images_received count=%d urls=%s", len(reference_images), reference_images)
            for img_url in reference_images[:3]:
                try:
                    ref_headers: dict[str, str] = {}
                    if "aads.newtalk.kr" in str(img_url):
                        mk = os.getenv("AADS_MONITOR_KEY", "")
                        if mk:
                            ref_headers["X-Monitor-Key"] = mk
                    async with httpx.AsyncClient(timeout=15.0) as http_client:
                        img_resp = await http_client.get(str(img_url), headers=ref_headers)
                        img_resp.raise_for_status()
                    mime = img_resp.headers.get("content-type", "image/jpeg").split(";")[0]
                    contents.append(types.Part.from_bytes(data=img_resp.content, mime_type=mime))
                    logger.info("gemini_native_ref_image_loaded url=%s bytes=%d mime=%s", img_url, len(img_resp.content), mime)
                except Exception as e:
                    logger.error("gemini_native_ref_image_failed url=%s error=%s", img_url, e)

        api_keys = [_secret_value(self.settings, "GOOGLE_API_KEY")]
        fallback_key = os.getenv("GEMINI_API_KEY_2", "")
        if fallback_key:
            api_keys.append(fallback_key)

        loop = asyncio.get_event_loop()
        _contents = contents
        last_error: Exception | None = None
        for key_idx, api_key in enumerate(api_keys):
            try:
                client = genai.Client(api_key=api_key)
                response = await loop.run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=model_id,
                        contents=_contents,
                        config=types.GenerateContentConfig(**gen_config),
                    ),
                )
                if not response.candidates:
                    raise ValueError(f"No candidates returned from Gemini {model_id}")
                candidate = response.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    finish_reason = getattr(candidate, "finish_reason", None)
                    raise ValueError(
                        f"Gemini {model_id} returned candidate without image content"
                        f" (finish_reason={finish_reason})"
                    )
                for part in candidate.content.parts:
                    if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                        b64 = base64.b64encode(part.inline_data.data).decode()
                        mime = part.inline_data.mime_type
                        if key_idx > 0:
                            logger.info("gemini_fallback_key_succeeded key_index=%d", key_idx)
                        return {"url": f"data:{mime};base64,{b64}", "provider": model_id, "prompt": original}
                raise ValueError(f"No image part found in Gemini {model_id} response")
            except Exception as exc:
                err_str = str(exc)
                if ("RESOURCE_EXHAUSTED" in err_str or "429" in err_str) and key_idx < len(api_keys) - 1:
                    logger.warning("gemini_key_exhausted key_index=%d, retrying with fallback", key_idx)
                    last_error = exc
                    continue
                raise
        if last_error:
            raise last_error

    @staticmethod
    def _kling_base_url() -> str:
        return os.getenv("KLING_API_BASE_URL", "https://api-singapore.klingai.com").rstrip("/")

    @staticmethod
    def _kling_model_name(model_id: str) -> str:
        aliases = {
            "kling-2.0": "kling-v2",
            "kling-v2.0": "kling-v2",
            "kling-v2.1": "kling-v2-1",
            "kling-v2.5": "kling-v2-5",
        }
        normalized = str(model_id or "").strip().lower()
        return aliases.get(normalized, str(model_id or "kling-v2").strip() or "kling-v2")

    @staticmethod
    def _strip_data_uri(value: Any) -> str:
        text = str(value or "").strip()
        if text.startswith("data:") and "," in text:
            return text.split(",", 1)[1]
        return text

    @staticmethod
    def _jwt_b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @classmethod
    def _build_kling_jwt(cls, access_key: str, secret_key: str) -> str:
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {"iss": access_key, "exp": now + 1800, "nbf": now - 5}
        signing_input = ".".join(
            (
                cls._jwt_b64(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                cls._jwt_b64(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            )
        )
        signature = hmac.new(
            secret_key.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{signing_input}.{cls._jwt_b64(signature)}"

    async def _get_kling_credentials(self) -> tuple[str, str]:
        access_key = os.getenv("KLING_ACCESS_KEY", "").strip()
        secret_key = os.getenv("KLING_SECRET_KEY", "").strip()
        if access_key and secret_key:
            return access_key, secret_key

        try:
            from app.core.llm_key_provider import get_api_key, get_provider_key_records

            access_key = access_key or (await get_api_key("KLING_ACCESS_KEY")).strip()
            secret_key = secret_key or (await get_api_key("KLING_SECRET_KEY")).strip()
            if access_key and secret_key:
                return access_key, secret_key

            for record in await get_provider_key_records("kling", include_rate_limited=False):
                key_name = str(record.get("key_name") or "").upper()
                value = str(record.get("value") or "").strip()
                if key_name.endswith("ACCESS_KEY") or key_name.endswith("_AK"):
                    access_key = access_key or value
                elif key_name.endswith("SECRET_KEY") or key_name.endswith("_SK"):
                    secret_key = secret_key or value
        except Exception:
            logger.exception("kling_credentials_lookup_failed")
        return access_key, secret_key

    async def _kling_request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        access_key, secret_key = await self._get_kling_credentials()
        if not access_key or not secret_key:
            raise ValueError("Kling credentials are not configured")
        token = self._build_kling_jwt(access_key, secret_key)
        url = f"{self._kling_base_url()}{endpoint}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload if payload is not None else None,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text[:1000]
            raise ValueError(
                f"Kling API HTTP {response.status_code}: {body}"
            ) from exc
        data = response.json()
        code = data.get("code")
        if code not in (0, "0", None):
            raise ValueError(f"Kling API error {code}: {data.get('message') or data}")
        return data

    @staticmethod
    def _kling_task_payload(
        *,
        prompt: str,
        model_id: str,
        input_refs: dict[str, Any] | None = None,
        aspect_ratio: str | None = None,
    ) -> dict[str, Any]:
        refs = dict(input_refs or {})
        payload: dict[str, Any] = {
            "model_name": MediaGenerationService._kling_model_name(model_id),
            "prompt": str(prompt or "")[:2500],
        }
        for source_key, target_key in (
            ("negative_prompt", "negative_prompt"),
            ("mode", "mode"),
            ("duration", "duration"),
            ("callback_url", "callback_url"),
            ("external_task_id", "external_task_id"),
            ("cfg_scale", "cfg_scale"),
            ("camera_control", "camera_control"),
        ):
            if refs.get(source_key) not in (None, ""):
                payload[target_key] = refs[source_key]
        payload["aspect_ratio"] = str(
            refs.get("aspect_ratio") or aspect_ratio or "16:9"
        )
        return payload

    @staticmethod
    def _kling_status_to_job_status(task_status: str) -> str:
        status = str(task_status or "").strip().lower()
        if status in {"succeed", "success", "completed", "complete"}:
            return "succeeded"
        if status in {"failed", "failure", "error"}:
            return "failed"
        if status in {"processing", "running"}:
            return "running"
        return "queued"

    @staticmethod
    def _kling_first_result_url(data: Mapping[str, Any], kind: str) -> str | None:
        task_result = _as_dict(data.get("task_result"))
        if kind == "video":
            videos = task_result.get("videos")
            if isinstance(videos, list) and videos:
                item = _as_dict(videos[0])
                return item.get("url") or item.get("video_url")
            return task_result.get("url") or task_result.get("video_url")
        images = task_result.get("images")
        if isinstance(images, list) and images:
            item = _as_dict(images[0])
            return item.get("url") or item.get("image_url")
        return task_result.get("url") or task_result.get("image_url")

    async def _submit_kling_image_job(
        self,
        *,
        job: Mapping[str, Any],
        prompt: str,
        route: MediaRoute,
        aspect_ratio: str | None,
        reference_images: list[str] | None,
        input_refs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        refs = dict(input_refs or {})
        payload = self._kling_task_payload(
            prompt=prompt,
            model_id=route.model_id,
            input_refs=refs,
            aspect_ratio=aspect_ratio,
        )
        image_value = refs.get("image") or refs.get("image_url") or refs.get("input_image")
        if image_value:
            payload["image"] = self._strip_data_uri(image_value)
        if reference_images:
            payload["subject_image_list"] = [
                {"subject_image": self._strip_data_uri(image)}
                for image in reference_images[:4]
                if str(image or "").strip()
            ]
        if refs.get("scene_image"):
            payload["scene_image"] = self._strip_data_uri(refs["scene_image"])
        if refs.get("style_image"):
            payload["style_image"] = self._strip_data_uri(refs["style_image"])
        if refs.get("n"):
            payload["n"] = refs["n"]
        response = await self._kling_request("POST", "/v1/images/generations", payload)
        data = _as_dict(response.get("data"))
        provider_task_id = str(data.get("task_id") or "").strip()
        provider_status = str(data.get("task_status") or "submitted")
        metadata = {
            "provider": route.provider,
            "model_id": route.model_id,
            "provider_task_id": provider_task_id,
            "provider_status": provider_status,
            "provider_endpoint": "/v1/images/generations",
            "request_id": response.get("request_id"),
        }
        updated = await self.update_job_status(
            str(job.get("job_id") or ""),
            self._kling_status_to_job_status(provider_status),
            result_metadata=metadata,
        )
        public = _public_job(updated or job)
        public.update(
            {
                "job_id": job.get("job_id"),
                "kind": "image",
                "status": public.get("status") or "queued",
                "provider": route.provider,
                "model_id": route.model_id,
                "provider_task_id": provider_task_id,
                "availability": "submitted",
            }
        )
        return public

    async def _submit_kling_video_job(
        self,
        *,
        job: Mapping[str, Any],
        prompt: str,
        route: MediaRoute,
        input_refs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        refs = dict(input_refs or {})
        payload = self._kling_task_payload(prompt=prompt, model_id=route.model_id, input_refs=refs)
        image_value = (
            refs.get("image")
            or refs.get("image_url")
            or refs.get("start_frame")
            or refs.get("reference_image")
        )
        endpoint = "/v1/videos/text2video"
        if image_value:
            endpoint = "/v1/videos/image2video"
            payload["image"] = self._strip_data_uri(image_value)
        if refs.get("image_tail"):
            payload["image_tail"] = self._strip_data_uri(refs["image_tail"])
        response = await self._kling_request("POST", endpoint, payload)
        data = _as_dict(response.get("data"))
        provider_task_id = str(data.get("task_id") or "").strip()
        provider_status = str(data.get("task_status") or "submitted")
        metadata = {
            "provider": route.provider,
            "model_id": route.model_id,
            "provider_task_id": provider_task_id,
            "provider_status": provider_status,
            "provider_endpoint": endpoint,
            "request_id": response.get("request_id"),
        }
        updated = await self.update_job_status(
            str(job.get("job_id") or ""),
            self._kling_status_to_job_status(provider_status),
            result_metadata=metadata,
        )
        public = _public_job(updated or job)
        public.update(
            {
                "job_id": job.get("job_id"),
                "kind": "video",
                "status": public.get("status") or "queued",
                "provider": route.provider,
                "model_id": route.model_id,
                "provider_task_id": provider_task_id,
                "availability": "submitted",
            }
        )
        return public

    async def _refresh_kling_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        metadata = _as_dict(job.get("result_metadata"))
        provider_task_id = str(metadata.get("provider_task_id") or "").strip()
        endpoint = str(metadata.get("provider_endpoint") or "").strip()
        if not provider_task_id or not endpoint:
            return dict(job)
        response = await self._kling_request("GET", f"{endpoint.rstrip('/')}/{provider_task_id}")
        data = _as_dict(response.get("data"))
        provider_status = str(data.get("task_status") or metadata.get("provider_status") or "")
        status = self._kling_status_to_job_status(provider_status)
        result_uri = self._kling_first_result_url(data, str(job.get("kind") or ""))
        merged_metadata = {
            **metadata,
            "provider_status": provider_status,
            "provider_response_updated_at": data.get("updated_at"),
            "provider_final_unit_deduction": data.get("final_unit_deduction"),
            "request_id": response.get("request_id") or metadata.get("request_id"),
        }
        error_message = data.get("task_status_msg") if status == "failed" else None
        return await self.update_job_status(
            str(job.get("job_id") or ""),
            status,
            result_uri=result_uri,
            result_metadata=merged_metadata,
            error_message=error_message,
        )

    async def edit_image(
        self,
        prompt: str,
        *,
        input_refs: dict[str, Any] | None = None,
        size: str = "1024x1024",
        model_id: str | None = None,
        provider: str | None = None,
        requested_by: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not str(prompt or "").strip():
            raise ValueError("프롬프트를 입력하세요")
        refs = dict(input_refs or {})
        route = await self.resolve_route("edit_image", model_id=model_id, provider=provider)
        job = await self._insert_job(
            kind="edit_image",
            provider=route.provider,
            model_id=route.model_id,
            prompt=prompt,
            input_refs={**refs, "size": size},
            status="queued" if route.provider in {"pc_local", "genspark_ui"} else "running",
            requested_by=requested_by,
            session_id=session_id,
        )
        if not route.enabled:
            return await self._mark_failed(
                job,
                code="MODEL_DISABLED",
                message=route.reason,
                route=route,
            )
        if not route.configured:
            return await self._mark_failed(
                job,
                code="NOT_CONFIGURED",
                message=route.reason,
                route=route,
            )
        if not route.supported:
            return await self._mark_failed(
                job,
                code="PROVIDER_UNAVAILABLE",
                message=route.reason,
                route=route,
            )
        if route.provider == "pc_local":
            return await self._prepare_local_media_job(
                job=job,
                kind="edit_image",
                prompt=prompt,
                input_refs={**refs, "size": size},
                route=route,
            )
        if route.provider == "genspark_ui":
            return await self._prepare_genspark_ui_job(
                job=job,
                kind="edit_image",
                prompt=prompt,
                input_refs={**refs, "size": size},
                route=route,
            )
        image_path = refs.get("image_path") or refs.get("input_image_path")
        if not image_path:
            return await self._mark_failed(
                job,
                code="INVALID_INPUT",
                message="edit_image requires image_path or input_image_path",
                route=route,
            )
        try:
            result = await self._edit_openai_image(
                prompt=prompt,
                image_path=str(image_path),
                mask_path=str(refs.get("mask_path") or "") or None,
                size=size,
                model_id=route.model_id,
            )
            metadata = {"provider": route.provider, "model_id": route.model_id, "size": size}
            result, metadata, result_path = self._externalize_media_result(
                job_id=str(job.get("job_id") or ""),
                kind="edit_image",
                result=result,
                metadata=metadata,
            )
            await self.update_job_status(
                str(job.get("job_id") or ""),
                "succeeded",
                result_uri=result.get("url"),
                result_path=result_path,
                result_metadata=metadata,
            )
            result.update(
                {
                    "job_id": job.get("job_id"),
                    "kind": "edit_image",
                    "status": "succeeded",
                    "model_id": route.model_id,
                }
            )
            return result
        except Exception as exc:
            return await self._mark_failed(
                job,
                code="PROVIDER_UNAVAILABLE",
                message=str(exc),
                route=route,
            )

    async def _edit_openai_image(
        self,
        *,
        prompt: str,
        image_path: str,
        mask_path: str | None,
        size: str,
        model_id: str,
    ) -> dict[str, Any]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=_secret_value(self.settings, "OPENAI_API_KEY"))
        sanitized = _sanitize_prompt(prompt)
        with open(image_path, "rb") as image_file:
            kwargs: dict[str, Any] = {
                "model": model_id,
                "image": image_file,
                "prompt": sanitized,
                "size": size,
                "n": 1,
            }
            if mask_path:
                with open(mask_path, "rb") as mask_file:
                    kwargs["mask"] = mask_file
                    resp = await client.images.edit(**kwargs)
            else:
                resp = await client.images.edit(**kwargs)
        if resp.data and getattr(resp.data[0], "b64_json", None):
            b64 = resp.data[0].b64_json
            return {"url": f"data:image/png;base64,{b64}", "provider": model_id, "prompt": prompt}
        url = resp.data[0].url if resp.data else None
        if not url:
            raise ValueError("No edited image returned from OpenAI")
        return {"url": url, "provider": model_id, "prompt": prompt}

    async def generate_video(
        self,
        prompt: str,
        *,
        input_refs: dict[str, Any] | None = None,
        model_id: str | None = None,
        provider: str | None = None,
        requested_by: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not str(prompt or "").strip():
            raise ValueError("프롬프트를 입력하세요")
        route = await self.resolve_route("video", model_id=model_id, provider=provider)
        job = await self._insert_job(
            kind="video",
            provider=route.provider,
            model_id=route.model_id,
            prompt=prompt,
            input_refs=input_refs or {},
            status="queued",
            requested_by=requested_by,
            session_id=session_id,
        )
        if not route.enabled:
            return await self._mark_failed(
                job,
                code="MODEL_DISABLED",
                message=route.reason,
                route=route,
            )
        if not route.configured:
            return await self._mark_failed(
                job,
                code="NOT_CONFIGURED",
                message=route.reason,
                route=route,
            )
        if route.provider == "pc_local":
            return await self._prepare_local_media_job(
                job=job,
                kind="video",
                prompt=prompt,
                input_refs=input_refs or {},
                route=route,
            )
        if route.provider == "genspark_ui":
            return await self._prepare_genspark_ui_job(
                job=job,
                kind="video",
                prompt=prompt,
                input_refs=input_refs or {},
                route=route,
            )
        if route.provider == "kling":
            try:
                return await self._submit_kling_video_job(
                    job=job,
                    prompt=prompt,
                    route=route,
                    input_refs=input_refs or {},
                )
            except Exception as exc:
                return await self._mark_failed(
                    job,
                    code="PROVIDER_UNAVAILABLE",
                    message=str(exc),
                    route=route,
                )
        return await self._mark_failed(
            job,
            code="PROVIDER_UNAVAILABLE",
            message=route.reason or "Video provider adapter is not available in P0",
            route=route,
        )

    async def _generate_local_async_job(
        self,
        kind: str,
        prompt: str,
        *,
        input_refs: dict[str, Any] | None = None,
        model_id: str | None = None,
        provider: str | None = "pc_local",
        requested_by: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not str(prompt or "").strip():
            raise ValueError("프롬프트를 입력하세요")
        route = await self.resolve_route(kind, model_id=model_id, provider=provider or "pc_local")
        job = await self._insert_job(
            kind=kind,
            provider=route.provider,
            model_id=route.model_id,
            prompt=prompt,
            input_refs=input_refs or {},
            status="queued",
            requested_by=requested_by,
            session_id=session_id,
        )
        if not route.enabled:
            return await self._mark_failed(job, code="MODEL_DISABLED", message=route.reason, route=route)
        if not route.configured:
            return await self._mark_failed(job, code="NOT_CONFIGURED", message=route.reason, route=route)
        if not route.supported or route.provider != "pc_local":
            return await self._mark_failed(
                job,
                code="PROVIDER_UNAVAILABLE",
                message=route.reason or f"{kind} provider adapter is not available",
                route=route,
            )
        return await self._prepare_local_media_job(
            job=job,
            kind=kind,
            prompt=prompt,
            input_refs=input_refs or {},
            route=route,
        )

    async def generate_music(
        self,
        prompt: str,
        *,
        input_refs: dict[str, Any] | None = None,
        model_id: str | None = None,
        provider: str | None = "pc_local",
        requested_by: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._generate_local_async_job(
            "music",
            prompt,
            input_refs=input_refs,
            model_id=model_id,
            provider=provider,
            requested_by=requested_by,
            session_id=session_id,
        )

    async def generate_3d(
        self,
        prompt: str,
        *,
        input_refs: dict[str, Any] | None = None,
        model_id: str | None = None,
        provider: str | None = "pc_local",
        requested_by: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._generate_local_async_job(
            "model_3d",
            prompt,
            input_refs=input_refs,
            model_id=model_id,
            provider=provider,
            requested_by=requested_by,
            session_id=session_id,
        )

    async def media_status(self, job_id: str) -> dict[str, Any]:
        job = await self.get_job(str(job_id or "").strip())
        if not job:
            return {
                "error": "JOB_NOT_FOUND",
                "message": "media job not found or storage unavailable",
                "job_id": job_id,
            }
        if job.get("provider") == "kling" and job.get("status") not in TERMINAL_STATUSES:
            try:
                job = await self._refresh_kling_job(job)
            except Exception as exc:
                logger.warning("kling_media_status_refresh_failed job_id=%s error=%s", job_id, exc)
        return _public_job(job)

    async def video_status(self, job_id: str) -> dict[str, Any]:
        job = await self.get_job(str(job_id or "").strip())
        if not job:
            return {
                "error": "JOB_NOT_FOUND",
                "message": "video job not found or storage unavailable",
                "job_id": job_id,
            }
        if job.get("kind") != "video":
            return {"error": "INVALID_JOB_KIND", "job_id": job_id, "kind": job.get("kind")}
        if job.get("provider") == "kling" and job.get("status") not in TERMINAL_STATUSES:
            try:
                job = await self._refresh_kling_job(job)
            except Exception as exc:
                logger.warning("kling_video_status_refresh_failed job_id=%s error=%s", job_id, exc)
        return _public_job(job)

    async def video_download(
        self,
        job_id: str,
        *,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        job = await self.get_job(str(job_id or "").strip())
        if not job:
            return {
                "error": "JOB_NOT_FOUND",
                "message": "video job not found or storage unavailable",
                "job_id": job_id,
            }
        if job.get("kind") != "video":
            return {"error": "INVALID_JOB_KIND", "job_id": job_id, "kind": job.get("kind")}
        if job.get("status") != "succeeded":
            return {
                "error": "JOB_NOT_READY",
                "job_id": job_id,
                "status": job.get("status"),
                "message": "video job has no downloadable result yet",
            }
        existing_path = str(job.get("result_path") or "").strip()
        if existing_path and Path(existing_path).exists():
            return {
                "job_id": job_id,
                "status": job.get("status"),
                "result_path": existing_path,
                "result_metadata": job.get("result_metadata") or {},
            }
        result_uri = str(job.get("result_uri") or "").strip()
        if not result_uri:
            return {"error": "RESULT_UNAVAILABLE", "job_id": job_id, "status": job.get("status")}
        try:
            saved = await self._save_video_result(job_id, result_uri, output_dir=output_dir)
        except Exception as exc:
            return {"error": "DOWNLOAD_FAILED", "job_id": job_id, "message": str(exc)}
        metadata = {
            **(job.get("result_metadata") or {}),
            "downloaded": True,
            "bytes": saved["bytes"],
            "content_type": saved["content_type"],
        }
        updated = await self.update_job_status(
            job_id,
            "succeeded",
            result_path=saved["path"],
            result_metadata=metadata,
        )
        return {
            "job_id": job_id,
            "status": "succeeded",
            "result_path": saved["path"],
            "result_metadata": updated.get("result_metadata") or metadata,
        }

    async def _save_video_result(
        self,
        job_id: str,
        result_uri: str,
        *,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        content_type = "video/mp4"
        if result_uri.startswith("data:"):
            header, _, payload = result_uri.partition(",")
            if ";base64" not in header or not payload:
                raise ValueError("unsupported data URI")
            content_type = header[5:].split(";", 1)[0] or content_type
            body = base64.b64decode(payload)
        elif result_uri.startswith("http://") or result_uri.startswith("https://"):
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(result_uri)
                response.raise_for_status()
                body = response.content
                content_type = response.headers.get("content-type", content_type).split(";", 1)[0]
        else:
            source = Path(result_uri).expanduser().resolve()
            if not source.exists() or not source.is_file():
                raise ValueError("result URI is not a readable file")
            body = source.read_bytes()
            content_type = mimetypes.guess_type(str(source))[0] or content_type

        configured_root = self.output_dir.expanduser().resolve()
        base_dir = Path(output_dir or configured_root).expanduser().resolve()
        try:
            base_dir.relative_to(configured_root)
        except ValueError:
            raise ValueError("unsafe video output root")
        video_dir = (base_dir / "videos").resolve()
        video_dir.mkdir(parents=True, exist_ok=True)
        safe_job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
        ext = mimetypes.guess_extension(content_type) or ".mp4"
        target = (video_dir / f"{safe_job_id}{ext}").resolve()
        try:
            target.relative_to(video_dir)
        except ValueError:
            raise ValueError("unsafe video output path")
        target.write_bytes(body)
        return {"path": str(target), "bytes": len(body), "content_type": content_type}


media_generation_service = MediaGenerationService()
