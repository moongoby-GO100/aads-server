"""
AADS 토큰 추정 유틸리티 — 한국어/다국어 지원.

기존 len(text) // 4 (영어 기준)는 한국어에서 2~3배 과소 추정.
UTF-8 바이트 기반 추정으로 다국어 환경에서 정확도 향상.

- 영어: ~4 chars = ~4 bytes = 1 token → bytes // 4 ≈ chars // 4
- 한국어: ~1 char = ~3 bytes ≈ 2 tokens → bytes // 3 ≈ chars * 2
- 혼합: UTF-8 바이트 // 3 이 가장 균형 잡힌 추정치
"""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """한국어/다국어를 고려한 빠른 토큰 추정.

    UTF-8 바이트 기반: 영어(1byte/char) → //3 보수적, 한국어(3bytes/char) → //3 ≈ 2tok/char.
    """
    if not text:
        return 0
    return len(text.encode("utf-8")) // 3


def estimate_tokens_for_messages(
    messages: list[dict],
    system_prompt: str = "",
) -> int:
    """메시지 리스트 + 시스템 프롬프트의 총 토큰 추정."""
    total_bytes = len(system_prompt.encode("utf-8")) if system_prompt else 0
    for msg in messages:
        role = msg.get("role", "")
        total_bytes += len(role.encode("utf-8"))
        content = msg.get("content", "")
        if isinstance(content, str):
            total_bytes += len(content.encode("utf-8"))
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    if text:
                        total_bytes += len(str(text).encode("utf-8"))
                elif isinstance(item, str):
                    total_bytes += len(item.encode("utf-8"))
        elif content:
            total_bytes += len(str(content).encode("utf-8"))
    return total_bytes // 3


# 역변환 상수: 토큰 → 문자 변환 시 사용 (혼합 텍스트 평균)
CHARS_PER_TOKEN = 2  # 한국어+영어 혼합 평균 (보수적)


# ── 비용 계산 ──────────────────────────────────────────────────────
from decimal import Decimal

# Model pricing per 1M tokens (input_rate, output_rate) in USD
COST_MAP: dict[str, tuple[float, float]] = {
    # Claude — Anthropic 공식 가격 (per 1M tokens)
    "claude-opus": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku": (0.80, 4.0),
    "claude-haiku-4-5": (0.80, 4.0),
    # Gemini
    "gemini-flash": (0.075, 0.3),
    "gemini-flash-lite": (0.01, 0.04),
    "gemini-pro": (1.25, 5.0),
    "gemini-3-flash-preview": (0.1, 0.4),
    "gemini-deep-research": (1.25, 5.0),
    "brave-search": (0.0, 0.0),  # API 건당 $0.005 별도
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """모델 + 토큰 기반 비용 추정."""
    in_rate, out_rate = COST_MAP.get(model, (3.0, 15.0))
    return Decimal(str(round(
        input_tokens * in_rate / 1_000_000 + output_tokens * out_rate / 1_000_000, 6
    )))
