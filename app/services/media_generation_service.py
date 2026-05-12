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
import json
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import httpx

try:
    from app.config import settings as app_settings
except Exception:
    app_settings = SimpleNamespace(OPENAI_API_KEY="", GOOGLE_API_KEY="")


IMAGE_MODELS = (
    "gpt-image-2",
    "gpt-image-1",
    "dall-e-3",
    "imagen-4.0-generate-001",
    "imagen-4.0-fast-generate-001",
    "imagen-4.0-ultra-generate-001",
    "gemini-3.1-flash-image-preview",
)
VIDEO_MODELS = (
    "sora-2",
    "sora-2-pro",
    "veo-3.1-generate-preview",
)
LLM_ROUTING_MODELS = (
    "gpt-5.5",
    "claude-opus-4-7",
    "gemini-3.1-pro-preview",
)

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
VALID_JOB_KINDS = {"image", "edit_image", "video"}
VALID_JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class MediaRoute:
    kind: str
    provider: str
    model_id: str
    configured: bool
    supported: bool
    reason: str = ""


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _as_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=_json_default)


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
        model = str(model_id or "").strip()
        lowered = model.lower()
        if lowered in {"gpt-image-2", "gpt-image-1", "dall-e-3"}:
            return {"kind": "image", "provider": "openai", "model_id": model}
        if lowered.startswith("imagen-4.0-"):
            return {"kind": "image", "provider": "google", "model_id": model}
        if lowered == "gemini-3.1-flash-image-preview":
            return {"kind": "image", "provider": "gemini", "model_id": model}
        if lowered in {"sora-2", "sora-2-pro"}:
            return {"kind": "video", "provider": "openai", "model_id": model}
        if lowered == "veo-3.1-generate-preview":
            return {"kind": "video", "provider": "google", "model_id": model}
        if lowered == "gpt-5.5":
            return {"kind": "llm", "provider": "codex", "model_id": model}
        if lowered == "claude-opus-4-7":
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
                row = await conn.fetchrow(
                    """
                    SELECT provider, model_id, execution_model_id, execution_backend
                    FROM llm_models
                    WHERE (model_id = $1 OR execution_model_id = $1)
                      AND COALESCE(is_active, TRUE) = TRUE
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    model_id,
                )
                return _normalize_job_row(row) if row else None
        except Exception:
            return None

    def _provider_configured(self, provider: str) -> bool:
        normalized = str(provider or "").lower()
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
        return False

    def _default_model_for(self, kind: str, provider: str | None = None) -> str:
        if kind == "video":
            if provider == "google":
                return "veo-3.1-generate-preview"
            return "sora-2"
        if provider == "openai":
            return "gpt-image-2"
        if provider == "gemini":
            return "gemini-3.1-flash-image-preview"
        return "imagen-4.0-generate-001"

    async def resolve_route(
        self,
        kind: str,
        *,
        model_id: str | None = None,
        provider: str | None = None,
    ) -> MediaRoute:
        normalized_kind = "image" if kind == "edit_image" else str(kind or "").strip()
        requested_model = str(model_id or "").strip()
        requested_provider = str(provider or "").strip().lower()

        db_route = await self._fetch_model_route(requested_model)
        if db_route:
            requested_provider = str(db_route.get("provider") or requested_provider).strip().lower()
            requested_model = str(
                db_route.get("execution_model_id") or db_route.get("model_id") or requested_model
            ).strip()

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

        if not requested_provider:
            requested_provider = "google" if _secret_value(self.settings, "GOOGLE_API_KEY") else "openai"

        configured = self._provider_configured(requested_provider)
        supported = self._route_supported(kind, requested_provider, requested_model)
        reason = ""
        if not configured:
            reason = f"{requested_provider or 'provider'} credentials are not configured"
        elif not supported:
            reason = f"{requested_provider}:{requested_model} is not available in the P0 adapter"
        return MediaRoute(
            kind=kind,
            provider=requested_provider,
            model_id=requested_model,
            configured=configured,
            supported=supported,
            reason=reason,
        )

    def _route_supported(self, kind: str, provider: str, model_id: str) -> bool:
        if kind == "image":
            return provider in {"openai", "google"} and (
                model_id in {"gpt-image-2", "gpt-image-1", "dall-e-3"}
                or model_id.startswith("imagen-4.0-")
            )
        if kind == "edit_image":
            return provider == "openai" and model_id in {"gpt-image-2", "gpt-image-1"}
        if kind == "video":
            return False
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
            metadata.update({"provider": route.provider, "model_id": route.model_id})
        await self.update_job_status(
            str(job.get("job_id") or ""),
            "failed",
            result_metadata=metadata,
            error_message=message,
        )
        return self._job_error(code=code, message=message, job=job, route=route)

    async def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        *,
        model_id: str | None = None,
        provider: str | None = None,
        requested_by: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not str(prompt or "").strip():
            raise ValueError("프롬프트를 입력하세요")
        route = await self.resolve_route("image", model_id=model_id, provider=provider)
        job = await self._insert_job(
            kind="image",
            provider=route.provider,
            model_id=route.model_id,
            prompt=prompt,
            input_refs={"size": size},
            status="running",
            requested_by=requested_by,
            session_id=session_id,
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
        try:
            if model_id or provider:
                result = await self._generate_image_with_route(prompt, size, route)
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
            await self.update_job_status(
                str(job.get("job_id") or ""),
                "succeeded",
                result_uri=result.get("url"),
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
    ) -> dict[str, Any]:
        sanitized = _sanitize_prompt(prompt)
        if route.provider == "openai":
            return await self._generate_openai_image(sanitized, prompt, size, route.model_id)
        if route.provider == "google":
            return await self._generate_google_image(sanitized, prompt, route.model_id)
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
    ) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_secret_value(self.settings, "GOOGLE_API_KEY"))
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_images(
                model=model_id,
                prompt=sanitized,
                config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="1:1"),
            ),
        )
        if not response.generated_images:
            raise ValueError("No images returned from Google Imagen")
        image_bytes = response.generated_images[0].image.image_bytes
        b64 = base64.b64encode(image_bytes).decode()
        return {"url": f"data:image/png;base64,{b64}", "provider": model_id, "prompt": original}

    async def edit_image(
        self,
        prompt: str,
        *,
        input_refs: dict[str, Any] | None = None,
        size: str = "1024x1024",
        model_id: str | None = "gpt-image-2",
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
            status="running",
            requested_by=requested_by,
            session_id=session_id,
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
            await self.update_job_status(
                str(job.get("job_id") or ""),
                "succeeded",
                result_uri=result.get("url"),
                result_metadata={"provider": route.provider, "model_id": route.model_id, "size": size},
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
        model_id: str | None = "sora-2",
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
        if not route.configured:
            return await self._mark_failed(
                job,
                code="NOT_CONFIGURED",
                message=route.reason,
                route=route,
            )
        return await self._mark_failed(
            job,
            code="PROVIDER_UNAVAILABLE",
            message=route.reason or "Video provider adapter is not available in P0",
            route=route,
        )

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
