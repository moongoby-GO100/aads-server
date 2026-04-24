from __future__ import annotations

import logging
import time as _time_mod
from typing import Any

import asyncpg

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

_FLAG_CACHE_TTL_SECONDS = 60
_FLAG_CACHE: dict[str, dict[str, Any]] = {}


def _cache_get(flag_key: str) -> bool | None:
    cached = _FLAG_CACHE.get(flag_key)
    if not cached:
        return None
    expires_at = float(cached.get("expires_at") or 0.0)
    if expires_at <= _time_mod.monotonic():
        _FLAG_CACHE.pop(flag_key, None)
        return None
    return bool(cached.get("enabled"))


def _cache_set(flag_key: str, enabled: bool) -> None:
    _FLAG_CACHE[flag_key] = {
        "enabled": bool(enabled),
        "expires_at": _time_mod.monotonic() + _FLAG_CACHE_TTL_SECONDS,
    }


def invalidate_flag_cache(flag_key: str | None = None) -> None:
    if flag_key:
        _FLAG_CACHE.pop(flag_key, None)
        return
    _FLAG_CACHE.clear()


async def get_flag(flag_key: str, default: bool = True) -> bool:
    cached = _cache_get(flag_key)
    if cached is not None:
        return cached

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            enabled = await conn.fetchval(
                "SELECT enabled FROM feature_flags WHERE flag_key = $1",
                flag_key,
            )
    except (RuntimeError, asyncpg.UndefinedTableError) as exc:
        logger.warning("feature_flag_lookup_unavailable: %s", exc)
        return default
    except Exception as exc:
        logger.warning("feature_flag_lookup_failed: %s", exc)
        return default

    if enabled is None:
        _cache_set(flag_key, default)
        return default

    resolved = bool(enabled)
    _cache_set(flag_key, resolved)
    return resolved


async def set_flag(flag_key: str, enabled: bool, changed_by: str = "system") -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO feature_flags (flag_key, enabled, last_changed_by, last_changed_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (flag_key) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                last_changed_by = EXCLUDED.last_changed_by,
                last_changed_at = NOW()
            RETURNING flag_key, enabled, scope, last_changed_by, last_changed_at, notes
            """,
            flag_key,
            bool(enabled),
            (changed_by or "system").strip() or "system",
        )

    invalidate_flag_cache(flag_key)
    if row is None:
        raise RuntimeError(f"feature flag update failed: {flag_key}")
    return dict(row)


async def governance_enabled(default: bool = True) -> bool:
    return await get_flag("governance_enabled", default=default)


async def list_flags() -> list[dict[str, Any]]:
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT flag_key, enabled, scope, last_changed_by, last_changed_at, notes
                FROM feature_flags
                ORDER BY flag_key
                """
            )
    except asyncpg.UndefinedTableError:
        return []

    return [dict(row) for row in rows]
