"""Phase 3: 시스템 프롬프트 섹션별 토큰 프로파일링."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """간단한 토큰 추정 (한영 혼합 기준 ~0.7 토큰/char)."""
    return int(len(text) * 0.7)


def profile_sections() -> dict:
    """개별 섹션 토큰 크기 프로파일."""
    from app.core.prompts.system_prompt_v2 import (
        LAYER1_BEHAVIOR, LAYER1_CEO_GUIDE, LAYER1_TOOLS,
        LAYER1_RULES, LAYER1_RESPONSE_GUIDELINES,
        LAYER4_SELF_AWARENESS_TEMPLATE, WS_ROLES, WS_CAPABILITIES,
    )
    sections = {
        "BEHAVIOR": LAYER1_BEHAVIOR,
        "CEO_GUIDE": LAYER1_CEO_GUIDE,
        "TOOLS": LAYER1_TOOLS,
        "RULES": LAYER1_RULES,
        "RESPONSE_GUIDELINES": LAYER1_RESPONSE_GUIDELINES,
        "LAYER4_TEMPLATE": LAYER4_SELF_AWARENESS_TEMPLATE,
    }
    for name in WS_ROLES:
        sections[f"ROLE_{name}"] = WS_ROLES[name]
    for name in WS_CAPABILITIES:
        sections[f"CAP_{name}"] = WS_CAPABILITIES[name]

    return {
        name: {"chars": len(text), "est_tokens": estimate_tokens(text)}
        for name, text in sections.items()
    }


def profile_all_workspaces() -> dict:
    """전체 워크스페이스 x 대표 인텐트별 build_layer1() 토큰 프로파일."""
    from app.core.prompts.system_prompt_v2 import build_layer1, WS_ROLES
    test_intents = ["greeting", "search", "code_task", "strategy", "directive", "status_check"]
    result = {}
    for ws in WS_ROLES:
        result[ws] = {}
        for intent in test_intents:
            prompt = build_layer1(ws, "", intent)
            result[ws][intent] = {
                "chars": len(prompt),
                "est_tokens": estimate_tokens(prompt),
            }
    return result
