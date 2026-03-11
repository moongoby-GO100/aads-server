"""
AADS-186D: Prompt Caching 최적화 — cache_control 헬퍼
Anthropic Prompt Caching: cache_control: {"type": "ephemeral"}
- 최소 1,024 토큰 이상 블록에서 효과 (서버 캐시 5분 TTL)
- 시스템 프롬프트 + CKP 요약 + 도구 정의에 적용
- 캐시 적중 시 비용 ~90% 절감 (cache_read_input_tokens)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 캐시 적용 최소 토큰 수 (Anthropic 권장 1,024)
MIN_CACHE_TOKENS = 1_024

# cache_control 블록 타입 (현재 "ephemeral"만 지원 — 5분 TTL)
_CACHE_CONTROL = {"type": "ephemeral"}


def make_cacheable_block(text: str, force: bool = False) -> Dict[str, Any]:
    """텍스트를 Anthropic cache_control 시스템 블록으로 래핑.

    Args:
        text: 블록 텍스트 내용
        force: True면 토큰 수 무관 cache_control 강제 적용
    Returns:
        {"type": "text", "text": ..., "cache_control": ...} 또는 cache_control 없는 블록
    """
    from app.core.token_utils import estimate_tokens as _est_tokens
    estimated_tokens = _est_tokens(text)
    block: Dict[str, Any] = {"type": "text", "text": text}
    if force or estimated_tokens >= MIN_CACHE_TOKENS:
        block["cache_control"] = _CACHE_CONTROL
    return block


def build_cached_system_blocks(
    layer1_text: str,
    layer2_text: str,
    ckp_text: str = "",
) -> List[Dict[str, Any]]:
    """3계층 시스템 프롬프트를 Anthropic cache_control 블록 리스트로 구성.

    Layer 1 (정적, ~1400t): cache_control 항상 적용 — 매 요청 캐시 히트 기대
    Layer 2 (동적, ~300t): cache_control 없음 — 매 요청 변경
    CKP    (동적, ~1500t): cache_control 적용 — 세션 내 반복 적중

    Args:
        layer1_text: 정적 시스템 프롬프트 (build_layer1 결과)
        layer2_text: 동적 런타임 정보 (현재 시각, 작업 상태 등)
        ckp_text:    CKP 요약 (선택, AADS/CEO 워크스페이스만)
    Returns:
        Anthropic messages.create(system=...) 파라미터용 블록 리스트
    """
    blocks: List[Dict[str, Any]] = []

    # Layer 1: 정적 — 캐싱 강제 적용 (최소 1024t 기준 충족 여부 무관)
    blocks.append(make_cacheable_block(layer1_text, force=True))

    # CKP: 동적이지만 길이가 길므로 캐싱 시도
    if ckp_text:
        combined_layer2 = layer2_text + "\n" + ckp_text
        blocks.append(make_cacheable_block(combined_layer2))
    else:
        # Layer 2만 있을 때는 토큰 수 기반 판단
        blocks.append(make_cacheable_block(layer2_text))

    return blocks


def build_cached_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """도구 정의 리스트의 마지막 항목에 cache_control 적용.

    Anthropic Prompt Caching은 tools 배열의 마지막 항목에 cache_control을
    붙이면 해당 지점까지 전체 도구 정의를 캐시함.
    최소 도구 토큰 합계가 1024+ 이어야 효과적.

    Args:
        tools: Anthropic Tool Use 포맷 도구 목록
    Returns:
        마지막 도구에 cache_control이 추가된 목록
    """
    if not tools:
        return tools

    from app.core.token_utils import estimate_tokens as _est_tokens
    total_tokens = sum(
        _est_tokens(str(t.get("description", "")) + str(t.get("input_schema", "")))
        for t in tools
    )
    if total_tokens < MIN_CACHE_TOKENS:
        logger.debug(
            f"[CacheConfig] 도구 토큰 {total_tokens} < {MIN_CACHE_TOKENS}, "
            "cache_control 미적용"
        )
        return tools

    # 마지막 도구에 cache_control 추가 (복사본 생성)
    cached_tools = [dict(t) for t in tools]
    cached_tools[-1]["cache_control"] = _CACHE_CONTROL
    logger.debug(f"[CacheConfig] 도구 {len(tools)}개 캐싱 적용 (~{total_tokens}t)")
    return cached_tools


def estimate_cache_savings(
    cached_tokens: int,
    non_cached_tokens: int,
    cache_hit_rate: float = 0.8,
    num_requests: int = 10,
) -> Dict[str, float]:
    """캐시 적중률 기반 비용 절감 추정 (다수 요청 상각 방식).

    Anthropic Prompt Caching 요금:
    - cache_write: 1.25× 기본 요금 (최초 1회 또는 TTL 만료 시)
    - cache_read: 0.1× 기본 요금 (히트 시, 90% 절감)

    상각 모델 (num_requests 요청 기준):
    - 캐시 미적용 베이스라인: N × (cached + non_cached)
    - 캐시 적용: write 1회 + (N-1) × (hit_rate × 0.1 + miss_rate × 1.25) + N × non_cached

    Args:
        cached_tokens: 캐시 대상 토큰 수
        non_cached_tokens: 비캐시 토큰 수
        cache_hit_rate: 캐시 적중률 (기본 80%)
        num_requests: 비용 상각 기준 요청 수 (기본 10)
    Returns:
        {"baseline_cost_ratio": ..., "cached_cost_ratio": ..., "savings_pct": ...}
    """
    n = max(2, num_requests)
    baseline_total = n * (cached_tokens + non_cached_tokens)

    # 캐시 첫 번째 write + 이후 히트/미스
    follow_up = n - 1
    cached_total = (
        cached_tokens * 1.25                                        # 최초 write
        + follow_up * cache_hit_rate * cached_tokens * 0.1          # 히트 → read
        + follow_up * (1 - cache_hit_rate) * cached_tokens * 1.25   # 미스 → write
        + n * non_cached_tokens                                      # 비캐시 부분 (항상)
    )
    savings_pct = max(0.0, (baseline_total - cached_total) / baseline_total * 100)
    return {
        "baseline_cost_ratio": 1.0,
        "cached_cost_ratio": round(cached_total / baseline_total, 3),
        "savings_pct": round(savings_pct, 1),
        "cache_hit_rate": cache_hit_rate,
        "num_requests": n,
    }
