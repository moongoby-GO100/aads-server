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
    naver_type: str = ""   # 'news' | 'blog' | 'shop' | 'local' | 'book' | 'image' | 'encyc' | 'kin' | ''


INTENT_MAP: dict[str, dict] = {
    # ─── 도구 불필요 인텐트 ───────────────────────────────────────────────────
    "casual":           {"model": "gemini-flash-lite",           "tools": False, "group": ""},
    "greeting":         {"model": "gemini-flash-lite",           "tools": False, "group": ""},
    "deep_research":    {"model": "gemini-pro",                  "tools": False, "group": "",        "gemini_direct": "deep_research"},
    "strategy":         {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "planning":         {"model": "claude-sonnet",               "tools": False, "group": ""},
    "decision":         {"model": "claude-sonnet",               "tools": False, "group": ""},
    "design":           {"model": "claude-sonnet",               "tools": False, "group": ""},
    "design_fix":       {"model": "claude-sonnet",               "tools": False, "group": ""},
    "image_analyze":    {"model": "claude-sonnet",               "tools": False, "group": ""},
    "video_analyze":    {"model": "gemini-3-flash-preview",       "tools": False, "group": ""},
    "cto_strategy":     {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    # ─── 도구 사용 인텐트 — 전부 group="all" ─────────────────────────────────
    "system_status":    {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "health_check":     {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "dashboard":        {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "diagnosis":        {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "task_history":     {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "search":           {"model": "gemini-3-flash-preview",       "tools": True,  "group": "all",     "gemini_direct": "grounding"},
    "url_analyze":      {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "code_task":        {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "directive":        {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "directive_gen":    {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "complex_analysis": {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "architect":        {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "code_exec":        {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "memory_recall":    {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "qa":               {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "execution_verify": {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "workspace_switch": {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "cost_report":      {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "browser":          {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "server_file":      {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    # ─── CTO 모드 인텐트 ─────────────────────────────────────────────────────
    "cto_code_analysis":{"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cto_directive":    {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    "cto_verify":       {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cto_impact":       {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cto_tech_debt":    {"model": "claude-sonnet",               "tools": True,  "group": "all"},
    # AADS-188C: Agent SDK 자율 실행 인텐트
    "execute":            {"model": "claude-opus",               "tools": True,  "group": "all"},
    "code_modify":        {"model": "claude-opus",               "tools": True,  "group": "all"},
    # Pipeline C: Claude Code 자율 작업 파이프라인
    "pipeline_c":         {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    # 자동 반응 (파이프라인 완료 후)
    "auto_reaction":      {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    # 첨부파일 읽기
    "file_read":          {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    # AADS-188C Phase 2: 메타 도구 인텐트
    "task_query":         {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    "status_check":       {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    # AADS-186A 신규 인텐트
    "service_inspection": {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    "all_service_status": {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    # AADS-186E-1 크롤링 인텐트
    "url_read":           {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    "deep_crawl":         {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    # AADS-186E-3 딥리서치 + 코드탐색 인텐트
    "code_explorer":      {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    "analyze_changes":    {"model": "claude-sonnet",             "tools": True,  "group": "all"},
    "search_all_projects":{"model": "claude-sonnet",             "tools": True,  "group": "all"},
    # Naver 특화 검색 인텐트
    "news_search":        {"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "news"},
    "blog_search":        {"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "blog"},
    "shop_search":        {"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "shop"},
    "local_search":       {"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "local"},
    "book_search":        {"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "book"},
    "image_search":       {"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "image"},
    "encyclopedia_search":{"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "encyc"},
    "knowledge_search":   {"model": "gemini-3-flash-preview",     "tools": True,  "group": "all",     "gemini_direct": "grounding", "naver_type": "kin"},
}

_DEFAULT_INTENT = IntentResult(
    intent="casual",
    model="gemini-flash-lite",
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
code_explorer, analyze_changes, search_all_projects,
execute, code_modify, task_query, status_check, pipeline_c, file_read,
news_search, blog_search, shop_search, local_search, book_search, image_search, encyclopedia_search, knowledge_search

규칙:
- "다른 친구에게 시킨거 진행 확인", "걔 작업 됐나", "작업 현황", "시킨거 확인", "진행 상태 확인해줘" → task_query
- "전체 상태 보고", "서비스 상태 확인해줘", "시스템 체크", "상태 체크", "현재 상태 알려줘" → status_check
- "이 파일 수정해", "코드 고쳐", "버그 수정해서 배포해", "이거 반영해", "직접 수정해" → code_modify
- "실행해", "배포해", "서버 재시작", "빌드해", "테스트 돌려" → execute
- "도구 테스트", "전체 테스트", "전부 테스트", "모든 도구", "tool test" → complex_analysis
- "안녕", "안녕하세요", 인사 → greeting
- 날씨/시간/간단한 질문 → casual
- 서버 상태, 헬스체크 → health_check
- 대시보드, 작업현황, 파이프라인 → dashboard
- 진단, 종합 상태 → diagnosis
- 최근 작업, 완료 목록 → task_history
- 파일 읽어, 첨부파일, 업로드한 파일, 이전 파일, 파일 다시, 보고서 파일, 파일 내용 보여줘, 파일 검토 → file_read
- 서버 검색, 원격 서버 파일, SSH 파일 목록, 프로젝트 서버에서 찾아줘 → server_file
- 서비스 점검, {프로젝트} 점검해, 프로세스 확인, 서비스 상태 자세히 → service_inspection
- 전체 서비스 상태, 6개 서비스, 올 스테이터스, 모든 서비스 → all_service_status
- 검색해줘, 찾아봐, 웹 검색 → search
- 뉴스, 오늘 뉴스, 뉴스 검색, 기사 → news_search
- 블로그, 블로그 검색, 후기, 리뷰 → blog_search
- 쇼핑, 가격 비교, 최저가, 상품 검색 → shop_search
- 맛집, 근처, 지역 검색, 장소, 위치 → local_search
- 책, 도서, 책 검색, 서적, 저자 → book_search
- 이미지 검색, 사진 찾기, 이미지 찾아 → image_search
- 백과사전, 사전, 뜻, 정의, 의미 → encyclopedia_search
- 지식인, 지식iN, 질문, Q&A → knowledge_search
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
- "스크린샷 찍어", "화면 캡처", "렌더링 확인", "화면이 이상해", "화면 봐줘" → browser
- "여기 확인해", "여기 채팅창 기능 분석", "여기 기능 분석", "페이지 기능 분석" → cto_code_analysis (소스 코드 우선 분석)
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

중요 규칙 — CEO 명령형 메시지:
- "확인하고 보고하라", "확인해봐", "보고해", "점검하라", "진단하라", "체크해" → status_check (casual이 절대 아님)
- "~하라", "~해라", "~해봐", "~해줘" 형태의 짧은 명령 + 확인/점검/보고/진단/조회/분석 키워드 → status_check 또는 execute
- 대화 맥락상 이전에 서버 확인, 작업 보고 등의 대화가 있었고 짧은 후속 지시가 오면 → 이전 맥락의 인텐트 유지 (casual이 아님)
- "넌 ~할 수 있다", "너는 ~가 가능하다" + 서버/도구/접근 → status_check (능력 확인 후 실행 기대)
- "파이프라인 시작", "클로드봇한테 시켜", "봇한테 시켜", "봇에게 시켜", "자율작업", "파이프라인C", "pipeline c" → pipeline_c

JSON으로만 응답하세요: {"intent": "...", "confidence": 0.0~1.0}"""


async def classify(
    message: str,
    workspace: str = "CEO",
    recent_messages: list | None = None,
) -> IntentResult:
    """
    Gemini Flash-Lite로 인텐트 분류.
    recent_messages: 최근 대화 히스토리 (컨텍스트 인식 분류용).
    실패 시 키워드 기반 폴백.
    """
    # ─── 컨텍스트 인식: 이전 대화에서 도구 사용 중이면 짧은 후속 지시는 casual 아님 ───
    _prev_used_tools = False
    _prev_intent = ""
    if recent_messages and len(recent_messages) >= 2:
        # 마지막 assistant 메시지에서 도구 사용 흔적 감지
        for m in reversed(recent_messages[:-1]):  # 현재 user 메시지 제외
            if m.get("role") == "assistant":
                c = m.get("content", "")
                if any(marker in c for marker in ("도구 조회 결과", "tool_use", "🔧", "실행 중")):
                    _prev_used_tools = True
                break

    try:
        # LLM에 최근 컨텍스트 제공 (짧은 메시지의 맥락 파악용)
        _context_hint = ""
        if recent_messages and len(message) <= 30:
            # 짧은 메시지: 직전 2개 메시지를 컨텍스트로 제공
            _recent = recent_messages[-4:] if len(recent_messages) >= 4 else recent_messages
            _ctx_parts = []
            for m in _recent:
                role = m.get("role", "")
                content = (m.get("content", "") or "")[:100]
                if role in ("user", "assistant") and content:
                    _ctx_parts.append(f"[{role}] {content}")
            if _ctx_parts:
                _context_hint = "\n최근 대화 컨텍스트:\n" + "\n".join(_ctx_parts[-3:])

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{LITELLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
                json={
                    "model": "gemini-flash-lite",
                    "messages": [
                        {"role": "system", "content": _CLASSIFY_PROMPT},
                        {"role": "user", "content": f"워크스페이스: {workspace}\n메시지: {message}{_context_hint}"},
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
                    # CEO 명령형 오분류 보정: casual/greeting인데 실제 명령형 패턴이면 override
                    if intent in ("casual", "greeting"):
                        override = _command_override(message)
                        if override:
                            logger.info(f"intent_override: {intent} → {override} for '{message[:40]}'")
                            return _make_result(override)
                        # 컨텍스트 보정: 이전에 도구를 쓰고 있었고 짧은 후속 지시면 → status_check
                        if _prev_used_tools and len(message) <= 30:
                            logger.info(f"intent_context_override: {intent} → status_check (prev used tools) for '{message[:40]}'")
                            return _make_result("status_check")
                    return _make_result(intent)
    except Exception as e:
        logger.debug(f"intent_router classify error: {e}")

    # 키워드 폴백
    result = _keyword_fallback(message)
    # 키워드도 casual인데 이전 대화에서 도구 사용 중이었으면 → status_check
    if result.intent == "casual" and _prev_used_tools and len(message) <= 30:
        logger.info(f"intent_fallback_context_override: casual → status_check for '{message[:40]}'")
        return _make_result("status_check")
    return result


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
        naver_type=cfg.get("naver_type", ""),
    )


def _command_override(message: str) -> str | None:
    """CEO 명령형 메시지가 casual/greeting으로 오분류된 경우 보정."""
    msg = message.lower().strip()
    # 명령형 키워드 + 어미 조합
    _cmd_keywords = ("확인", "보고", "점검", "진단", "체크", "조회", "분석", "파악", "살펴", "알아봐")
    _action_keywords = ("수정", "배포", "실행", "재시작", "적용", "반영", "시작")
    _cmd_suffixes = ("하라", "해라", "해봐", "해줘", "하고", "해서", "하라고")

    has_cmd = any(kw in msg for kw in _cmd_keywords)
    has_action = any(kw in msg for kw in _action_keywords)
    has_suffix = any(sf in msg for sf in _cmd_suffixes)

    # "확인하고 보고하라" / "점검해봐" / "진단해줘"
    if has_cmd and (has_suffix or len(message) <= 30):
        return "status_check"
    # "수정해라" / "배포하라" / "적용해줘"
    if has_action and (has_suffix or len(message) <= 30):
        return "execute"
    # "넌 ~가능하다" 패턴
    if ("넌 " in msg or "너는 " in msg) and any(w in msg for w in ("가능", "접근", "할 수", "할수", "서버")):
        return "status_check"
    return None


def _keyword_fallback(message: str) -> IntentResult:
    """Gemini 실패 시 키워드 기반 분류."""
    msg = message.lower()

    if any(w in msg for w in ("안녕", "hello", "hi ", "반가")):
        return _make_result("greeting")
    if any(w in msg for w in ("도구 테스트", "전체 테스트", "전부 테스트", "모든 도구", "tool test", "도구 전부", "도구 모두")):
        return _make_result("complex_analysis")
    # AADS-188C Phase 2: task_query — 2개 이상 키워드 매칭으로 정확도 향상
    _tq_keywords = ["시킨거", "진행", "확인", "됐나", "했나", "작업 현황", "다른 친구", "다른 애", "걔", "그 봇", "진행 상태"]
    _tq_hits = sum(1 for w in _tq_keywords if w in msg)
    if _tq_hits >= 2:
        return _make_result("task_query")
    # AADS-188C Phase 2: status_check
    if any(w in msg for w in ("전체 상태 보고", "시스템 체크", "상태 체크", "현재 상태 알려", "전체 현황")):
        return _make_result("status_check")
    if any(w in msg for w in ("헬스체크", "서버 상태", "health")):
        return _make_result("health_check")
    if any(w in msg for w in ("파이프라인 시작", "파이프라인c", "pipeline c", "클로드봇", "봇한테 시켜", "봇에게 시켜", "자율작업", "자율 작업")):
        return _make_result("pipeline_c")
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
    # Naver 특화 검색 키워드
    if any(w in msg for w in ("뉴스", "기사", "속보", "뉴스 검색")):
        return _make_result("news_search")
    if any(w in msg for w in ("블로그", "후기", "리뷰 검색", "블로그 검색")):
        return _make_result("blog_search")
    if any(w in msg for w in ("쇼핑", "최저가", "가격 비교", "상품 검색", "쇼핑 검색")):
        return _make_result("shop_search")
    if any(w in msg for w in ("맛집", "근처", "지역 검색", "장소 검색", "주변")):
        return _make_result("local_search")
    if any(w in msg for w in ("책 검색", "도서 검색", "서적", "isbn")):
        return _make_result("book_search")
    if any(w in msg for w in ("이미지 검색", "사진 찾", "이미지 찾")):
        return _make_result("image_search")
    if any(w in msg for w in ("백과사전", "사전", "의미", "뜻이")):
        return _make_result("encyclopedia_search")
    if any(w in msg for w in ("지식인", "지식in", "q&a")):
        return _make_result("knowledge_search")
    if any(w in msg for w in ("검색", "찾아봐", "최신")):
        return _make_result("search")
    if any(w in msg for w in ("지시서", "directive_start", ">>>directive")):
        return _make_result("directive_gen")
    if any(w in msg for w in ("아키텍처", "설계", "architect")):
        return _make_result("architect")
    if any(w in msg for w in ("전략", "strategy")):
        return _make_result("strategy")
    if any(w in msg for w in ("직접 수정", "코드 고쳐", "파일 수정", "반영해", "코드 수정해", "수정해서 배포", "수정하고 배포")):
        return _make_result("code_modify")
    if any(w in msg for w in ("실행해", "배포해", "서버 재시작", "빌드해", "테스트 돌려", "deploy")):
        return _make_result("execute")
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
    # 브라우저 도구 — 렌더링 확인이 명확한 경우만
    if any(w in msg for w in ("스크린샷", "화면 캡처", "화면 봐줘", "렌더링 확인", "ui 깨", "화면이 이상")):
        return _make_result("browser")
    # "여기 확인해", "채팅창 기능" → 코드 분석 우선 (cto_code_analysis)
    if any(w in msg for w in ("여기 확인", "여기 채팅", "여기 기능", "채팅창 기능", "페이지 기능")):
        return _make_result("cto_code_analysis")
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

    # ─── CEO 명령형 패턴 (casual 오분류 방지) ─────────────────────────────
    # "확인하라", "보고하라", "점검해", "진단해" 등 짧은 명령형
    _cmd_keywords = ("확인", "보고", "점검", "진단", "체크", "조회", "분석", "파악", "살펴", "알아봐", "찾아봐")
    _cmd_suffixes = ("하라", "해라", "해봐", "해줘", "하고", "해서")
    if any(kw in msg for kw in _cmd_keywords):
        # 명령형 어미가 있거나 메시지가 짧으면(CEO 지시 스타일) → status_check
        if any(msg.endswith(sf) or sf in msg for sf in _cmd_suffixes) or len(message) <= 30:
            return _make_result("status_check")
    # "넌 ~할 수 있다" 패턴 → 능력 확인 후 실행 기대
    if ("넌 " in msg or "너는 " in msg) and any(w in msg for w in ("가능", "접근", "할 수", "할수", "서버")):
        return _make_result("status_check")

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
        "deepseek-chat":     "deepseek-chat",
        "deepseek-reasoner": "deepseek-reasoner",
        "groq-llama-70b":    "groq-llama-70b",
        "groq-llama-8b":     "groq-llama-8b",
        "groq-llama4-maverick": "groq-llama4-maverick",
        "groq-llama4-scout": "groq-llama4-scout",
        "groq-qwen3-32b":   "groq-qwen3-32b",
        "groq-kimi-k2":     "groq-kimi-k2",
        "groq-gpt-oss-120b":"groq-gpt-oss-120b",
        "groq-compound":    "groq-compound",
    }
    return mapping.get(model_override, model_override)
