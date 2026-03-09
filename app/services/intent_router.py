"""
AADS-186A: 인텐트 분류 + 모델 라우팅
Gemini 2.5 Flash-Lite로 인텐트 분류 (LiteLLM 경유, ~200ms 목표)
신규: service_inspection(inspect_service), all_service_status(get_all_service_status)
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
    "server_file":      {"model": "claude-sonnet",               "tools": True,  "group": "action"},
    # ─── CTO 모드 인텐트 (AADS-186B / AADS-186E-2) ────────────────────────────
    # cto_strategy/cto_code_analysis/cto_verify/cto_impact: Opus + Extended Thinking
    "cto_strategy":     {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "cto_code_analysis":{"model": "claude-opus",                 "tools": True,  "group": "action",  "thinking": True},
    "cto_directive":    {"model": "claude-sonnet",               "tools": True,  "group": "action"},
    "cto_verify":       {"model": "claude-opus",                 "tools": True,  "group": "system",  "thinking": True},
    "cto_impact":       {"model": "claude-opus",                 "tools": True,  "group": "action",  "thinking": True},
    "cto_tech_debt":    {"model": "claude-sonnet",               "tools": True,  "group": "system"},
    # AADS-186A 신규 인텐트
    "service_inspection": {"model": "claude-sonnet",             "tools": True,  "group": "workflow"},
    "all_service_status": {"model": "claude-sonnet",             "tools": True,  "group": "workflow"},
    # AADS-186E-1 크롤링 인텐트
    "url_read":           {"model": "claude-sonnet",             "tools": True,  "group": "crawl"},
    "deep_crawl":         {"model": "claude-sonnet",             "tools": True,  "group": "crawl"},
    # AADS-186E-3 딥리서치 + 코드탐색 인텐트
    "code_explorer":      {"model": "claude-sonnet",             "tools": True,  "group": "research"},
    "analyze_changes":    {"model": "claude-sonnet",             "tools": True,  "group": "research"},
    "search_all_projects":{"model": "claude-sonnet",             "tools": True,  "group": "research"},
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
qa, execution_verify, workspace_switch, cost_report, browser, image_analyze, video_analyze, server_file,
cto_strategy, cto_code_analysis, cto_directive, cto_verify, cto_impact, cto_tech_debt,
service_inspection, all_service_status,
url_read, deep_crawl,
code_explorer, analyze_changes, search_all_projects

규칙:
- "안녕", "안녕하세요", 인사 → greeting
- 날씨/시간/간단한 질문 → casual
- 서버 상태, 헬스체크 → health_check
- 대시보드, 작업현황, 파이프라인 → dashboard
- 진단, 종합 상태 → diagnosis
- 최근 작업, 완료 목록 → task_history
- 서버 검색, 원격 서버 파일, SSH 파일 목록, 프로젝트 서버에서 찾아줘 → server_file
- 서비스 점검, {프로젝트} 점검해, 프로세스 확인, 서비스 상태 자세히 → service_inspection
- 전체 서비스 상태, 6개 서비스, 올 스테이터스, 모든 서비스 → all_service_status
- 검색해줘, 찾아봐, 최신 뉴스 → search
- 딥리서치, "깊이 조사", "조사해서 보고서 써줘", "시장 분석 보고서", "경쟁 분석 보고서", 기술 동향 보고, 논문 조사 → deep_research
- "검색해"만 있으면 → search (빠르고 저렴)
- URL 분석, 링크 내용 확인 → url_analyze
- 이 URL 읽어, 이 문서 분석, 이 페이지 내용, http로 시작하는 URL → url_read
- 조사해서 정리, 여러 소스 비교, 크롤링해서 분석, 딥 크롤 → deep_crawl
- 함수 호출 체인, 로직 흐름 추적, 코드 탐색, 함수 추적 다이어그램 → code_explorer
- git 변경 분석, 최근 커밋, 변경사항 위험도, 이번주 변경 → analyze_changes
- 전체 프로젝트 검색, 6개 서비스에서 찾아줘, 모든 프로젝트 코드 검색 → search_all_projects
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
- 전략 토론, 방향, 아키텍처 토론, 어떻게 생각해, 의견 → cto_strategy
- 코드 분석, 코드 흐름, 함수 추적, 소스 분석 → cto_code_analysis
- 지시서 생성, 태스크 생성, 작업 지시, 이거 시켜 → cto_directive
- 검증, 확인해, 작업 결과 점검, 커밋 확인 → cto_verify
- 영향 분석, 이거 바꾸면, 사전 분석 → cto_impact
- 기술 부채, TODO 정리, 정리 필요한 것 → cto_tech_debt

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
    if any(w in msg for w in ("서버 검색", "원격 서버", "ssh 파일", "서버 파일", "프로젝트 서버에서", "kis 서버", "sf 서버", "ntv2 서버", "go100 서버")):
        return _make_result("server_file")
    if any(w in msg for w in ("서비스 점검", "점검해", "프로세스 확인", "서비스 상태 자세히", "docker 상태", "로그 확인")):
        return _make_result("service_inspection")
    if any(w in msg for w in ("전체 서비스 상태", "6개 서비스", "올 스테이터스", "모든 서비스 상태")):
        return _make_result("all_service_status")
    if any(w in msg for w in ("심층", "deep research", "리서치 보고서", "시장 조사", "리서치", "경쟁사 분석", "트렌드 분석")):
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
    # CTO 모드 키워드 폴백
    if any(w in msg for w in ("전략 토론", "방향 의견", "어떻게 생각", "기술 방향")):
        return _make_result("cto_strategy")
    if any(w in msg for w in ("코드 분석", "코드 흐름", "함수 추적", "소스 분석")):
        return _make_result("cto_code_analysis")
    if any(w in msg for w in ("지시서 생성", "태스크 생성", "작업 지시", "이거 시켜")):
        return _make_result("cto_directive")
    if any(w in msg for w in ("작업 결과 검증", "커밋 확인", "결과 점검")):
        return _make_result("cto_verify")
    if any(w in msg for w in ("영향 분석", "이거 바꾸면", "사전 분석")):
        return _make_result("cto_impact")
    if any(w in msg for w in ("기술 부채", "todo 정리", "fixme", "정리 필요")):
        return _make_result("cto_tech_debt")
    if any(w in msg for w in ("이 url 읽어", "이 문서 분석", "이 페이지 내용", "http://", "https://", "url 열어", "링크 내용")):
        return _make_result("url_read")
    if any(w in msg for w in ("조사해서 정리", "여러 소스 비교", "크롤링해서 분석", "딥 크롤", "deep crawl")):
        return _make_result("deep_crawl")
    if any(w in msg for w in ("딥리서치", "깊이 조사", "종합 보고서 써줘", "시장 분석 보고서", "경쟁 분석 보고서", "기술 동향 보고", "논문 조사", "조사해줘", "조사해서", "경쟁사", "트렌드", "보고서 작성")):
        return _make_result("deep_research")
    if any(w in msg for w in ("함수 호출 체인", "로직 흐름 추적", "코드 탐색", "함수 추적 다이어그램", "trace_function")):
        return _make_result("code_explorer")
    if any(w in msg for w in ("git 변경 분석", "최근 커밋 분석", "변경사항 위험도", "이번주 변경", "이번달 변경")):
        return _make_result("analyze_changes")
    if any(w in msg for w in ("전체 프로젝트 검색", "6개 서비스에서", "모든 프로젝트 코드", "전체 코드 검색")):
        return _make_result("search_all_projects")

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
        "gemini-2.5-flash":  "gemini-2.5-flash",
        "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
        "gemini-2.5-pro":    "gemini-2.5-pro",
        "gemini-2.5-flash-image": "gemini-2.5-flash-image",
        "gemini-3-pro-preview":   "gemini-3-pro-preview",
        "gemini-3-flash-preview":  "gemini-3-flash-preview",
        "gemini-3.1-pro-preview":  "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite-preview",
        "gemma-3-27b-it":    "gemma-3-27b-it",
    }
    return mapping.get(model_override, model_override)
