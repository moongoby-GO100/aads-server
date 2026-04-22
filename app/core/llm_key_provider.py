from __future__ import annotations

import logging
import os
import time

from app.core.credential_vault import decrypt_value, encrypt_value
from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[str, float]] = {}


def _get_cached_value(key_name: str, *, allow_stale: bool = False) -> str:
    cached = _cache.get(key_name)
    if not cached:
        return ""
    value, expires_at = cached
    if allow_stale or expires_at > time.time():
        return value
    _cache.pop(key_name, None)
    return ""


def _set_cached_value(key_name: str, value: str) -> None:
    _cache[key_name] = (value, time.time() + _CACHE_TTL_SECONDS)


async def get_api_key(key_name: str, fallback_env: str = "") -> str:
    """DB에서 key_name으로 조회 → 복호화 반환. DB 실패 시 env 폴백."""
    cached = _get_cached_value(key_name)
    if cached:
        return cached

    try:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT encrypted_value
            FROM llm_api_keys
            WHERE key_name = $1
              AND is_active = TRUE
            LIMIT 1
            """,
            key_name,
        )
        if row and row["encrypted_value"]:
            value = decrypt_value(row["encrypted_value"])
            _set_cached_value(key_name, value)
            try:
                await pool.execute(
                    """
                    UPDATE llm_api_keys
                    SET last_used_at = NOW(),
                        updated_at = NOW()
                    WHERE key_name = $1
                    """,
                    key_name,
                )
            except Exception:
                logger.exception("llm_key_provider.get_api_key.touch_failed", extra={"key_name": key_name})
            return value
    except Exception:
        cached = _get_cached_value(key_name, allow_stale=True)
        if cached:
            logger.warning("llm_key_provider.get_api_key.cache_fallback", extra={"key_name": key_name})
            return cached
        logger.exception("llm_key_provider.get_api_key.db_failed", extra={"key_name": key_name})

    env_name = fallback_env or key_name
    return os.getenv(env_name, "")


async def get_provider_keys(provider: str) -> list[str]:
    """provider별 활성 키 목록 (priority 순). rate_limited_until 지난 것만."""
    try:
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT key_name, encrypted_value
            FROM llm_api_keys
            WHERE provider = $1
              AND is_active = TRUE
              AND (rate_limited_until IS NULL OR rate_limited_until <= NOW())
            ORDER BY priority ASC, id ASC
            """,
            provider,
        )
    except Exception:
        logger.exception("llm_key_provider.get_provider_keys.db_failed", extra={"provider": provider})
        return []

    values: list[str] = []
    for row in rows:
        try:
            value = decrypt_value(row["encrypted_value"])
        except Exception:
            logger.exception(
                "llm_key_provider.get_provider_keys.decrypt_failed",
                extra={"provider": provider, "key_name": row["key_name"]},
            )
            continue
        _set_cached_value(row["key_name"], value)
        values.append(value)
    return values


async def mark_key_rate_limited(key_name: str, seconds: int = 300):
    """rate_limited_until = NOW() + seconds 업데이트."""
    pool = get_pool()
    await pool.execute(
        """
        UPDATE llm_api_keys
        SET rate_limited_until = NOW() + ($2::int * INTERVAL '1 second'),
            updated_at = NOW()
        WHERE key_name = $1
        """,
        key_name,
        max(1, seconds),
    )


async def store_api_key(
    key_name: str,
    plaintext_value: str,
    provider: str,
    label: str = "",
    priority: int = 1,
):
    """키 암호화 저장 (UPSERT)."""
    encrypted_value = encrypt_value(plaintext_value)
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO llm_api_keys (
            provider,
            key_name,
            encrypted_value,
            label,
            priority,
            is_active,
            rate_limited_until,
            last_verified_at,
            updated_at
        )
        VALUES ($1, $2, $3, $4, $5, TRUE, NULL, NOW(), NOW())
        ON CONFLICT (key_name)
        DO UPDATE SET
            provider = EXCLUDED.provider,
            encrypted_value = EXCLUDED.encrypted_value,
            label = EXCLUDED.label,
            priority = EXCLUDED.priority,
            is_active = TRUE,
            rate_limited_until = NULL,
            last_verified_at = NOW(),
            updated_at = NOW()
        """,
        provider,
        key_name,
        encrypted_value,
        label,
        priority,
    )
    _set_cached_value(key_name, plaintext_value)
