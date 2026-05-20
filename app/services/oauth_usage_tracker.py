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
    "unified_status",
    "unified_5h_status",
    "unified_5h_utilization",
    "unified_5h_reset",
    "unified_7d_status",
    "unified_7d_utilization",
    "unified_7d_reset",
    "unified_fallback",
    "unified_fallback_pct",
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

    def _float(key: str) -> Optional[float]:
        val = headers.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
        return None

    def _epoch(key: str) -> Optional[datetime]:
        val = headers.get(key)
        if val is not None:
            try:
                return datetime.fromtimestamp(int(val), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
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
        "unified_status": headers.get("anthropic-ratelimit-unified-status"),
        "unified_5h_status": headers.get("anthropic-ratelimit-unified-5h-status"),
        "unified_5h_utilization": _float("anthropic-ratelimit-unified-5h-utilization"),
        "unified_5h_reset": _epoch("anthropic-ratelimit-unified-5h-reset"),
        "unified_7d_status": headers.get("anthropic-ratelimit-unified-7d-status"),
        "unified_7d_utilization": _float("anthropic-ratelimit-unified-7d-utilization"),
        "unified_7d_reset": _epoch("anthropic-ratelimit-unified-7d-reset"),
        "unified_fallback": headers.get("anthropic-ratelimit-unified-representative-claim"),
        "unified_fallback_pct": _float("anthropic-ratelimit-unified-fallback-percentage"),
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
        rl.get("unified_status"),
        rl.get("unified_5h_status"),
        rl.get("unified_5h_utilization"),
        rl.get("unified_5h_reset"),
        rl.get("unified_7d_status"),
        rl.get("unified_7d_utilization"),
        rl.get("unified_7d_reset"),
        rl.get("unified_fallback"),
        rl.get("unified_fallback_pct"),
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

        latest_unified_stats = await conn.fetchrow("""
            SELECT unified_5h_status, unified_5h_utilization,
                   unified_5h_reset, unified_7d_status,
                   unified_7d_utilization, unified_7d_reset
            FROM oauth_usage_log
            WHERE unified_5h_utilization IS NOT NULL
            ORDER BY id DESC LIMIT 1
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

    # Claude Max utilization — 실제 API 우선, 폴백으로 DB 추정
    import os as _os
    plan_limit_5h = int(_os.getenv("CLAUDE_MAX_5H_TOKEN_LIMIT", "5000000"))
    plan_limit_1w = int(_os.getenv("CLAUDE_MAX_1W_TOKEN_LIMIT", "50000000"))
    tok_5h = sum(int(r.get("total_tokens", 0)) for r in rows_5h)
    tok_1w = sum(int(r.get("total_tokens", 0)) for r in rows_1w)
    db_pct_5h = round(min(tok_5h / plan_limit_5h, 1.0) * 100, 1) if plan_limit_5h else 0
    db_pct_1w = round(min(tok_1w / plan_limit_1w, 1.0) * 100, 1) if plan_limit_1w else 0

    # 1순위: claude.ai API (실시간), 2순위: anthropic_header, 3순위: DB 추정
    try:
        real_usage = await fetch_claude_ai_usage()
    except Exception as _e:
        logger.warning("fetch_claude_ai_usage failed in stats: %s", str(_e)[:100])
        real_usage = None

    if real_usage:
        u5 = real_usage["five_hour"]["utilization"]
        u7 = real_usage["seven_day"]["utilization"]
        used_pct_5h = round(u5 * 100, 1) if u5 <= 1.0 else round(u5, 1)
        used_pct_1w = round(u7 * 100, 1) if u7 <= 1.0 else round(u7, 1)
        resets_at_5h = real_usage["five_hour"].get("resets_at")
        resets_at_1w = real_usage["seven_day"].get("resets_at")
        usage_source = "claude_ai_api"
    elif latest_unified_stats and latest_unified_stats["unified_5h_utilization"] is not None:
        used_pct_5h = round(latest_unified_stats["unified_5h_utilization"] * 100, 1)
        used_pct_1w = round((latest_unified_stats["unified_7d_utilization"] or 0) * 100, 1)
        resets_at_5h = latest_unified_stats["unified_5h_reset"].astimezone(KST).isoformat() if latest_unified_stats["unified_5h_reset"] else None
        resets_at_1w = latest_unified_stats["unified_7d_reset"].astimezone(KST).isoformat() if latest_unified_stats["unified_7d_reset"] else None
        usage_source = "anthropic_header"
    else:
        used_pct_5h = db_pct_5h
        used_pct_1w = db_pct_1w
        resets_at_5h = None
        resets_at_1w = None
        usage_source = "db_estimate"

    return {
        "token_labels": token_labels,
        "window_5h": [_row_to_dict(r) for r in rows_5h],
        "window_1w": [_row_to_dict(r) for r in rows_1w],
        "by_model_5h": [_row_to_dict(r) for r in rows_model],
        "latest_ratelimit": [_row_to_dict(r) for r in rows_latest_rl],
        "hourly_trend": [_row_to_dict(r) for r in rows_hourly],
        "errors_5h": [_row_to_dict(r) for r in rows_errors],
        "claude_max": {
            "plan_type": _os.getenv("CLAUDE_MAX_PLAN_TYPE", "max_20x"),
            "source": usage_source,
            "primary": {
                "used_percent": used_pct_5h,
                "window_minutes": 300,
                "total_tokens": tok_5h,
                "resets_at": resets_at_5h,
            },
            "secondary": {
                "used_percent": used_pct_1w,
                "window_minutes": 10080,
                "total_tokens": tok_1w,
                "resets_at": resets_at_1w,
            },
            "plan_limits": {"token_5h": plan_limit_5h, "token_1w": plan_limit_1w},
        },
        "generated_at": datetime.now(KST).isoformat(),
    }


async def get_claude_max_usage() -> Dict[str, Any]:
    """Claude Max 5h/1w 사용량 — Codex 호환 포맷.

    내부 oauth_usage_log 기반 실측 + unified_5h/7d 컬럼 활용.
    """
    import os
    pool = get_pool()
    now = datetime.now(timezone.utc)
    now_kst = now.astimezone(KST)

    async with pool.acquire() as conn:
        row_5h = await conn.fetchrow("""
            SELECT COUNT(*) as calls,
                   COALESCE(SUM(input_tokens), 0) as input_tok,
                   COALESCE(SUM(output_tokens), 0) as output_tok,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as total_tok,
                   COALESCE(SUM(cost_usd), 0) as cost
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '5 hours'
              AND error_code IS NULL
        """)
        row_1w = await conn.fetchrow("""
            SELECT COUNT(*) as calls,
                   COALESCE(SUM(input_tokens), 0) as input_tok,
                   COALESCE(SUM(output_tokens), 0) as output_tok,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as total_tok,
                   COALESCE(SUM(cost_usd), 0) as cost
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '7 days'
              AND error_code IS NULL
        """)
        latest_unified = await conn.fetchrow("""
            SELECT unified_5h_status, unified_5h_utilization,
                   unified_5h_reset, unified_7d_status,
                   unified_7d_utilization, unified_7d_reset
            FROM oauth_usage_log
            WHERE unified_5h_utilization IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """)
        by_model = await conn.fetch("""
            SELECT model, COUNT(*) as calls,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as total_tok
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '5 hours'
              AND error_code IS NULL
            GROUP BY model ORDER BY total_tok DESC LIMIT 10
        """)

    plan_limit_5h = int(os.getenv("CLAUDE_MAX_5H_TOKEN_LIMIT", "5000000"))
    plan_limit_1w = int(os.getenv("CLAUDE_MAX_1W_TOKEN_LIMIT", "50000000"))

    tok_5h = int(row_5h["total_tok"])
    tok_1w = int(row_1w["total_tok"])
    db_pct_5h = round(min(tok_5h / plan_limit_5h, 1.0) * 100, 1) if plan_limit_5h else 0
    db_pct_1w = round(min(tok_1w / plan_limit_1w, 1.0) * 100, 1) if plan_limit_1w else 0

    # 1순위: claude.ai API (실시간), 2순위: anthropic_header, 3순위: DB 추정
    try:
        real_usage = await fetch_claude_ai_usage()
    except Exception as _e:
        logger.warning("fetch_claude_ai_usage failed: %s", str(_e)[:100])
        real_usage = None

    if real_usage:
        u5 = real_usage["five_hour"]["utilization"]
        u7 = real_usage["seven_day"]["utilization"]
        used_pct_5h = round(u5 * 100, 1) if u5 <= 1.0 else round(u5, 1)
        used_pct_1w = round(u7 * 100, 1) if u7 <= 1.0 else round(u7, 1)
        resets_at_5h_iso = real_usage["five_hour"].get("resets_at")
        resets_at_1w_iso = real_usage["seven_day"].get("resets_at")
        try:
            r5 = datetime.fromisoformat(resets_at_5h_iso) if resets_at_5h_iso else now + timedelta(hours=5)
            r1w = datetime.fromisoformat(resets_at_1w_iso) if resets_at_1w_iso else now + timedelta(days=7)
        except Exception:
            r5 = now + timedelta(hours=5)
            r1w = now + timedelta(days=7)
        usage_source = "claude_ai_api"
    elif latest_unified and latest_unified["unified_5h_utilization"] is not None:
        used_pct_5h = round(latest_unified["unified_5h_utilization"] * 100, 1)
        used_pct_1w = round((latest_unified["unified_7d_utilization"] or 0) * 100, 1)
        r5_reset = latest_unified["unified_5h_reset"]
        r1w_reset = latest_unified["unified_7d_reset"]
        r5 = r5_reset if r5_reset else now + timedelta(hours=5)
        r1w = r1w_reset if r1w_reset else now + timedelta(days=7)
        resets_at_5h_iso = r5.isoformat()
        resets_at_1w_iso = r1w.isoformat()
        usage_source = "anthropic_header"
    else:
        used_pct_5h = db_pct_5h
        used_pct_1w = db_pct_1w
        r5 = now + timedelta(hours=5)
        r1w = now + timedelta(days=7)
        resets_at_5h_iso = r5.isoformat()
        resets_at_1w_iso = r1w.isoformat()
        usage_source = "db_estimate"

    unified = {}
    if latest_unified:
        try:
            unified = {
                "source": "anthropic_header",
                "status_5h": latest_unified["unified_5h_status"],
                "utilization_5h": latest_unified["unified_5h_utilization"],
                "reset_5h": latest_unified["unified_5h_reset"].astimezone(KST).isoformat() if latest_unified["unified_5h_reset"] else None,
                "status_7d": latest_unified["unified_7d_status"],
                "utilization_7d": latest_unified["unified_7d_utilization"],
                "reset_7d": latest_unified["unified_7d_reset"].astimezone(KST).isoformat() if latest_unified["unified_7d_reset"] else None,
            }
        except Exception:
            pass

    return {
        "ok": True,
        "source": usage_source,
        "plan_type": os.getenv("CLAUDE_MAX_PLAN_TYPE", "max_20x"),
        "limits": [{
            "limit_id": "claude_max",
            "plan_type": os.getenv("CLAUDE_MAX_PLAN_TYPE", "max_20x"),
            "primary": {
                "used_percent": used_pct_5h,
                "window_minutes": 300,
                "resets_at_epoch": int(r5.timestamp()),
                "resets_at_iso": resets_at_5h_iso,
                "resets_in_sec": max(0, int(r5.timestamp() - now.timestamp())),
                "calls": int(row_5h["calls"]),
                "total_tokens": tok_5h,
                "cost_usd": float(row_5h["cost"]),
            },
            "secondary": {
                "used_percent": used_pct_1w,
                "window_minutes": 10080,
                "resets_at_epoch": int(r1w.timestamp()),
                "resets_at_iso": resets_at_1w_iso,
                "resets_in_sec": max(0, int(r1w.timestamp() - now.timestamp())),
                "calls": int(row_1w["calls"]),
                "total_tokens": tok_1w,
                "cost_usd": float(row_1w["cost"]),
            },
        }],
        "by_model_5h": [{"model": r["model"], "calls": int(r["calls"]), "tokens": int(r["total_tok"])} for r in by_model],
        "plan_limits": {"token_5h": plan_limit_5h, "token_1w": plan_limit_1w},
        "last_anthropic_unified": unified or None,
        "fetched_at": now_kst.isoformat(),
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


# ── Claude.ai 실시간 사용량 API ──────────────────────────────────────
_CLAUDE_AI_USAGE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CLAUDE_AI_USAGE_TTL = 60


async def fetch_claude_ai_usage() -> Optional[Dict[str, Any]]:
    """claude.ai/api/organizations/{org_id}/usage 실시간 조회 (60초 캐시)."""
    import os
    import time as _t
    import httpx

    now = _t.time()
    cached = _CLAUDE_AI_USAGE_CACHE.get("data")
    if cached and (now - _CLAUDE_AI_USAGE_CACHE["ts"]) < _CLAUDE_AI_USAGE_TTL:
        return cached

    session_key = os.getenv("CLAUDE_SESSION_KEY", "")
    org_id = os.getenv("CLAUDE_ORG_ID", "")
    if not session_key or not org_id:
        try:
            from dotenv import dotenv_values
            for p in ("/app/.env", "/root/aads/aads-server/.env"):
                if os.path.exists(p):
                    env = dotenv_values(p)
                    break
            else:
                env = {}
            session_key = session_key or env.get("CLAUDE_SESSION_KEY", "")
            org_id = org_id or env.get("CLAUDE_ORG_ID", "")
        except Exception:
            pass
    if not session_key or not org_id:
        return None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            resp = await client.get(
                f"https://claude.ai/api/organizations/{org_id}/usage",
                cookies={"sessionKey": session_key, "lastActiveOrg": org_id},
                headers={
                    "accept": "application/json",
                    "anthropic-client-platform": "web",
                    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                },
            )
        if resp.status_code != 200:
            logger.warning("claude_ai_usage_fetch_failed: %d", resp.status_code)
            return cached

        data = resp.json()
        five_h = data.get("five_hour") or {}
        seven_d = data.get("seven_day") or {}
        result = {
            "five_hour": {
                "utilization": five_h.get("utilization", 0),
                "resets_at": five_h.get("resets_at"),
            },
            "seven_day": {
                "utilization": seven_d.get("utilization", 0),
                "resets_at": seven_d.get("resets_at"),
            },
            "source": "claude_ai_api",
            "fetched_at": datetime.now(KST).isoformat(),
        }
        _CLAUDE_AI_USAGE_CACHE["data"] = result
        _CLAUDE_AI_USAGE_CACHE["ts"] = now
        return result
    except Exception as e:
        logger.warning("claude_ai_usage_error: %s", str(e)[:120])
        return cached
