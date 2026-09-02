"""DB-backed AI capability routing helpers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

AI_ROUTE_KEYS = (
    "llm",
    "background_llm",
    "runner_llm",
    "search",
    "deep_research",
    "url_analyze",
    "image_analyze",
    "video_analyze",
    "image",
    "edit_image",
    "video",
    "embedding",
    "semantic_search",
    "visual_qa",
    "fact_check",
    "code_exec",
    "audio",
    "music",
)

ROUTE_GROUPS: dict[str, str] = {
    "llm": "text",
    "background_llm": "text",
    "runner_llm": "runner",
    "search": "research",
    "deep_research": "research",
    "url_analyze": "research",
    "fact_check": "research",
    "image_analyze": "multimodal",
    "video_analyze": "multimodal",
    "visual_qa": "multimodal",
    "image": "media",
    "edit_image": "media",
    "video": "media",
    "audio": "media",
    "music": "media",
    "embedding": "memory",
    "semantic_search": "memory",
    "code_exec": "tools",
}

GOOGLE_PROVIDERS = {"google", "gemini"}


@dataclass(frozen=True)
class AIRouteCandidate:
    route_key: str
    provider: str
    model_id: str
    display_order: int
    is_default: bool
    availability: str = "unknown"
    execution_model_id: str | None = None
    notes: str = ""

    @property
    def runtime_model(self) -> str:
        return self.execution_model_id or self.model_id


async def get_route_candidates(
    route_key: str,
    *,
    include_google: bool = False,
    enabled_only: bool = True,
) -> list[AIRouteCandidate]:
    """Return ordered DB route candidates for an AI capability."""
    if route_key not in AI_ROUTE_KEYS:
        logger.warning("unknown_ai_route_key route=%s", route_key)
        return []

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pref.route_key, pref.provider, pref.model_id,
                       pref.display_order, pref.is_default, pref.is_enabled,
                       pref.notes,
                       models.execution_model_id, models.is_active,
                       models.is_executable, models.verification_status
                FROM model_routing_preferences AS pref
                LEFT JOIN LATERAL (
                    SELECT m.execution_model_id, m.is_active, m.is_executable,
                           m.verification_status, m.updated_at, m.id
                    FROM llm_models AS m
                    WHERE m.provider = pref.provider
                      AND (m.model_id = pref.model_id OR m.execution_model_id = pref.model_id)
                    ORDER BY CASE WHEN m.model_id = pref.model_id THEN 0 ELSE 1 END,
                             m.updated_at DESC NULLS LAST, m.id DESC
                    LIMIT 1
                ) AS models ON TRUE
                WHERE pref.route_key = $1
                  AND ($2::boolean = FALSE OR pref.is_enabled = TRUE)
                ORDER BY pref.is_default DESC, pref.display_order ASC, pref.provider ASC, pref.model_id ASC
                """,
                route_key,
                enabled_only,
            )
    except Exception as exc:
        logger.warning("ai_route_candidates_failed route=%s error=%s", route_key, str(exc)[:160])
        return []

    candidates: list[AIRouteCandidate] = []
    for row in rows:
        provider = str(row["provider"] or "").strip().lower()
        if not include_google and provider in GOOGLE_PROVIDERS:
            continue
        if enabled_only and not row["is_enabled"]:
            continue
        verification_status = str(row["verification_status"] or "").strip().lower()
        if not row["is_active"] and not row["is_executable"]:
            availability = "not_configured"
        elif verification_status in {"disabled_billing_depleted", "auth_required", "rate_limited"}:
            availability = verification_status
        else:
            availability = "available"
        candidates.append(
            AIRouteCandidate(
                route_key=row["route_key"],
                provider=provider,
                model_id=row["model_id"],
                display_order=row["display_order"],
                is_default=row["is_default"],
                availability=availability,
                execution_model_id=row["execution_model_id"],
                notes=row["notes"] or "",
            )
        )
    return candidates


async def get_first_route_candidate(
    route_key: str,
    *,
    include_google: bool = False,
) -> AIRouteCandidate | None:
    candidates = await get_route_candidates(route_key, include_google=include_google)
    return candidates[0] if candidates else None


def normalize_embedding_dimension(vector: list[float], dim: int = 768) -> list[float]:
    """Keep pgvector dimensions stable across local/external embedding providers."""
    if len(vector) == dim:
        return vector
    if len(vector) > dim:
        return vector[:dim]
    return vector + [0.0] * (dim - len(vector))
