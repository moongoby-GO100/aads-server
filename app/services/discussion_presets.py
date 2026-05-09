from __future__ import annotations

from copy import deepcopy
from typing import Any

_DEFAULT_PRESET_NAME = "standard"

MODEL_ALIASES = {
    "옵스": "claude-sonnet-4-6",
    "오푸스": "claude-sonnet-4-6",
    "opus": "claude-sonnet-4-6",
    "claude-opus": "claude-sonnet-4-6",
    "claude-opus-4-6": "claude-sonnet-4-6",
    "소넷": "claude-sonnet-4-6",
    "sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "하이쿠": "claude-haiku-4-5-20251001",
    "haiku": "claude-haiku-4-5-20251001",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
    "제미나이": "gemini-2.5-pro",
    "gemini": "gemini-2.5-pro",
    "gemini-pro": "gemini-2.5-pro",
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-flash": "gemini-2.5-flash",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "deepseek": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
}

# Design doc 기준의 라운드당 대략 비용을 모델별 고정값으로 정리한다.
MODEL_ROUND_COST_USD = {
    "claude-sonnet-4-6": 0.7,
    "claude-haiku-4-5-20251001": 0.2,
    "gemini-2.5-pro": 0.8,
    "gemini-2.5-flash": 0.1,
    "deepseek-v4-pro": 0.3,
    "deepseek-v4-flash": 0.1,
}

DISCUSSION_PRESETS: dict[str, dict[str, Any]] = {
    "standard": {
        "name": "standard",
        "label": "기본 3인",
        "synthesizer_model": "claude-sonnet-4-6",
        "participants": [
            {
                "name": "기획 A",
                "role": "strategic_planner",
                "model": "claude-sonnet-4-6",
                "color": "#6c5ce7",
                "avatar": "A",
            },
            {
                "name": "기획 B",
                "role": "market_analyst",
                "model": "gemini-2.5-pro",
                "color": "#00cec9",
                "avatar": "B",
            },
            {
                "name": "검증 C",
                "role": "critical_reviewer",
                "model": "gemini-2.5-flash",
                "color": "#fdcb6e",
                "avatar": "C",
            },
        ],
    },
    "deep": {
        "name": "deep",
        "label": "심층 4인",
        "synthesizer_model": "claude-sonnet-4-6",
        "participants": [
            {
                "name": "기획 A",
                "role": "strategic_planner",
                "model": "claude-sonnet-4-6",
                "color": "#6c5ce7",
                "avatar": "A",
            },
            {
                "name": "시장 B",
                "role": "market_analyst",
                "model": "gemini-2.5-pro",
                "color": "#00cec9",
                "avatar": "B",
            },
            {
                "name": "검증 C",
                "role": "critical_reviewer",
                "model": "claude-haiku-4-5-20251001",
                "color": "#fdcb6e",
                "avatar": "C",
            },
            {
                "name": "속도 D",
                "role": "rapid_ideator",
                "model": "gemini-2.5-flash",
                "color": "#55efc4",
                "avatar": "D",
            },
        ],
    },
    "light": {
        "name": "light",
        "label": "경량 2인",
        "synthesizer_model": "claude-haiku-4-5-20251001",
        "participants": [
            {
                "name": "기획 A",
                "role": "planner",
                "model": "claude-sonnet-4-6",
                "color": "#6c5ce7",
                "avatar": "A",
            },
            {
                "name": "속도 B",
                "role": "rapid_ideator",
                "model": "gemini-2.5-flash",
                "color": "#00cec9",
                "avatar": "B",
            },
        ],
    },
}


def get_preset(name: str | None) -> dict[str, Any]:
    """토론 프리셋을 반환하고, 모르는 이름은 standard로 폴백한다."""
    key = (name or _DEFAULT_PRESET_NAME).strip().lower()
    preset = DISCUSSION_PRESETS.get(key, DISCUSSION_PRESETS[_DEFAULT_PRESET_NAME])
    return deepcopy(preset)


def resolve_model_name(name: str | None) -> str:
    """CEO가 입력한 별칭을 실제 모델명으로 보수적으로 정규화한다."""
    raw = (name or "").strip()
    normalized = raw.lower()

    if not normalized:
        return "claude-sonnet-4-6"

    if normalized in MODEL_ALIASES:
        return MODEL_ALIASES[normalized]

    if "옵스" in raw or "오푸스" in raw or "opus" in normalized:
        return "claude-sonnet-4-6"
    if "소넷" in raw or "sonnet" in normalized:
        return "claude-sonnet-4-6"
    if "하이쿠" in raw or "haiku" in normalized:
        return "claude-haiku-4-5-20251001"
    if "제미나이" in raw or "gemini" in normalized:
        if "flash" in normalized or "플래시" in raw:
            return "gemini-2.5-flash"
        return "gemini-2.5-pro"

    return raw


def estimate_round_cost(
    preset_or_participants: dict[str, Any] | list[dict[str, Any]],
) -> float:
    """프리셋 또는 참가자 목록 기준으로 라운드당 추정 비용을 계산한다."""
    if isinstance(preset_or_participants, dict):
        participants = preset_or_participants.get("participants", [])
    else:
        participants = preset_or_participants

    total_cost = 0.0
    for participant in participants:
        model_name = resolve_model_name(participant.get("model"))
        total_cost += MODEL_ROUND_COST_USD.get(model_name, 0.0)

    return round(total_cost, 4)


__all__ = [
    "DISCUSSION_PRESETS",
    "MODEL_ALIASES",
    "MODEL_ROUND_COST_USD",
    "estimate_round_cost",
    "get_preset",
    "resolve_model_name",
]
