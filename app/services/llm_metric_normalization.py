"""LLM 지표 조회 시점 정규화 — 모델 차원·폴백 기록·비용 산출 근거.

원본 행(`oauth_usage_log`, `chat_turn_executions`, `pipeline_jobs`)은 건드리지
않는다. 같은 모델이 여러 이름으로 적혀 있어도(표시명 `GPT-6 Sol (Codex CLI)`,
공급자 접두어 `codex:gpt-6-sol`, 슬러그 `gpt-6-sol`) 읽을 때 하나로 모은다.

2026-09-29 실측: `chat_turn_executions.actual_model` distinct 15종 중 상당수가
같은 모델의 다른 표기였고, `interrupted`/`stopped`/`unknown`/`unverified` 처럼
**모델이 아닌 상태값**이 모델 차원에 섞여 있었다(러너 경로 최대 항목이
`unverified` 145건). 상태값은 모델 차원에서 빼서 상태 차원으로 보낸다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

try:  # 클로드 별칭의 정본은 모델 계약이다. 여기서 따로 목록을 만들지 않는다.
    from scripts.claude_model_contract import ALIASES as _CLAUDE_ALIASES
except Exception:  # pragma: no cover - 계약 모듈이 없으면 별칭 해석만 건너뛴다
    _CLAUDE_ALIASES = {}

# 모델 차원에 들어오면 안 되는 값. 실행 상태이거나 "모른다"는 표지다.
NON_MODEL_STATES = frozenset({
    "interrupted",
    "stopped",
    "unknown",
    "unverified",
    "cancelled",
    "canceled",
    "error",
    "failed",
    "none",
    "null",
    "n/a",
    # 자동 라우팅 표지 — CEO 가 모델을 고르지 않았다는 뜻이지 모델 이름이 아니다
    # (ceo_chat: req.model in {"mixture", "auto"} → 자동 라우팅).
    "mixture",
    "auto",
    # Claude CLI 가 모델 호출 없이 만든 합성 응답 표지(중단·오류 안내 등).
    # 2026-09-30 실측: chat_messages.model_used 24시간 147건.
    "<synthetic>",
})

_PROVIDER_PREFIXES = (
    "codex:",
    "litellm:",
    "claude:",
    "anthropic:",
    "anthropic/",
    "openai/",
)
# 표시명 꼬리표: "GPT-6 Sol (Codex CLI)", "Claude Opus 5 (Claude CLI)"
_DISPLAY_SUFFIX_RE = re.compile(r"\s*\([^)]*\bcli\b[^)]*\)\s*$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_model_dimension(raw: Any) -> dict[str, Any]:
    """원시 모델 문자열을 (모델, 상태) 두 차원으로 나눈다.

    반환: {"model": 정규화 슬러그 | None, "state": 비모델 상태값 | None, "raw": 원문}
    모델이면 state 가 None, 상태값이면 model 이 None 이다.
    """
    text = str(raw or "").strip()
    if not text:
        return {"model": None, "state": "unknown", "raw": text}

    lowered = text.lower()
    if lowered in NON_MODEL_STATES:
        return {"model": None, "state": lowered, "raw": text}

    slug = _DISPLAY_SUFFIX_RE.sub("", text).strip()
    lowered_slug = slug.lower()
    for prefix in _PROVIDER_PREFIXES:
        if lowered_slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    slug = _WHITESPACE_RE.sub("-", slug.strip()).lower()
    if not slug or slug in NON_MODEL_STATES:
        return {"model": None, "state": slug or "unknown", "raw": text}

    slug = _CLAUDE_ALIASES.get(slug, slug)
    return {"model": slug, "state": None, "raw": text}


# ── 폴백 기록 ────────────────────────────────────────────────────────────
#
# `chat_turn_executions.fallback_chain` 은 세 가지 모양으로 남는다.
#   1) `[]`                     — 컬럼 기본값. 폴백 기록이 없다.
#   2) {"from","to","reason","stages":[…]}  — 폴백 성공 (`_save_and_update_session`)
#   3) {"result": <마지막 stage>, "stages":[…]} — 전체 실패 (`_record_fallback_trace`, M5)
# 과거 비어 있지 않은 배열(stage 목록)이 들어온 경우도 읽는다.
# 기록 경로는 채팅 429 모델 폴백뿐이므로 "none_recorded" 는 "폴백 없음" 이
# 아니라 "폴백 기록 없음" 이다.

FALLBACK_CLASSES = ("none_recorded", "fallback_succeeded", "fallback_all_failed", "fallback_attempted", "unparseable")
_UNPARSEABLE = object()


def _parse_fallback_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return _UNPARSEABLE
    return value


def _stage_names(stages: Any) -> list[str]:
    if not isinstance(stages, list):
        return []
    return [str(item.get("stage") or "") for item in stages if isinstance(item, dict)]


def classify_fallback_chain(value: Any) -> str:
    """fallback_chain 한 값을 FALLBACK_CLASSES 중 하나로 판정한다."""
    parsed = _parse_fallback_value(value)
    if parsed is _UNPARSEABLE:
        return "unparseable"
    if parsed is None or parsed == [] or parsed == {}:
        return "none_recorded"

    if isinstance(parsed, dict):
        stages = _stage_names(parsed.get("stages"))
        result = str(parsed.get("result") or "")
        if parsed.get("to") or result == "fallback_succeeded" or "fallback_succeeded" in stages:
            return "fallback_succeeded"
        if result == "all_failed" or "all_failed" in stages:
            return "fallback_all_failed"
        if stages or parsed.get("from") or result:
            return "fallback_attempted"
        return "unparseable"

    if isinstance(parsed, list):
        stages = _stage_names(parsed)
        if "fallback_succeeded" in stages:
            return "fallback_succeeded"
        if "all_failed" in stages:
            return "fallback_all_failed"
        return "fallback_attempted"

    return "unparseable"


def summarize_fallback_chains(rows: Iterable[tuple[Any, int]]) -> dict[str, Any]:
    """(fallback_chain 값, 건수) 목록을 폴백률 집계로 만든다."""
    counts = {name: 0 for name in FALLBACK_CLASSES}
    for value, count in rows:
        counts[classify_fallback_chain(value)] += int(count or 0)
    turns = sum(counts.values())
    fallback_turns = counts["fallback_succeeded"] + counts["fallback_all_failed"] + counts["fallback_attempted"]
    return {
        "turns": turns,
        "fallback_turns": fallback_turns,
        "fallback_rate_pct": round(fallback_turns * 100.0 / turns, 1) if turns else None,
        "fallback_success_rate_pct": (
            round(counts["fallback_succeeded"] * 100.0 / fallback_turns, 1) if fallback_turns else None
        ),
        "by_class": counts,
        "note": (
            "fallback_chain 은 채팅 429 모델 폴백 경로만 기록한다. "
            "none_recorded 는 '폴백 없음'이 아니라 '폴백 기록 없음'이다."
        ),
    }


# ── 비용 산출 근거 ───────────────────────────────────────────────────────
#
# AADS 의 Anthropic 인증은 OAuth 구독(sk-ant-oat01, R-AUTH)이고 Codex 도 구독
# 계정이다. `oauth_usage_log.cost_usd` 는 CLI 의 `total_cost_usd` 나 토큰 단가
# 추정으로 적힌 **정가 환산값**이지 실청구가 아니다. 2026-09-29 24시간 합계
# 약 $11,162 를 "지출" 로 보고하면 실제를 수십 배로 부풀린다.
# 실청구액은 어디에서도 측정하지 않으므로 None(미측정)으로 둔다. 추정하지 않는다.

COST_BASIS_LIST_PRICE = "list_price_equivalent"
COST_BASIS_NOT_RECORDED = "not_recorded"
COST_BASIS_UNKNOWN = "unknown"

# call_source → cost_usd 를 누가 어떻게 채우는가 (코드 확인분)
_COST_SOURCES: dict[str, tuple[str, str]] = {
    "cli_relay": (COST_BASIS_LIST_PRICE, "Claude CLI result.total_cost_usd (구독 호출의 정가 환산)"),
    "model_selector_sdk": (COST_BASIS_LIST_PRICE, "Agent SDK total_cost_usd 또는 _estimate_cost 토큰 단가"),
    "codex_relay": (COST_BASIS_LIST_PRICE, "_estimate_cost 토큰 단가 (Codex 구독의 정가 환산)"),
    # 아래 경로는 log_usage 에 cost_usd 를 넘기지 않아 항상 0 이 적힌다.
    "anthropic_client": (COST_BASIS_NOT_RECORDED, "cost_usd 미전달 — 0 은 비용 0 이 아니라 미기록"),
    "ceo_chat": (COST_BASIS_NOT_RECORDED, "cost_usd 미전달 — 0 은 비용 0 이 아니라 미기록"),
    "ceo_chat_tools": (COST_BASIS_NOT_RECORDED, "cost_usd 미전달 — 0 은 비용 0 이 아니라 미기록"),
}


def cost_basis_for_source(call_source: Any) -> dict[str, Any]:
    """call_source 의 비용 산출 근거. 실청구는 항상 미측정(None)이다."""
    key = str(call_source or "").strip()
    basis, detail = _COST_SOURCES.get(key, (COST_BASIS_UNKNOWN, "산출 경로 미확인 call_source"))
    return {
        "call_source": key,
        "cost_basis": basis,
        "cost_basis_detail": detail,
        "billed_usd": None,
        "billed_status": "미측정",
    }


def build_cost_entry(call_source: Any, cost_usd: Any, *, calls: int = 0) -> dict[str, Any]:
    """비용 합계를 산출 근거와 함께 표기한다. 정가 환산이 아니면 금액을 내지 않는다."""
    basis = cost_basis_for_source(call_source)
    try:
        amount = round(float(cost_usd or 0), 6)
    except (TypeError, ValueError):
        amount = None
    list_price = amount if basis["cost_basis"] == COST_BASIS_LIST_PRICE else None
    return {
        **basis,
        "calls": int(calls or 0),
        "list_price_equivalent_usd": list_price,
        "recorded_cost_usd": amount,
    }
