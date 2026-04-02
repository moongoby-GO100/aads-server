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
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from app.core.db_pool import get_pool
from app.core.auth_provider import get_oauth_tokens, get_token_labels

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

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

async def _insert_usage(
    account_slot: str,
    token_prefix: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    cost_usd: float,
    rl: Dict[str, Any],
    call_source: str,
    session_id: str,
    error_code: Optional[str],
    duration_ms: int,
) -> None:
    """DB INSERT — 실패해도 로그만 남기고 예외 전파 안 함."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO oauth_usage_log (
                    account_slot, token_prefix, model,
                    input_tokens, output_tokens,
                    cache_creation_tokens, cache_read_tokens,
                    cost_usd,
                    rl_requests_limit, rl_requests_remaining, rl_requests_reset,
                    rl_tokens_limit, rl_tokens_remaining, rl_tokens_reset,
                    rl_input_tokens_limit, rl_input_tokens_remaining, rl_input_tokens_reset,
                    rl_output_tokens_limit, rl_output_tokens_remaining, rl_output_tokens_reset,
                    call_source, session_id, error_code, duration_ms
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,
                    $9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
                    $21,$22,$23,$24
                )
                """,
                account_slot, token_prefix, model,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                float(cost_usd),
                rl.get("rl_requests_limit"), rl.get("rl_requests_remaining"), rl.get("rl_requests_reset"),
                rl.get("rl_tokens_limit"), rl.get("rl_tokens_remaining"), rl.get("rl_tokens_reset"),
                rl.get("rl_input_tokens_limit"), rl.get("rl_input_tokens_remaining"), rl.get("rl_input_tokens_reset"),
                rl.get("rl_output_tokens_limit"), rl.get("rl_output_tokens_remaining"), rl.get("rl_output_tokens_reset"),
                call_source, session_id or "", error_code, duration_ms,
            )
    except Exception as e:
        logger.warning("oauth_usage_insert_failed: %s", str(e)[:120])


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
    """사용량 기록 (fire-and-forget). LLM 호출 직후 호출."""
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

    asyncio.ensure_future(
        _insert_usage(
            account_slot=slot,
            token_prefix=prefix,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd,
            rl=rl,
            call_source=call_source,
            session_id=session_id,
            error_code=error_code,
            duration_ms=duration_ms,
        )
    )


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
