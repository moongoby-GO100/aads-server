"""
AADS-185: 인텐트 분류 + 모델 라우팅
Gemini 2.5 Flash-Lite로 인텐트 분류 (LiteLLM 경유, ~200ms 목표)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")

# ─── 인텐트 → 모델/도구 매핑 ──────────────────────────────────────────────────

@dataclass
class IntentResult:
    intent: str
    model: str
    use_tools: bool
    tool_group: str  # 'system' | 'action' | 'search' | 'all' | ''
    use_extended_thinking: bool = False
    use_gemini_direct: bool = False
    gemini_mode: str = ""  # 'grounding' | 'deep_research' | ''


INTENT_MAP: dict[str, dict] = {
    "casual":           {"model": "gemini-flash-lite",           "tools": False, "group": ""},
    "greeting":         {"model": "gemini-flash-lite",           "tools": False, "group": ""},
    "system_status":    {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "health_check":     {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "dashboard":        {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "diagnosis":        {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "task_history":     {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "search":           {"model": "gemini-flash",                "tools": True,  "group": "search",  "gemini_direct": "grounding"},
    "url_analyze":      {"model": "claude-sonnet",               "tools": True,  "group": "action"},
    "deep_research":    {"model": "gemini-flash",                "tools": False, "group": "",        "gemini_direct": "deep_research"},
    "code_task":        {"model": "claude-sonnet",               "tools": True,  "group": "action"},
    "directive":        {"model": "claude-opus",                 "tools": True,  "group": "action",  "thinking": True},
    "directive_gen":    {"model": "claude-opus",                 "tools": True,  "group": "action",  "thinking": True},
    "complex_analysis": {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "strategy":         {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "planning":         {"model": "claude-sonnet",               "tools": False, "group": ""},
    "decision":         {"model": "claude-sonnet",               "tools": False, "group": ""},
    "design":           {"model": "claude-sonnet",               "tools": False, "group": ""},
    "design_fix":       {"model": "claude-sonnet",               "tools": False, "group": ""},
    "architect":        {"model": "claude-opus",                 "tools": True,  "group": "action",  "thinking": True},
    "code_exec":        {"model": "claude-sonnet",               "tools": True,  "group": "action"},
    "memory_recall":    {"model": "claude-sonnet",               "tools": True,  "group": "action"},
    "qa":               {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "execution_verify": {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "workspace_switch": {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "cost_report":      {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    "browser":          {"model": "gemini-flash",                "tools": False, "group": ""},
    "image_analyze":    {"model": "claude-sonnet",               "tools": False, "group": ""},
    "video_analyze":    {"model": "gemini-flash",                "tools": False, "group": ""},
}

_DEFAULT_INTENT = IntentResult(
    intent="casual",
    model="claude-sonnet",
    use_tools=False,
    tool_group="",
)

# ─── 분류 프롬프트 ──────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """당신은 인텐트 분류기입니다. 사용자 메시지를 분석하여 정확히 하나의 인텐트를 반환하세요.

가능한 인텐트 목록:
casual, greeting, system_status, health_check, dashboard, diagnosis, task_history,
search, url_analyze, deep_research, code_task, directive, directive_gen, complex_analysis,
strategy, planning, decision, design, design_fix, architect, code_exec, memory_recall,
qa, execution_verify, workspace_switch, cost_report, browser, image_analyze, video_analyze

규칙:
- "안녕", "안녕하세요", 인사 → greeting
- 날씨/시간/간단한 질문 → casual
- 서버 상태, 헬스체크 → health_check
- 대시보드, 작업현황, 파이프라인 → dashboard
- 진단, 종합 상태 → diagnosis
- 최근 작업, 완료 목록 → task_history
- 검색해줘, 찾아봐, 최신 뉴스 → search
- 심층 분석, 리서치 보고서, 시장 조사 → deep_research
- URL 분석, 링크 내용 확인 → url_analyze
- 지시서 작성, DIRECTIVE_START → directive_gen
- 코드 작성, 버그 수정 → code_task
- 설계, 아키텍처 → architect
- 전략, 방향성 → strategy
- 기획 → planning
- 의사결정 → decision
- 디자인 → design
- 이미지 분석 → image_analyze
- 영상 분석 → video_analyze
- 코드 실행 → code_exec
- 메모리, 과거 기록 → memory_recall
- QA 검증 → qa
- 실행 확인 → execution_verify
- 워크스페이스 변경 → workspace_switch
- 비용 조회 → cost_report
- 복잡한 분석, 종합 → complex_analysis

JSON으로만 응답하세요: {"intent": "...", "confidence": 0.0~1.0}"""


async def classify(message: str, workspace: str = "CEO") -> IntentResult:
    """
    Gemini Flash-Lite로 인텐트 분류.
    실패 시 키워드 기반 폴백.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{LITELLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
                json={
                    "model": "gemini-flash-lite",
                    "messages": [
                        {"role": "system", "content": _CLASSIFY_PROMPT},
                        {"role": "user", "content": f"워크스페이스: {workspace}\n메시지: {message}"},
                    ],
                    "max_tokens": 80,
                    "temperature": 0.1,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                raw = data["choices"][0]["message"]["content"].strip()
                # JSON 파싱
                if raw.startswith("{"):
                    parsed = json.loads(raw)
                    intent = parsed.get("intent", "casual")
                    return _make_result(intent)
    except Exception as e:
        logger.debug(f"intent_router classify error: {e}")

    # 키워드 폴백
    return _keyword_fallback(message)


def _make_result(intent: str) -> IntentResult:
    """인텐트 → IntentResult 변환."""
    cfg = INTENT_MAP.get(intent, INTENT_MAP.get("casual", {}))
    model = cfg.get("model", "claude-sonnet")
    gemini_direct = cfg.get("gemini_direct", "")
    return IntentResult(
        intent=intent,
        model=model,
        use_tools=cfg.get("tools", False),
        tool_group=cfg.get("group", ""),
        use_extended_thinking=cfg.get("thinking", False),
        use_gemini_direct=bool(gemini_direct),
        gemini_mode=gemini_direct,
    )


def _keyword_fallback(message: str) -> IntentResult:
    """Gemini 실패 시 키워드 기반 분류."""
    msg = message.lower()

    if any(w in msg for w in ("안녕", "hello", "hi ", "반가")):
        return _make_result("greeting")
    if any(w in msg for w in ("헬스체크", "서버 상태", "health")):
        return _make_result("health_check")
    if any(w in msg for w in ("대시보드", "작업현황", "pipeline", "파이프라인")):
        return _make_result("dashboard")
    if any(w in msg for w in ("심층", "deep research", "리서치 보고서", "시장 조사")):
        return _make_result("deep_research")
    if any(w in msg for w in ("검색", "찾아봐", "최신", "뉴스")):
        return _make_result("search")
    if any(w in msg for w in ("지시서", "directive_start", ">>>directive")):
        return _make_result("directive_gen")
    if any(w in msg for w in ("아키텍처", "설계", "architect")):
        return _make_result("architect")
    if any(w in msg for w in ("전략", "strategy")):
        return _make_result("strategy")
    if any(w in msg for w in ("코드", "버그", "수정", "개발")):
        return _make_result("code_task")

    return _make_result("casual")


def get_model_for_override(model_override: str) -> str:
    """
    프론트엔드 model_override 문자열을 LiteLLM 모델명으로 변환.
    예: "claude-sonnet-4-6" → "claude-sonnet" (litellm alias)
    """
    mapping = {
        "claude-sonnet-4-6": "claude-sonnet",
        "claude-opus-4-6":   "claude-opus",
        "claude-haiku-4-5":  "claude-haiku",
        "claude-sonnet":     "claude-sonnet",
        "claude-opus":       "claude-opus",
        "claude-haiku":      "claude-haiku",
        "gemini-flash":      "gemini-flash",
        "gemini-flash-lite": "gemini-flash-lite",
        "gemini-pro":        "gemini-pro",
    }
    return mapping.get(model_override, model_override)
