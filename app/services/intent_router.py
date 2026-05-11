"""
AADS-186A: 인텐트 분류 + 모델 라우팅
Gemini 2.5 Flash-Lite로 인텐트 분류 (LiteLLM 경유, ~200ms 목표)
신규: service_inspection(inspect_service), all_service_status(get_all_service_status)
"""
from __future__ import annotations

import hashlib
import json
import json as _json
import re as _re
import logging
import os
from dataclasses import dataclass, asdict
from typing import Optional

import httpx

_intent_redis = None


async def _get_intent_redis():
    """인텐트 캐시용 Redis 연결 (실패해도 무시)"""
    global _intent_redis
    if _intent_redis is None:
        try:
            import redis.asyncio as aioredis
            _intent_redis = await aioredis.from_url(
                "redis://aads-redis:6379/1",
                decode_responses=True,
                socket_connect_timeout=1,
            )
        except Exception:
            return None
    return _intent_redis

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
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
    "casual":           {"model": "qwen-turbo",           "tools": False, "group": ""},
    "greeting":         {"model": "qwen-turbo",           "tools": False, "group": ""},
    "deep_research":    {"model": "gemini-pro",                  "tools": False, "group": "",        "gemini_direct": "deep_research"},
    "strategy":         {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "discussion":       {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "planning":         {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "decision":         {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "design":           {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "design_fix":       {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "image_analyze":    {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    "video_analyze":    {"model": "gemini-3-flash-preview",       "tools": False, "group": ""},
    "cto_strategy":     {"model": "claude-opus",                 "tools": False, "group": "",        "thinking": True},
    # ─── 도구 사용 인텐트 — 전부 group="all", 기본 Opus ───────────────────────
    "system_status":    {"model": "claude-sonnet",                "tools": True,  "group": "all",     "thinking": True},
    "health_check":     {"model": "claude-sonnet",                "tools": True,  "group": "all",     "thinking": True},
    "dashboard":        {"model": "claude-sonnet",                "tools": True,  "group": "all",     "thinking": True},
    "diagnosis":        {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "task_history":     {"model": "claude-sonnet",                "tools": True,  "group": "all",     "thinking": True},
    "search":           {"model": "gemini-3-flash-preview",       "tools": True,  "group": "all",     "gemini_direct": "grounding"},
    "url_analyze":      {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "code_task":        {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "directive":        {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "directive_gen":    {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "complex_analysis": {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "architect":        {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "code_exec":        {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "memory_recall":    {"model": "claude-sonnet",                "tools": True,  "group": "all",     "thinking": True},
    "qa":               {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "execution_verify": {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "workspace_switch": {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cost_report":      {"model": "claude-sonnet",                "tools": True,  "group": "all",     "thinking": True},
    "browser":          {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "server_file":      {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    # ─── CTO 모드 인텐트 ─────────────────────────────────────────────────────
    "cto_code_analysis":{"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cto_directive":    {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cto_verify":       {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cto_impact":       {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    "cto_tech_debt":    {"model": "claude-opus",                 "tools": True,  "group": "all",     "thinking": True},
    # AADS-188C: Agent SDK 자율 실행 인텐트
    "execute":            {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    "code_modify":        {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    # Pipeline Runner: Claude Code 자율 작업 파이프라인
    "pipeline_runner":    {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    # 자동 반응 (파이프라인 완료 후)
    "auto_reaction":      {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    # 첨부파일 읽기
    "file_read":          {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    # AADS-188C Phase 2: 메타 도구 인텐트
    "task_query":         {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    "status_check":       {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    # AADS-186A 신규 인텐트
    "service_inspection": {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    "all_service_status": {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    # AADS-195 Phase 3: PC 제어 인텐트
    "pc_control":         {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    "pc_screenshot":      {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    "pc_file":            {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    "pc_kakao":           {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    # AADS-186E-1 크롤링 인텐트
    "url_read":           {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    "deep_crawl":         {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    # 아젠다 관리 인텐트
    "agenda_manage":      {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    "agenda_decide":      {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    "agenda_auto_detect": {"model": "claude-sonnet",              "tools": True,  "group": "all",     "thinking": True},
    # AADS-186E-3 딥리서치 + 코드탐색 인텐트
    "code_explorer":      {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    "analyze_changes":    {"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
    "search_all_projects":{"model": "claude-opus",               "tools": True,  "group": "all",     "thinking": True},
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
    model="qwen-turbo",
    use_tools=False,
    tool_group="",
)

# ─── 인텐트 Temperature 매핑 (v2.1 Q17: W1-C2 선행 완료 후 활성화) ─────────────
# governance_enabled가 false이면 이 맵을 직접 사용 (DB 조회 없음)
INTENT_TEMPERATURE_MAP: dict[str, float] = {
    "casual":             0.2,
    "greeting":           0.1,
    "deep_research":      0.3,
    "strategy":          0.15,
    "discussion":        0.15,
    "planning":          0.2,
    "decision":          0.2,
    "design":            0.25,
    "design_fix":        0.2,
    "image_analyze":     0.2,
    "video_analyze":     0.2,
    "cto_strategy":      0.1,
    "system_status":     0.1,
    "health_check":      0.1,
    "dashboard":         0.15,
    "diagnosis":         0.15,
    "task_history":      0.1,
    "search":            0.2,
    "url_analyze":       0.2,
    "code_task":         0.15,
    "directive":         0.15,
    "directive_gen":     0.15,
    "complex_analysis":  0.15,
    "architect":         0.2,
    "code_exec":         0.1,
    "memory_recall":     0.2,
    "qa":                0.1,
    "execution_verify":  0.1,
    "workspace_switch":  0.1,
    "cost_report":       0.1,
    "browser":           0.2,
    "server_file":       0.2,
    "cto_code_analysis": 0.1,
    "cto_directive":     0.1,
    "cto_verify":        0.1,
    "cto_impact":        0.15,
    "cto_tech_debt":     0.15,
    "execute":           0.1,
    "code_modify":       0.1,
    "pipeline_runner":   0.1,
    "auto_reaction":     0.2,
    "file_read":         0.1,
    "task_query":        0.1,
    "status_check":      0.1,
    "service_inspection":0.15,
    "all_service_status":0.1,
    "pc_control":        0.15,
    "pc_screenshot":     0.1,
    "pc_file":           0.15,
    "pc_kakao":          0.1,
    "url_read":          0.2,
    "deep_crawl":        0.25,
    "agenda_manage":     0.15,
    "agenda_decide":     0.1,
    "agenda_auto_detect":0.2,
    "code_explorer":     0.2,
    "analyze_changes":   0.15,
    "search_all_projects":0.2,
    "news_search":       0.2,
    "blog_search":       0.2,
    "shop_search":       0.2,
    "local_search":      0.2,
    "book_search":       0.2,
    "image_search":      0.2,
    "encyclopedia_search":0.2,
    "knowledge_search":  0.2,
}

_DEFAULT_TEMPERATURE = 0.2


async def resolve_intent_temperature(intent: str) -> float:
    """
    v2.1 Q17: W1-C2 선행 완료 후 활성화될 인텐트별 temperature 해결.
    governance_enabled=false → DB 조회 skip, INTENT_TEMPERATURE_MAP 폴백 직행.
    governance_enabled=true → DB에서 커스텀 temperature 조회.
    """
    from app.core.feature_flags import governance_enabled

    if not await governance_enabled():
        # governance off: DB 조회 없이 하드코딩 맵으로 폴백
        return INTENT_TEMPERATURE_MAP.get(intent, _DEFAULT_TEMPERATURE)

    # governance on: DB에서 커스텀 temperature 조회 (future W1-C2 slot)
    try:
        pool = None
        try:
            from app.db import get_pool
            pool = get_pool()
        except ImportError:
            from app.core.db_pool import get_pool
            pool = get_pool()

        async with pool.acquire() as conn:
            temp = await conn.fetchval(
                "SELECT temperature FROM intent_temperatures WHERE intent = $1",
                intent,
            )
        if temp is not None:
            return float(temp)
    except Exception:
        pass  # DB 조회 실패 시 하드코딩 맵으로 폴백

    return INTENT_TEMPERATURE_MAP.get(intent, _DEFAULT_TEMPERATURE)


# ─── 분류 프롬프트 ──────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """당신은 인텐트 분류기입니다. 사용자 메시지를 분석하여 정확히 하나의 인텐트를 반환하세요.

가능한 인텐트 목록:
casual, greeting, system_status, health_check, dashboard, diagnosis, task_history,
search, url_analyze, deep_research, code_task, directive, directive_gen, complex_analysis,
strategy, planning, decision, design, design_fix, architect, code_exec, memory_recall,
discussion,
qa, execution_verify, workspace_switch, cost_report, browser, image_analyze, video_analyze, server_file,
cto_strategy, cto_code_analysis, cto_directive, cto_verify, cto_impact, cto_tech_debt,
service_inspection, all_service_status,
url_read, deep_crawl,
code_explorer, analyze_changes, search_all_projects,
execute, code_modify, task_query, status_check, pipeline_runner, file_read,
news_search, blog_search, shop_search, local_search, book_search, image_search, encyclopedia_search, knowledge_search,
pc_control, pc_screenshot, pc_file, pc_kakao,
agenda_manage, agenda_decide, agenda_auto_detect

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
- "토론해봐", "다관점으로 분석해", "다관점 토론해", "찬반 토론해", "run_debate" → discussion
- "장단점 비교", "어떻게 해야 할까", "의견 줘"만 있으면 → cto_strategy 또는 strategy (discussion 아님)
- "다관점 토론은 명시 지시 때만", "토론 기능 조치", "인텐트 문제 수정"처럼 토론 기능 자체를 조치/수정/확인하는 요청 → code_modify 또는 status_check (discussion 아님)
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
- 디자인, 디자인 스튜디오, 디자인 수정 카드 생성, UI 수정 요청 생성 → design
- "스크린샷 찍어", "화면 캡처", "렌더링 확인", "화면이 이상해", "화면 봐줘" → browser
- "여기 확인해", "여기 채팅창 기능 분석", "여기 기능 분석", "페이지 기능 분석" → cto_code_analysis (소스 코드 우선 분석)
- "PC 스크린샷 찍어", "PC 화면 캡처", "PC 화면" → pc_screenshot
- "PC에서 메모장 열어", "PC 프로그램 실행", "PC 명령", "PC 제어", "PC 원격" → pc_control
- "PC 파일 보여줘", "PC 파일 목록", "PC 파일 읽어" → pc_file
- "카카오톡으로 보내", "카톡 보내", "카톡으로 전달", "카카오톡 메시지" → pc_kakao
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
- "아젠다 등록해", "아젠다 추가", "아젠다 보여줘", "아젠다 목록", "아젠다 수정", "아젠다 보류" → agenda_manage
- "아젠다 결정", "아젠다 진행", "아젠다 승인", "이 아젠다 처리" → agenda_decide
- "나중에", "나중에 결정", "나중에 논의", "다음에 논의", "다음에 다시", "보류해", "일단 킵", "일단 보류", "검토 필요", "나중에 하자", "다음에 하자", "잠깐 미뤄", "미뤄두자" → agenda_auto_detect

중요 규칙 — CEO 명령형 메시지:
- "확인하고 보고하라", "확인해봐", "보고해", "점검하라", "진단하라", "체크해" → status_check (casual이 절대 아님)
- "~하라", "~해라", "~해봐", "~해줘" 형태의 짧은 명령 + 확인/점검/보고/진단/조회/분석 키워드 → status_check 또는 execute
- 대화 맥락상 이전에 서버 확인, 작업 보고 등의 대화가 있었고 짧은 후속 지시가 오면 → 이전 맥락의 인텐트 유지 (casual이 아님)
- "넌 ~할 수 있다", "너는 ~가 가능하다" + 서버/도구/접근 → status_check (능력 확인 후 실행 기대)
- "파이프라인 시작", "클로드봇한테 시켜", "봇한테 시켜", "봇에게 시켜", "자율작업", "파이프라인C", "pipeline c" → pipeline_runner

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
    Redis 캐싱: 메시지 앞 100자 SHA256 해시 키(앞 16자), TTL 60초.
    """
    # ─── Redis 인텐트 캐시 조회 ──────────────────────────────────────────────
    _cache_key = _build_intent_cache_key(message, workspace, recent_messages)
    try:
        _r = await _get_intent_redis()
        if _r:
            _cached = await _r.get(_cache_key)
            if _cached:
                logger.info(f"intent_cache_hit: {_cache_key}")
                return IntentResult(**_json.loads(_cached))
    except Exception:
        pass

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

    _pc_override = _pc_agent_followup_override(message)
    if _pc_override:
        return _make_result(_pc_override)

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
                    "model": "qwen-turbo",
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
                # JSON 추출 — Gemini가 텍스트를 앞뒤로 붙이는 경우 대응
                _json_match = _re.search(r'\{[^{}]*\}', raw)
                if _json_match:
                    parsed = json.loads(_json_match.group())
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
                    if intent == "discussion" and not is_explicit_debate_request(message):
                        override = _discussion_guard_fallback(message)
                        logger.info(
                            "intent_discussion_guard: discussion → %s for '%s'",
                            override,
                            message[:80],
                        )
                        return _make_result(override)
                    result = _make_result(intent)
                    # Redis 캐시 저장 (TTL 60초 — 컨텍스트 의존 오분류 방지를 위해 짧게)
                    try:
                        _r = await _get_intent_redis()
                        if _r:
                            await _r.setex(_cache_key, 60, _json.dumps(asdict(result)))
                    except Exception:
                        pass
                    return result
    except Exception as e:
        logger.debug(f"intent_router classify error: {e}")

    # 키워드 폴백
    result = _keyword_fallback(message)
    # 키워드도 casual인데 이전 대화에서 도구 사용 중이었으면 → status_check
    if result.intent == "casual" and _prev_used_tools and len(message) <= 30:
        logger.info(f"intent_fallback_context_override: casual → status_check for '{message[:40]}'")
        return _make_result("status_check")
    return result


def _is_context_dependent_message(message: str) -> bool:
    """이전 턴 의미에 의존하는 짧은 후속 지시 여부."""
    msg = (message or "").lower().strip()
    if len(msg) <= 50:
        return True
    return any(
        marker in msg
        for marker in (
            "이거", "그거", "저거", "위 ", "위에", "앞서", "방금", "이전",
            "전 지시", "전 응답", "이어", "계속", "했는데", "그 작업", "그거",
            "pc에이전트", "pc 에이전트", "내 pc", "pc연결", "pc 연결",
        )
    )


def _build_intent_cache_key(
    message: str,
    workspace: str = "CEO",
    recent_messages: list | None = None,
) -> str:
    """인텐트 캐시 키.

    같은 문장이라도 워크스페이스와 직전 대화가 다르면 의미가 달라진다.
    특히 "이거 확인해", "이어서 진행해", "했는데" 같은 후속 지시는
    메시지 단독 캐싱 시 이전 세션의 인텐트를 재사용하는 문제가 생긴다.
    """
    parts = [str(workspace or "CEO").upper(), (message or "")[:100]]
    if recent_messages and _is_context_dependent_message(message):
        ctx_parts: list[str] = []
        for item in recent_messages[-4:]:
            role = str(item.get("role", ""))[:16]
            content = str(item.get("content", "") or "").replace("\n", " ")[:160]
            if role and content:
                ctx_parts.append(f"{role}:{content}")
        if ctx_parts:
            parts.append("|".join(ctx_parts))
    raw_key = "\n".join(parts)
    return f"intent:{hashlib.sha256(raw_key.encode()).hexdigest()[:16]}"


def _pc_agent_followup_override(message: str) -> str | None:
    """PC 에이전트 관련 후속 정정/불만을 워크스페이스 전환으로 보내지 않는다."""
    msg = (message or "").lower().replace(" ", "")
    if not msg:
        return None

    mentions_pc_agent = "pc에이전트" in msg or "pcagent" in msg
    mentions_pc_connection = "내pc" in msg and "연결" in msg
    if not (mentions_pc_agent or mentions_pc_connection or "pc연결" in msg):
        return None

    verify_markers = (
        "진행하라고", "했는데", "안되어", "안돼", "안되", "구현", "연결",
        "상태", "확인", "점검", "보고", "왜", "문제",
    )
    control_markers = (
        "열어", "실행", "클릭", "입력", "캡처", "스크린샷", "명령", "제어",
        "파일", "카톡", "카카오",
    )
    if any(marker in msg for marker in verify_markers):
        return "cto_verify"
    if any(marker in msg for marker in control_markers):
        return None
    return "cto_verify"


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
    _action_keywords = ("수정", "배포", "실행", "재시작", "적용", "반영", "시작", "조치", "구현")
    _cmd_suffixes = ("하라", "해라", "해봐", "해줘", "하고", "해서", "하라고")

    has_cmd = any(kw in msg for kw in _cmd_keywords)
    has_action = any(kw in msg for kw in _action_keywords)
    has_suffix = any(sf in msg for sf in _cmd_suffixes)

    if any(w in msg for w in ("인텐트", "인턴트", "intent", "라우팅", "오분류", "분류")) and has_action:
        return "code_modify"
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


_EXPLICIT_DEBATE_PATTERNS = (
    r"\brun_debate\b",
    r"\bdebate\b",
    r"토론\s*(해|해줘|해봐|하자|시작|진행)",
    r"다관점(으로)?\s*(토론|분석|검토)\s*(해|해줘|해봐|하자|시작)?",
    r"다각도(로)?\s*(토론|분석|검토)\s*(해|해줘|해봐|하자|시작)?",
    r"관점별(로)?\s*(토론|분석|검토)\s*(해|해줘|해봐|하자|시작)?",
    r"찬반\s*토론\s*(해|해줘|해봐|하자|시작)?",
)

_DEBATE_META_ACTION_WORDS = (
    "조치", "수정", "구현", "반영", "적용", "배포", "고쳐", "막아",
    "분류", "라우팅", "인텐트", "intent", "오분류", "문제",
)


def is_explicit_debate_request(message: str) -> bool:
    """CEO가 토론 실행을 명시한 경우에만 True.

    토론 기능을 고치라는 문장에 "다관점 토론"이 포함되어도 토론 실행으로 보지 않는다.
    """
    msg = (message or "").lower().strip()
    if not msg:
        return False

    if any(word in msg for word in _DEBATE_META_ACTION_WORDS):
        strong_execute = ("토론해" in msg or "토론 해" in msg or "run_debate" in msg or "debate " in msg)
        if not strong_execute:
            return False

    return any(_re.search(pattern, msg) for pattern in _EXPLICIT_DEBATE_PATTERNS)


def _discussion_guard_fallback(message: str) -> str:
    """discussion 오분류를 일반 운영/전략 인텐트로 되돌린다."""
    msg = (message or "").lower()
    override = _command_override(message)
    if override:
        return override
    if any(w in msg for w in ("조치", "수정", "구현", "반영", "적용", "고쳐", "막아")):
        return "code_modify"
    if any(w in msg for w in ("확인", "보고", "점검", "검증", "운영", "상태", "되나", "맞나", "정확")):
        return "cto_verify"
    return "cto_strategy"


def _keyword_fallback(message: str) -> IntentResult:
    """Gemini 실패 시 키워드 기반 분류."""
    msg = message.lower()

    # AADS-195 Phase 3: PC 제어 인텐트 (키워드 우선)
    if any(w in msg for w in ("카카오톡으로", "카톡 보내", "카톡으로", "카카오톡 메시지", "카톡 메시지")):
        return _make_result("pc_kakao")
    if any(w in msg for w in ("pc 스크린샷", "pc 화면 캡처", "pc 화면 찍", "pc화면")):
        return _make_result("pc_screenshot")
    if any(w in msg for w in ("pc 파일", "pc에서 파일", "pc 폴더")):
        return _make_result("pc_file")
    if any(w in msg for w in ("pc 제어", "pc 원격", "pc에서 실행", "pc에서 열어", "pc 프로그램", "pc 명령", "pc에서 메모장", "pc에서 크롬")):
        return _make_result("pc_control")
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
        return _make_result("pipeline_runner")
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
    if is_explicit_debate_request(message):
        return _make_result("discussion")
    if (
        any(w in msg for w in ("다관점", "다각도", "관점별", "토론", "run_debate", "debate"))
        and any(w in msg for w in ("조치", "수정", "고쳐", "반영", "적용", "구현", "막아", "명시", "정확하게 지시"))
    ):
        return _make_result("code_modify")
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
    # 기술/라이브러리 관련 키워드가 함께 있으면 code_task 우선 (SearXNG 허용)
    _tech_keywords = ("라이브러리", "프레임워크", "패키지", "버전", "api ", "sdk", "공식문서", "최신 버전",
                      "설치", "import", "pip ", "npm ", "yarn ", "모듈", "의존성", "changelog")
    if any(w in msg for w in ("최신", "검색", "찾아봐")):
        if any(tw in msg for tw in _tech_keywords) or any(w in msg for w in ("코드", "버그", "개발", "구현", "함수")):
            return _make_result("code_task")
        return _make_result("search")
    if any(w in msg for w in ("지시서", "directive_start", ">>>directive")):
        return _make_result("directive_gen")
    if any(w in msg for w in ("아키텍처", "설계", "architect")):
        return _make_result("architect")
    if any(w in msg for w in ("장단점 비교", "찬반 비교", "어떻게 해야 할까", "어떻게 하는 게 좋", "비교해봐")):
        return _make_result("cto_strategy")
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
    if any(w in msg for w in ("인텐트", "인턴트", "intent", "라우팅", "오분류", "분류")) and any(w in msg for w in ("조치", "수정", "고쳐", "반영", "적용", "구현", "막아")):
        return _make_result("code_modify")
    if any(w in msg for w in ("되나", "맞나", "정확한 맥락", "정확한 정보", "진화", "발전하는 세션", "확인하고", "보고완료")):
        return _make_result("cto_verify")

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
    model_override = (model_override or "").strip()
    lowered = model_override.lower()
    if lowered.startswith("codex:"):
        return model_override.split(":", 1)[1].strip()
    codex_display_aliases = {
        "gpt-5.5 (codex cli)": "gpt-5.5",
        "gpt-5.4 (codex cli)": "gpt-5.4",
        "gpt-5.4 mini (codex cli)": "gpt-5.4-mini",
        "gpt-5.3 codex (codex cli)": "gpt-5.3-codex",
    }
    if lowered in codex_display_aliases:
        return codex_display_aliases[lowered]
    mapping = {
        "claude-sonnet-4-6": "claude-sonnet",
        "claude-opus-4-6":   "claude-opus",
        "claude-haiku-4-5":  "claude-haiku",
        "claude-haiku-4-5-20251001": "claude-haiku",
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
        "deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-v4-pro":   "deepseek-v4-pro",
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
        "claude-3-5-sonnet-20241022": "claude-sonnet",
        "claude-3-5-haiku-20241022":  "claude-haiku",
        "claude-3-opus-20240229":     "claude-opus",
        "claude-3-sonnet-20240229":   "claude-sonnet",
        "claude-3-haiku-20240307":    "claude-haiku",
        "claude-2.1":                 "claude-sonnet",
        "claude-opus-4-5":            "claude-opus",
        "claude-sonnet-4-5":          "claude-sonnet",
    }
    return mapping.get(model_override, model_override)
