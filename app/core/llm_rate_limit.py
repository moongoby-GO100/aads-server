"""Anthropic 사용량 한도 스냅샷 — 응답 헤더를 그대로 받아 적는다.

채팅 화면에 "지금 내 계정 한도가 얼마나 남았나" 를 띄우려면 값을 어딘가에서
가져와야 하는데, Anthropic 은 잔량 조회 API 를 주지 않는다. 유일한 출처가 매
응답에 붙어 오는 `anthropic-ratelimit-*` 헤더다. 그래서 호출이 200 으로 성공하든
429 로 튕기든, 그 순간의 헤더를 긁어 마지막 값 하나만 남긴다.

설계 세 가지를 미리 적어둔다. 나중에 "왜 이렇게 했나" 를 다시 묻게 된다.

1. **누적 로그가 아니라 최신 스냅샷 한 행이다.** `(user_id, provider, key_source)`
   유니크로 UPSERT 한다. 헤더는 호출할 때마다 오므로 append 하면 채팅량만큼 행이
   늘어나는데, 화면이 필요로 하는 것은 "마지막 값" 하나뿐이다.

2. **파싱한 컬럼과 별개로 raw JSONB 를 같이 남긴다.** 구독 토큰(`sk-ant-oat`)은
   API 키와 다른 헤더 집합(`unified-*`)을 돌려주고, Anthropic 이 헤더를 추가해도
   우리 스키마는 그것을 모른다. 모르는 헤더를 버리면 나중에 원인을 못 찾는다.

3. **기록은 예외를 밖으로 내보내지 않는다.** 사용량 표시가 실패해서 채팅 스트림이
   끊기면 주객이 전도된다. 실패는 로그만 남기고 조용히 지나간다.

`key_source` 는 `byok`(사용자 본인 키) / `system`(회사 공용 경유) 두 값이다.
BYOK 전용 정책(model_selector `_BYOK_ONLY_ENFORCED`)이 걸린 뒤로 가입 사용자는
사실상 `byok` 만 남지만, ceo/admin 예외가 있어 두 값을 구분해 둔다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_HEADER_PREFIX = "anthropic-ratelimit-"

# 파싱해 컬럼으로 올리는 차원. Anthropic 은 요청 수와 토큰 수를 따로 센다.
_DIMENSIONS = ("requests", "tokens", "input-tokens", "output-tokens")

_TABLE_READY = False

_DDL = """
CREATE TABLE IF NOT EXISTS llm_rate_limit_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'anthropic',
    key_source TEXT NOT NULL DEFAULT 'byok',
    model TEXT,
    status_code INTEGER,
    requests_limit INTEGER,
    requests_remaining INTEGER,
    requests_reset TIMESTAMPTZ,
    tokens_limit BIGINT,
    tokens_remaining BIGINT,
    tokens_reset TIMESTAMPTZ,
    input_tokens_limit BIGINT,
    input_tokens_remaining BIGINT,
    input_tokens_reset TIMESTAMPTZ,
    output_tokens_limit BIGINT,
    output_tokens_remaining BIGINT,
    output_tokens_reset TIMESTAMPTZ,
    unified_status TEXT,
    retry_after_seconds INTEGER,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_rate_limit_snapshot_scope
    ON llm_rate_limit_snapshots(user_id, provider, key_source);
CREATE INDEX IF NOT EXISTS idx_llm_rate_limit_snapshot_observed
    ON llm_rate_limit_snapshots(observed_at DESC);
"""


async def _ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute(_DDL)
    _TABLE_READY = True


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_dt(value: Any) -> Optional[datetime]:
    """RFC 3339(`2026-09-14T11:00:00Z`) 또는 잔여 초(`30`) 둘 다 받는다.

    Anthropic 은 reset 을 절대시각으로 주지만, retry-after 처럼 초로 오는 경우가
    섞여 있어 숫자면 now + N 초로 해석한다.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        from datetime import timedelta

        return datetime.now(timezone.utc) + timedelta(seconds=int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_rate_limit_headers(headers: Mapping[str, str]) -> Dict[str, Any]:
    """`anthropic-ratelimit-*` 헤더를 스냅샷 dict 로 변환.

    헤더가 하나도 없으면 `raw` 가 빈 dict 다 — 호출부는 이것으로 "기록할 게
    없음" 을 판단한다. LiteLLM 경유 응답에는 이 헤더가 붙지 않는 경우가 있다.
    """
    lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    raw = {k: v for k, v in lowered.items() if k.startswith(_HEADER_PREFIX)}

    parsed: Dict[str, Any] = {"raw": raw}
    for dim in _DIMENSIONS:
        column = dim.replace("-", "_")
        parsed[f"{column}_limit"] = _to_int(raw.get(f"{_HEADER_PREFIX}{dim}-limit"))
        parsed[f"{column}_remaining"] = _to_int(raw.get(f"{_HEADER_PREFIX}{dim}-remaining"))
        parsed[f"{column}_reset"] = _to_dt(raw.get(f"{_HEADER_PREFIX}{dim}-reset"))

    parsed["unified_status"] = raw.get(f"{_HEADER_PREFIX}unified-status")
    parsed["retry_after_seconds"] = _to_int(lowered.get("retry-after"))
    return parsed


async def record_snapshot(
    *,
    user_id: Optional[str],
    headers: Mapping[str, str],
    provider: str = "anthropic",
    key_source: str = "byok",
    model: Optional[str] = None,
    status_code: Optional[int] = None,
) -> bool:
    """헤더를 최신 스냅샷으로 UPSERT. 실패해도 예외를 올리지 않는다."""
    if not user_id:
        return False
    try:
        parsed = parse_rate_limit_headers(headers)
        if not parsed["raw"] and parsed["retry_after_seconds"] is None:
            return False

        await _ensure_table()
        from app.core.db_pool import get_pool

        await get_pool().execute(
            """
            INSERT INTO llm_rate_limit_snapshots (
                user_id, provider, key_source, model, status_code,
                requests_limit, requests_remaining, requests_reset,
                tokens_limit, tokens_remaining, tokens_reset,
                input_tokens_limit, input_tokens_remaining, input_tokens_reset,
                output_tokens_limit, output_tokens_remaining, output_tokens_reset,
                unified_status, retry_after_seconds, raw, observed_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                $9, $10, $11,
                $12, $13, $14,
                $15, $16, $17,
                $18, $19, $20::jsonb, NOW()
            )
            ON CONFLICT (user_id, provider, key_source) DO UPDATE SET
                model = EXCLUDED.model,
                status_code = EXCLUDED.status_code,
                requests_limit = EXCLUDED.requests_limit,
                requests_remaining = EXCLUDED.requests_remaining,
                requests_reset = EXCLUDED.requests_reset,
                tokens_limit = EXCLUDED.tokens_limit,
                tokens_remaining = EXCLUDED.tokens_remaining,
                tokens_reset = EXCLUDED.tokens_reset,
                input_tokens_limit = EXCLUDED.input_tokens_limit,
                input_tokens_remaining = EXCLUDED.input_tokens_remaining,
                input_tokens_reset = EXCLUDED.input_tokens_reset,
                output_tokens_limit = EXCLUDED.output_tokens_limit,
                output_tokens_remaining = EXCLUDED.output_tokens_remaining,
                output_tokens_reset = EXCLUDED.output_tokens_reset,
                unified_status = EXCLUDED.unified_status,
                retry_after_seconds = EXCLUDED.retry_after_seconds,
                raw = EXCLUDED.raw,
                observed_at = NOW()
            """,
            str(user_id),
            provider,
            key_source,
            model,
            status_code,
            parsed["requests_limit"],
            parsed["requests_remaining"],
            parsed["requests_reset"],
            parsed["tokens_limit"],
            parsed["tokens_remaining"],
            parsed["tokens_reset"],
            parsed["input_tokens_limit"],
            parsed["input_tokens_remaining"],
            parsed["input_tokens_reset"],
            parsed["output_tokens_limit"],
            parsed["output_tokens_remaining"],
            parsed["output_tokens_reset"],
            parsed["unified_status"],
            parsed["retry_after_seconds"],
            json.dumps(parsed["raw"], ensure_ascii=False),
        )
        return True
    except Exception as exc:  # 사용량 표시가 채팅을 끊으면 안 된다
        logger.debug(
            "llm_rate_limit_snapshot_failed user=%s provider=%s err=%s",
            str(user_id)[:12],
            provider,
            str(exc)[:120],
        )
        return False


def _headroom(limit: Optional[int], remaining: Optional[int]) -> Optional[float]:
    if not limit or remaining is None or limit <= 0:
        return None
    return max(0.0, min(1.0, remaining / limit))


def summarize(row: Mapping[str, Any]) -> Dict[str, Any]:
    """스냅샷 한 행 → 화면이 바로 쓰는 요약.

    화면에 차원 4개를 다 늘어놓으면 읽히지 않는다. **가장 먼저 바닥나는 차원**
    하나를 골라 그것만 크게 보여주고 나머지는 상세로 접는다.
    """
    dims = []
    for key, label in (
        ("requests", "요청 수"),
        ("tokens", "토큰"),
        ("input_tokens", "입력 토큰"),
        ("output_tokens", "출력 토큰"),
    ):
        limit = row.get(f"{key}_limit")
        remaining = row.get(f"{key}_remaining")
        ratio = _headroom(limit, remaining)
        if ratio is None:
            continue
        reset = row.get(f"{key}_reset")
        dims.append(
            {
                "key": key,
                "label": label,
                "limit": int(limit),
                "remaining": int(remaining),
                "ratio": round(ratio, 4),
                "reset_at": reset.isoformat() if hasattr(reset, "isoformat") else reset,
            }
        )

    tightest = min(dims, key=lambda d: d["ratio"]) if dims else None
    ratio = tightest["ratio"] if tightest else None
    if row.get("status_code") == 429 or (ratio is not None and ratio <= 0.0):
        level = "exhausted"
    elif ratio is not None and ratio < 0.1:
        level = "critical"
    elif ratio is not None and ratio < 0.3:
        level = "warning"
    elif ratio is None:
        level = "unknown"
    else:
        level = "ok"

    observed = row.get("observed_at")
    return {
        "provider": row.get("provider"),
        "key_source": row.get("key_source"),
        "model": row.get("model"),
        "status_code": row.get("status_code"),
        "unified_status": row.get("unified_status"),
        "retry_after_seconds": row.get("retry_after_seconds"),
        "level": level,
        "tightest": tightest,
        "dimensions": dims,
        "observed_at": observed.isoformat() if hasattr(observed, "isoformat") else observed,
    }


async def get_snapshots(user_id: str, provider: Optional[str] = None) -> list[Dict[str, Any]]:
    """사용자의 최신 스냅샷 목록. 테이블이 아직 없으면 빈 목록."""
    try:
        await _ensure_table()
        from app.core.db_pool import get_pool

        pool = get_pool()
        if provider:
            rows = await pool.fetch(
                "SELECT * FROM llm_rate_limit_snapshots WHERE user_id = $1 AND provider = $2 "
                "ORDER BY observed_at DESC",
                str(user_id),
                provider,
            )
        else:
            rows = await pool.fetch(
                "SELECT * FROM llm_rate_limit_snapshots WHERE user_id = $1 ORDER BY observed_at DESC",
                str(user_id),
            )
        return [summarize(dict(r)) for r in rows]
    except Exception as exc:
        logger.warning(
            "llm_rate_limit_snapshot_read_failed user=%s err=%s", str(user_id)[:12], str(exc)[:120]
        )
        return []
