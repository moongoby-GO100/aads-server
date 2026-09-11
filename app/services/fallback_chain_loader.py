"""
DB 기반 폴백 체인 로더 — model_routing_preferences 단일 소스.
30초 캐시, invalidate_fallback_cache()로 즉시 무효화.
PRD: docs/PRD-AADS-FALLBACK-OPS-v1.0.md
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_TTL = 30
_cache: dict[str, tuple[list["FallbackEntry"], float]] = {}


@dataclass(frozen=True)
class FallbackEntry:
    provider: str
    model_id: str
    display_order: int
    is_enabled: bool
    is_default: bool
    display_name: Optional[str]
    notes: str


def invalidate_fallback_cache(route_key: Optional[str] = None) -> None:
    if route_key:
        _cache.pop(route_key, None)
    else:
        _cache.clear()
    logger.info("fallback_cache_invalidated: route_key=%s", route_key or "ALL")


async def get_fallback_chain(route_key: str) -> list[FallbackEntry]:
    cached = _cache.get(route_key)
    if cached:
        entries, expires_at = cached
        if time.time() < expires_at:
            return entries

    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT provider, model_id, display_order, is_enabled, is_default,
                   display_name, notes
            FROM model_routing_preferences
            WHERE route_key = $1 AND is_enabled = TRUE
            ORDER BY display_order
            """,
            route_key,
        )
        entries = [
            FallbackEntry(
                provider=r["provider"],
                model_id=r["model_id"],
                display_order=r["display_order"],
                is_enabled=r["is_enabled"],
                is_default=r["is_default"],
                display_name=r.get("display_name"),
                notes=r.get("notes", ""),
            )
            for r in rows
        ]
        _cache[route_key] = (entries, time.time() + _CACHE_TTL)
        return entries
    except Exception as e:
        logger.warning("fallback_chain_db_error: route_key=%s error=%s", route_key, str(e)[:120])
        cached_stale = _cache.get(route_key)
        if cached_stale:
            return cached_stale[0]
        return []


async def get_fallback_model_ids(route_key: str) -> list[str]:
    chain = await get_fallback_chain(route_key)
    return [e.model_id for e in chain]


async def get_default_model(route_key: str) -> Optional[str]:
    chain = await get_fallback_chain(route_key)
    for entry in chain:
        if entry.is_default:
            return entry.model_id
    return chain[0].model_id if chain else None


async def get_non_default_models(route_key: str) -> list[str]:
    chain = await get_fallback_chain(route_key)
    return [e.model_id for e in chain if not e.is_default]
