"""
OAuth 사용량 추적 모듈 — AADS-192.

Anthropic API 응답 헤더에서 rate-limit 정보를 추출하고,
5시간/1주일 롤링 윈도우 사용량을 DB에 기록·조회.

사용:
    from app.services.oauth_usage_tracker import log_usage, get_usage_stats
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.db_pool import get_pool
from app.core.auth_provider import get_oauth_tokens, get_token_labels

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
USAGE_LOG_FLUSH_BATCH_SIZE = 10
USAGE_LOG_FLUSH_INTERVAL_SEC = 30.0

_USAGE_LOG_BUFFER: List[Dict[str, Any]] = []
_USAGE_LOG_LOCK = asyncio.Lock()
_USAGE_LOG_FLUSH_TASK: Optional[asyncio.Task] = None
_USAGE_LOG_COLUMNS: Tuple[str, ...] = (
    "account_slot",
    "token_prefix",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "cost_usd",
    "rl_requests_limit",
    "rl_requests_remaining",
    "rl_requests_reset",
    "rl_tokens_limit",
    "rl_tokens_remaining",
    "rl_tokens_reset",
    "rl_input_tokens_limit",
    "rl_input_tokens_remaining",
    "rl_input_tokens_reset",
    "rl_output_tokens_limit",
    "rl_output_tokens_remaining",
    "rl_output_tokens_reset",
    "call_source",
    "session_id",
    "error_code",
    "duration_ms",
)

# ── 토큰 → 슬롯 매핑 ──────────────────────────────────────────────────

def _token_slot(token: str) -> str:
    """토큰이 primary인지 fallback인지 판별."""
    tokens = get_oauth_tokens()
    if not tokens:
        return "unknown"
    if token and len(tokens) > 0 and token[:20] == tokens[0][:20]:
        return "primary"
    if token and len(tokens) > 1 and token[:20] == tokens[1][:20]:
        return "fallback"
    return "unknown"


def _token_prefix(token: str) -> str:
    return token[:12] + "..." if token else ""


# ── 헤더 파싱 ────────────────────────────────────────────────────────────

def parse_ratelimit_headers(headers: Any) -> Dict[str, Any]:
    """Anthropic API 응답 헤더에서 rate-limit 정보 추출."""
    if headers is None:
        return {}

    def _int(key: str) -> Optional[int]:
        val = headers.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
        return None

    def _ts(key: str) -> Optional[datetime]:
        val = headers.get(key)
        if val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                pass
        return None

    return {
        "rl_requests_limit": _int("anthropic-ratelimit-requests-limit"),
        "rl_requests_remaining": _int("anthropic-ratelimit-requests-remaining"),
        "rl_requests_reset": _ts("anthropic-ratelimit-requests-reset"),
        "rl_tokens_limit": _int("anthropic-ratelimit-tokens-limit"),
        "rl_tokens_remaining": _int("anthropic-ratelimit-tokens-remaining"),
        "rl_tokens_reset": _ts("anthropic-ratelimit-tokens-reset"),
        "rl_input_tokens_limit": _int("anthropic-ratelimit-input-tokens-limit"),
        "rl_input_tokens_remaining": _int("anthropic-ratelimit-input-tokens-remaining"),
        "rl_input_tokens_reset": _ts("anthropic-ratelimit-input-tokens-reset"),
        "rl_output_tokens_limit": _int("anthropic-ratelimit-output-tokens-limit"),
        "rl_output_tokens_remaining": _int("anthropic-ratelimit-output-tokens-remaining"),
        "rl_output_tokens_reset": _ts("anthropic-ratelimit-output-tokens-reset"),
    }


# ── DB 기록 (fire-and-forget) ─────────────────────────────────────────

def _usage_log_values(entry: Dict[str, Any]) -> Tuple[Any, ...]:
    rl = entry["rl"]
    return (
        entry["account_slot"],
        entry["token_prefix"],
        entry["model"],
        entry["input_tokens"],
        entry["output_tokens"],
        entry["cache_creation_tokens"],
        entry["cache_read_tokens"],
        float(entry["cost_usd"]),
        rl.get("rl_requests_limit"),
        rl.get("rl_requests_remaining"),
        rl.get("rl_requests_reset"),
        rl.get("rl_tokens_limit"),
        rl.get("rl_tokens_remaining"),
        rl.get("rl_tokens_reset"),
        rl.get("rl_input_tokens_limit"),
        rl.get("rl_input_tokens_remaining"),
        rl.get("rl_input_tokens_reset"),
        rl.get("rl_output_tokens_limit"),
        rl.get("rl_output_tokens_remaining"),
        rl.get("rl_output_tokens_reset"),
        entry["call_source"],
        entry["session_id"],
        entry["error_code"],
        entry["duration_ms"],
    )


async def _insert_usage_batch(entries: List[Dict[str, Any]]) -> None:
    """배치 INSERT."""
    if not entries:
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        chunk_size = 100
        column_sql = ", ".join(_USAGE_LOG_COLUMNS)
        column_count = len(_USAGE_LOG_COLUMNS)
        for chunk_start in range(0, len(entries), chunk_size):
            chunk = entries[chunk_start:chunk_start + chunk_size]
            values_sql: List[str] = []
            args: List[Any] = []
            for row_index, entry in enumerate(chunk):
                base = row_index * column_count
                placeholders = ", ".join(
                    f"${base + column_index + 1}" for column_index in range(column_count)
                )
                values_sql.append(f"({placeholders})")
                args.extend(_usage_log_values(entry))
            await conn.execute(
                f"INSERT INTO oauth_usage_log ({column_sql}) VALUES {', '.join(values_sql)}",
                *args,
            )


async def _drain_usage_buffer() -> List[Dict[str, Any]]:
    async with _USAGE_LOG_LOCK:
        if not _USAGE_LOG_BUFFER:
            return []
        entries = list(_USAGE_LOG_BUFFER)
        _USAGE_LOG_BUFFER.clear()
        return entries


async def _flush_usage_buffer() -> None:
    entries = await _drain_usage_buffer()
    if not entries:
        return

    try:
        await _insert_usage_batch(entries)
    except Exception as e:
        async with _USAGE_LOG_LOCK:
            _USAGE_LOG_BUFFER[:0] = entries
        logger.warning("oauth_usage_batch_insert_failed: %s", str(e)[:120])


async def _usage_flush_loop() -> None:
    try:
        while True:
            await asyncio.sleep(USAGE_LOG_FLUSH_INTERVAL_SEC)
            await _flush_usage_buffer()
    except asyncio.CancelledError:
        await _flush_usage_buffer()
        raise


def _ensure_flush_task() -> None:
    global _USAGE_LOG_FLUSH_TASK

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    if _USAGE_LOG_FLUSH_TASK and not _USAGE_LOG_FLUSH_TASK.done():
        task_loop = _USAGE_LOG_FLUSH_TASK.get_loop()
        if task_loop is loop:
            return

    _USAGE_LOG_FLUSH_TASK = loop.create_task(_usage_flush_loop())


async def _enqueue_usage(entry: Dict[str, Any]) -> None:
    _ensure_flush_task()

    entries_to_flush: List[Dict[str, Any]] = []
    async with _USAGE_LOG_LOCK:
        _USAGE_LOG_BUFFER.append(entry)
        if len(_USAGE_LOG_BUFFER) >= USAGE_LOG_FLUSH_BATCH_SIZE:
            entries_to_flush = list(_USAGE_LOG_BUFFER)
            _USAGE_LOG_BUFFER.clear()

    if entries_to_flush:
        try:
            await _insert_usage_batch(entries_to_flush)
        except Exception as e:
            async with _USAGE_LOG_LOCK:
                _USAGE_LOG_BUFFER[:0] = entries_to_flush
            logger.warning("oauth_usage_batch_insert_failed: %s", str(e)[:120])


def log_usage(
    token: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    cost_usd: float = 0.0,
    headers: Any = None,
    call_source: str = "",
    session_id: str = "",
    error_code: Optional[str] = None,
    duration_ms: int = 0,
) -> None:
    """사용량 기록 (buffered fire-and-forget). LLM 호출 직후 호출."""
    rl = parse_ratelimit_headers(headers)
    slot = _token_slot(token)
    prefix = _token_prefix(token)

    # 선제적 계정 전환 경고
    remaining = rl.get("rl_tokens_remaining")
    if remaining is not None and remaining < 10000:
        logger.warning(
            "oauth_usage_low_tokens: slot=%s remaining=%d — 선제적 전환 권장",
            slot, remaining,
        )

    entry = {
        "account_slot": slot,
        "token_prefix": prefix,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cost_usd": cost_usd,
        "rl": rl,
        "call_source": call_source,
        "session_id": session_id or "",
        "error_code": error_code,
        "duration_ms": duration_ms,
    }

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("oauth_usage_log_skipped_no_running_loop: model=%s source=%s", model, call_source)
        return

    _ensure_flush_task()
    loop.create_task(_enqueue_usage(entry))


# ── 조회: 5시간/1주일 롤링 윈도우 ─────────────────────────────────────

async def get_usage_stats() -> Dict[str, Any]:
    """5시간/1주일 사용량 통계 + 최신 rate-limit 상태."""
    pool = get_pool()
    async with pool.acquire() as conn:
        # 5시간 윈도우
        rows_5h = await conn.fetch("""
            SELECT account_slot,
                   COUNT(*) as calls,
                   COALESCE(SUM(input_tokens), 0) as total_input,
                   COALESCE(SUM(output_tokens), 0) as total_output,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens,
                   COALESCE(SUM(cost_usd), 0) as total_cost
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '5 hours'
              AND error_code IS NULL
            GROUP BY account_slot
            ORDER BY account_slot
        """)

        # 1주일 윈도우
        rows_1w = await conn.fetch("""
            SELECT account_slot,
                   COUNT(*) as calls,
                   COALESCE(SUM(input_tokens), 0) as total_input,
                   COALESCE(SUM(output_tokens), 0) as total_output,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens,
                   COALESCE(SUM(cost_usd), 0) as total_cost
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '7 days'
              AND error_code IS NULL
            GROUP BY account_slot
            ORDER BY account_slot
        """)

        # 모델별 5시간 사용량
        rows_model = await conn.fetch("""
            SELECT model, account_slot,
                   COUNT(*) as calls,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '5 hours'
              AND error_code IS NULL
            GROUP BY model, account_slot
            ORDER BY total_tokens DESC
        """)

        # 최신 rate-limit 상태 (계정별 마지막 기록)
        rows_latest_rl = await conn.fetch("""
            SELECT DISTINCT ON (account_slot)
                   account_slot, token_prefix, model,
                   rl_requests_limit, rl_requests_remaining, rl_requests_reset,
                   rl_tokens_limit, rl_tokens_remaining, rl_tokens_reset,
                   rl_input_tokens_limit, rl_input_tokens_remaining, rl_input_tokens_reset,
                   rl_output_tokens_limit, rl_output_tokens_remaining, rl_output_tokens_reset,
                   created_at
            FROM oauth_usage_log
            WHERE rl_requests_remaining IS NOT NULL
            ORDER BY account_slot, created_at DESC
        """)

        # 시간대별 사용 추이 (최근 5시간, 30분 단위)
        rows_hourly = await conn.fetch("""
            SELECT date_trunc('hour', created_at) +
                   INTERVAL '30 min' * FLOOR(EXTRACT(MINUTE FROM created_at) / 30) as time_bucket,
                   account_slot,
                   COUNT(*) as calls,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '5 hours'
              AND error_code IS NULL
            GROUP BY time_bucket, account_slot
            ORDER BY time_bucket DESC
        """)

        # 에러 카운트 (최근 5시간)
        rows_errors = await conn.fetch("""
            SELECT account_slot, error_code, COUNT(*) as cnt
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '5 hours'
              AND error_code IS NOT NULL
            GROUP BY account_slot, error_code
            ORDER BY cnt DESC
        """)

    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.astimezone(KST).isoformat()
            elif hasattr(v, '__float__'):
                d[k] = float(v)
        return d

    token_labels = get_token_labels()

    return {
        "token_labels": token_labels,
        "window_5h": [_row_to_dict(r) for r in rows_5h],
        "window_1w": [_row_to_dict(r) for r in rows_1w],
        "by_model_5h": [_row_to_dict(r) for r in rows_model],
        "latest_ratelimit": [_row_to_dict(r) for r in rows_latest_rl],
        "hourly_trend": [_row_to_dict(r) for r in rows_hourly],
        "errors_5h": [_row_to_dict(r) for r in rows_errors],
        "generated_at": datetime.now(KST).isoformat(),
    }


async def should_switch_account(current_token: str) -> bool:
    """현재 토큰의 남은 한도가 임계값 이하인지 확인 → True면 전환 권장."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT rl_tokens_remaining, rl_requests_remaining
            FROM oauth_usage_log
            WHERE account_slot = $1
              AND rl_tokens_remaining IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        """, _token_slot(current_token))

    if row is None:
        return False

    tokens_remaining = row["rl_tokens_remaining"]
    requests_remaining = row["rl_requests_remaining"]

    if tokens_remaining is not None and tokens_remaining < 5000:
        return True
    if requests_remaining is not None and requests_remaining < 3:
        return True
    return False
