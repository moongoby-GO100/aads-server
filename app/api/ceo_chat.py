"""
CEO Chat v2 - 계층 메모리 + 컨텍스트 DB + 모델 분기 엔진
T-073: Context Manager + Model Router + Session Memory
AADS-156: 모델 라우팅 수정 + 전체 지원 모델 업데이트 + 402 fallback
AADS-157: Intent Classifier + DashboardCollector + Tool-use 루프 + Directive Submit
AADS-164: Agent Individual Call System (10 intents)

모델 라우터:
  complex  → claude-opus-4-6
  code     → claude-sonnet-4-6
  simple   → gemini-2.5-flash
  default  → claude-sonnet-4-6

Intent Classifier (10분류):
  qa           → QA Agent + Judge 실행
  design       → 스크린샷 + Vision 분석
  design_fix   → 디자인 분석 + Developer 코드 수정
  architect    → Architect Agent 설계 JSON
  dashboard    → DashboardCollector + tool-use
  diagnosis    → tool-use 활성화
  research     → tool-use 활성화
  execute      → 지시서 자동 생성 + /directives/submit
  browser      → 브라우저 자동화 tool-use
  strategy     → 현행 유지 (tool-use 없이 대화)
"""
import json
import re
import uuid
import logging
import asyncpg
import httpx
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from anthropic import AsyncAnthropic, APIStatusError
from app.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()

# ─── Anthropic 클라이언트 (1차/2차 키) ──────────────────────────────────────
anthropic_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())
_api_key_2 = settings.ANTHROPIC_API_KEY_2.get_secret_value()
anthropic_client_2: Optional[AsyncAnthropic] = AsyncAnthropic(api_key=_api_key_2) if _api_key_2 else None

# ─── OpenAI 클라이언트 (옵션) ─────────────────────────────────────────────
openai_client = None
_openai_key = settings.OPENAI_API_KEY.get_secret_value()
if _openai_key:
    try:
        from openai import AsyncOpenAI
        openai_client = AsyncOpenAI(api_key=_openai_key)
    except ImportError:
        logger.warning("openai package not installed; GPT models unavailable")

router = APIRouter()


# ─── DB 연결 ─────────────────────────────────────────────────────────────
async def _get_conn():
    return await asyncpg.connect(dsn=settings.DATABASE_URL)


# ─── Pydantic 모델 ───────────────────────────────────────────────────────
class CeoChatRequest(BaseModel):
    session_id: str = "auto"
    message: str
    model: Optional[str] = None  # T-104: CEO가 ModelSelector로 직접 선택한 모델 (None이면 자동 라우팅)


class CeoEndSessionRequest(BaseModel):
    session_id: str


# ─── 지원 모델 목록 (AADS-156) ───────────────────────────────────────────
# Claude 11개 + GPT 11개 + Gemini 6개 = 28개
SUPPORTED_MODELS: List[Dict[str, Any]] = [
    # Claude
    {"id": "claude-opus-4-6",            "provider": "anthropic", "name": "Claude Opus 4.6",            "input_$/M": 5.0,   "output_$/M": 25.0},
    {"id": "claude-sonnet-4-6",          "provider": "anthropic", "name": "Claude Sonnet 4.6",          "input_$/M": 3.0,   "output_$/M": 15.0},
    {"id": "claude-haiku-4-5-20251001",  "provider": "anthropic", "name": "Claude Haiku 4.5",           "input_$/M": 0.80,  "output_$/M": 4.0},
    {"id": "claude-opus-4-5",            "provider": "anthropic", "name": "Claude Opus 4.5",            "input_$/M": 5.0,   "output_$/M": 25.0},
    {"id": "claude-sonnet-4-5",          "provider": "anthropic", "name": "Claude Sonnet 4.5",          "input_$/M": 3.0,   "output_$/M": 15.0},
    {"id": "claude-3-5-sonnet-20241022", "provider": "anthropic", "name": "Claude 3.5 Sonnet",          "input_$/M": 3.0,   "output_$/M": 15.0},
    {"id": "claude-3-5-haiku-20241022",  "provider": "anthropic", "name": "Claude 3.5 Haiku",           "input_$/M": 0.80,  "output_$/M": 4.0},
    {"id": "claude-3-opus-20240229",     "provider": "anthropic", "name": "Claude 3 Opus",              "input_$/M": 15.0,  "output_$/M": 75.0},
    {"id": "claude-3-sonnet-20240229",   "provider": "anthropic", "name": "Claude 3 Sonnet",            "input_$/M": 3.0,   "output_$/M": 15.0},
    {"id": "claude-3-haiku-20240307",    "provider": "anthropic", "name": "Claude 3 Haiku",             "input_$/M": 0.25,  "output_$/M": 1.25},
    {"id": "claude-2.1",                 "provider": "anthropic", "name": "Claude 2.1",                 "input_$/M": 8.0,   "output_$/M": 24.0},
    # GPT
    {"id": "gpt-5",                      "provider": "openai",    "name": "GPT-5",                      "input_$/M": 10.0,  "output_$/M": 30.0},
    {"id": "gpt-5-mini",                 "provider": "openai",    "name": "GPT-5 mini",                 "input_$/M": 0.25,  "output_$/M": 2.0},
    {"id": "gpt-5.2-chat-latest",        "provider": "openai",    "name": "GPT-5.2 Chat",               "input_$/M": 5.0,   "output_$/M": 15.0},
    {"id": "gpt-4o",                     "provider": "openai",    "name": "GPT-4o",                     "input_$/M": 5.0,   "output_$/M": 15.0},
    {"id": "gpt-4o-mini",                "provider": "openai",    "name": "GPT-4o mini",                "input_$/M": 0.15,  "output_$/M": 0.60},
    {"id": "gpt-4-turbo",                "provider": "openai",    "name": "GPT-4 Turbo",                "input_$/M": 10.0,  "output_$/M": 30.0},
    {"id": "gpt-4",                      "provider": "openai",    "name": "GPT-4",                      "input_$/M": 30.0,  "output_$/M": 60.0},
    {"id": "gpt-3.5-turbo",              "provider": "openai",    "name": "GPT-3.5 Turbo",              "input_$/M": 0.5,   "output_$/M": 1.5},
    {"id": "o1",                         "provider": "openai",    "name": "o1",                         "input_$/M": 15.0,  "output_$/M": 60.0},
    {"id": "o1-mini",                    "provider": "openai",    "name": "o1-mini",                    "input_$/M": 3.0,   "output_$/M": 12.0},
    {"id": "o3-mini",                    "provider": "openai",    "name": "o3-mini",                    "input_$/M": 1.1,   "output_$/M": 4.4},
    # Gemini
    {"id": "gemini-2.5-pro",             "provider": "google",    "name": "Gemini 2.5 Pro",             "input_$/M": 7.0,   "output_$/M": 21.0},
    {"id": "gemini-3.1-pro-preview",     "provider": "google",    "name": "Gemini 3.1 Pro Preview",     "input_$/M": 2.0,   "output_$/M": 12.0},
    {"id": "gemini-2.5-flash",           "provider": "google",    "name": "Gemini 2.5 Flash",           "input_$/M": 0.30,  "output_$/M": 2.50},
    {"id": "gemini-2.0-flash",           "provider": "google",    "name": "Gemini 2.0 Flash",           "input_$/M": 0.075, "output_$/M": 0.30},
    {"id": "gemini-1.5-pro",             "provider": "google",    "name": "Gemini 1.5 Pro",             "input_$/M": 3.50,  "output_$/M": 10.50},
    {"id": "gemini-1.5-flash",           "provider": "google",    "name": "Gemini 1.5 Flash",           "input_$/M": 0.075, "output_$/M": 0.30},
]

# 빠른 조회용 dict
_MODEL_META: Dict[str, Dict] = {m["id"]: m for m in SUPPORTED_MODELS}


# ─── Intent Classifier (AADS-157 + AADS-159 + AADS-164) ──────────────────
_INTENT_PATTERNS: Dict[str, List[str]] = {
    # AADS-164: 에이전트 개별 호출 의도 (높은 우선순위)
    "design_fix":  ["디자인수정", "디자인 수정", "UI수정", "UI 수정", "CSS수정", "스타일수정"],
    "design":      ["디자인검수", "디자인 검수", "화면검수", "디자인", "UI검수", "UI 검수", "UX검수", "레이아웃"],
    "qa":          ["QA", "qa", "테스트", "검수", "품질", "QA 진행", "테스트해", "검증해",
                    "KIS", "GO100", "ShortFlow", "NTV2", "코드 검수", "백테스트", "코드검수"],
    "architect":   ["설계검토", "설계 검토", "아키텍처검토", "아키텍처 검토", "구조검토", "설계해"],
    # AADS-166: 헬스체크 의도
    "health_check": ["헬스체크", "건강", "시스템 상태", "인프라", "health", "전체 점검", "헬스 체크", "health-check", "healthcheck"],
    # AADS-170: 신규 Chat-First 인텐트 (높은 우선순위 — 기존 인텐트보다 앞에 위치)
    "casual":           ["안녕", "ㅎㅇ", "ㅋㅋ", "ㄱㄱ", "잡담", "날씨", "오늘", "기분", "피곤"],
    "deep_research":    ["deep research", "딥리서치", "심층조사", "심층 조사", "완전 조사", "보고서 써", "보고서 작성"],
    "url_analyze":      ["URL 분석", "url 분석", "링크 분석", "문서 분석", "웹페이지 분석", "URL:", "http://", "https://"],
    "video_analyze":    ["동영상 분석", "비디오 분석", "영상 분석", "유튜브 분석", "youtube 분석", "video 분석"],
    "image_analyze":    ["이미지 분석", "사진 분석", "그림 분석", "이미지를 봐", "사진을 봐", "이미지 설명"],
    "planning":         ["기획안", "로드맵", "플랜", "plan", "전략 수립", "방향 잡아", "방향성"],
    "decision":         ["결정해줘", "판단해줘", "선택해줘", "최선", "비교분석", "pros cons", "장단점"],
    "code_exec":        ["코드 실행", "실행해봐", "run", "코드 돌려", "실행 결과", "실행시켜"],
    "directive_gen":    ["지시서 만들어", "지시서 생성", "태스크 만들어", "태스크 생성", "task 생성", "지시서를 작성"],
    "memory_recall":    ["기억해", "이전에", "저번에", "지난번에", "회상", "recall", "조사 결과 찾아", "히스토리"],
    "workspace_switch": ["워크스페이스 전환", "workspace 전환", "CEO 모드", "AADS 모드", "SF 모드", "KIS 모드",
                         "GO100 모드", "NTV2 모드", "NAS 모드"],
    "search":           ["구글", "검색해줘", "찾아줘", "최신 뉴스", "news", "검색 결과"],
    # 기존 의도
    "dashboard":   ["상태", "확인", "보고", "현황", "서버", "대시보드", "요약", "overview"],
    "diagnosis":   ["왜", "안돼", "오류", "에러", "문제", "분석", "실패", "죽었", "죽어", "안됨", "error", "fail"],
    "research":    ["검색", "조사", "비교", "찾아", "최신", "찾아봐", "알아봐", "어떤", "무엇"],
    "pipeline_c":  ["파이프라인", "pipeline", "클로드봇", "claude code", "자율작업", "자율 작업", "봇한테", "봇에게",
                    "파이프라인C", "pipeline c", "파이프라인 시작", "봇 작업"],
    "execute":     ["만들어", "수정해", "고쳐", "배포", "진행", "승인", "작성해", "추가해", "구현", "지시서"],
    "strategy":    ["기획", "방향", "전략", "의도", "검토", "설계", "아키텍처", "계획"],
    # AADS-165: 실행 검증 의도
    "execution_verify": ["실행 검증", "실행해", "실행검증", "pytest", "백테스트 실행", "돌려봐", "실행 검증해"],
    # AADS-159: 브라우저 자동화 의도
    "browser":     ["스크린샷", "페이지", "열어", "화면", "브라우저", "사이트", "접속"],
}

# AADS-170 신규 인텐트 목록 (라우팅용)
_CHAT_FIRST_INTENTS = {
    "casual", "deep_research", "url_analyze", "video_analyze", "image_analyze",
    "planning", "decision", "code_exec", "directive_gen", "memory_recall",
    "workspace_switch", "search",
}

# 신규 인텐트 → 권장 모델 매핑
_CHAT_FIRST_MODEL_MAP: Dict[str, str] = {
    "casual":           "gemini-2.0-flash",
    "search":           "gemini-2.5-flash",
    "deep_research":    "gemini-2.5-pro",
    "url_analyze":      "gemini-2.5-flash",
    "video_analyze":    "gemini-2.5-flash",
    "image_analyze":    "gemini-2.5-flash",
    "planning":         "claude-sonnet-4-6",
    "decision":         "claude-opus-4-6",
    "code_exec":        "gemini-2.5-flash",
    "directive_gen":    "claude-sonnet-4-6",
    "memory_recall":    "claude-sonnet-4-6",
    "workspace_switch": "claude-sonnet-4-6",
}

_CROSS_PROJECT_NAMES = {"KIS", "GO100", "ShortFlow", "NTV2"}
_CROSS_PROJECT_QA_KEYWORDS = {"검수", "테스트", "코드", "백테스트", "분석", "검증", "리뷰", "코드검수", "코드 검수"}


def classify_intent(message: str) -> str:
    """
    메시지 의도 분류 (AADS-170 확장: 24분류).
    우선순위:
      신규(casual/deep_research/url_analyze/video_analyze/image_analyze/
           planning/decision/code_exec/directive_gen/memory_recall/
           workspace_switch/search)
      > design_fix > design > qa > execution_verify > architect > health_check
      > execute > browser > dashboard > diagnosis > research > strategy
    """
    priority_order = [
        # AADS-170 신규 (높은 우선순위)
        "workspace_switch", "directive_gen", "deep_research", "url_analyze",
        "video_analyze", "image_analyze", "memory_recall", "code_exec",
        "decision", "planning", "search", "casual",
        # 기존 (우선순위 유지)
        "design_fix", "design", "qa", "execution_verify", "architect",
        "health_check", "pipeline_c", "execute", "browser", "dashboard", "diagnosis",
        "research", "strategy",
    ]
    for intent in priority_order:
        if intent not in _INTENT_PATTERNS:
            continue
        if any(kw in message for kw in _INTENT_PATTERNS[intent]):
            # 프로젝트명만 매칭된 경우: QA 키워드 동반 확인 (예: "KIS 상태" → dashboard)
            if intent == "qa":
                matched_kw = [kw for kw in _INTENT_PATTERNS[intent] if kw in message]
                is_only_project = all(kw in _CROSS_PROJECT_NAMES for kw in matched_kw)
                if is_only_project and not any(qk in message for qk in _CROSS_PROJECT_QA_KEYWORDS):
                    continue  # 프로젝트명만 있고 QA 키워드 없으면 건너뛰기
            return intent
    return "strategy"


# ─── 크로스 프로젝트 도구 (AADS-165) ──────────────────────────────────────
_PROJECT_NAME_MAP = {
    "KIS": "KIS", "kis": "KIS",
    "GO100": "GO100", "go100": "GO100",
    "ShortFlow": "SF", "SF": "SF", "sf": "SF", "숏플로우": "SF",
    "NTV2": "NTV2", "ntv2": "NTV2", "뉴톡": "NTV2",
}


def _extract_project(message: str) -> Optional[str]:
    """메시지에서 프로젝트명 추출. 없으면 None."""
    for keyword, project in _PROJECT_NAME_MAP.items():
        if keyword in message:
            return project
    return None


_CODE_REVIEW_SYSTEM_PROMPT = """당신은 시니어 코드 리뷰어입니다. 제공된 코드를 다음 기준으로 분석하세요:

1. **코드 품질** (가독성, 구조, 네이밍)
2. **로직 오류** (잠재적 버그, 엣지 케이스)
3. **보안 취약점** (하드코딩된 시크릿, SQL 인젝션, 입력 검증)
4. **성능** (비효율적 루프, 메모리 누수, 불필요한 I/O)
5. **테스트 커버리지** (테스트 파일 존재 여부, 테스트 패턴)

종합 판정: PASS / WARNING / FAIL + 개선 사항 목록을 한국어로 제공하세요.
응답 마지막에 다음 안내를 추가하세요:
"실행 검증(pytest/백테스트 실행)이 필요하면 '실행 검증해줘'라고 입력하세요."
"""


# ─── Model Router ────────────────────────────────────────────────────────
def route_model(message: str) -> str:
    """메시지 키워드에 따라 최적 모델 선택 (T-073 지시서 기준)."""
    simple     = ['실행해', '결과', '상태', '확인', '스크린샷', '봐', '알려']
    code       = ['수정해', '만들어', '추가해', '지시서', '코드', '고쳐', '수정']
    complex_kw = ['설계', '분석', '개선안', '보고', '아키텍처', '전략', '검토', '평가']
    if any(p in message for p in complex_kw):
        return 'claude-opus-4-6'
    if any(p in message for p in code):
        return 'claude-sonnet-4-6'
    if any(p in message for p in simple):
        return 'gemini-2.5-flash'
    return 'claude-sonnet-4-6'


# ─── 비용 계산 ────────────────────────────────────────────────────────────
def _get_pricing(model: str) -> Dict[str, float]:
    meta = _MODEL_META.get(model)
    if meta:
        return {"input": meta["input_$/M"], "output": meta["output_$/M"]}
    return {"input": 3.0, "output": 15.0}  # 알 수 없는 모델 → Sonnet 수준


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = _get_pricing(model)
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


# 지시서 표시용 모델명
def _model_display_name(model: str) -> str:
    meta = _MODEL_META.get(model)
    if meta:
        return meta["name"]
    # 패턴 기반 fallback
    if "haiku" in model:
        return "Claude Haiku"
    if "sonnet" in model:
        return "Claude Sonnet"
    if "opus" in model:
        return "Claude Opus"
    if "gemini" in model:
        return model
    if "gpt" in model or model.startswith("o1") or model.startswith("o3"):
        return model
    return model


# ─── LLM 호출 ─────────────────────────────────────────────────────────────
async def call_llm(model: str, system_prompt: str, messages: List[Dict]) -> Tuple[str, int, int]:
    """모델에 따라 적합한 API 호출 → (응답텍스트, input_tokens, output_tokens)"""
    if model.startswith('gemini'):
        return await _call_gemini(model, system_prompt, messages)
    if model.startswith('gpt') or model.startswith('o1') or model.startswith('o3'):
        return await _call_openai(model, system_prompt, messages)
    return await _call_anthropic(model, system_prompt, messages)


async def _call_anthropic(model: str, system_prompt: str, messages: List[Dict]) -> Tuple[str, int, int]:
    """Anthropic API 호출. 402(credit_balance_too_low) 시 2차 키로 자동 전환."""
    clients = [c for c in [anthropic_client, anthropic_client_2] if c is not None]
    last_exc: Optional[Exception] = None
    for client in clients:
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=2000,
                system=system_prompt,
                messages=messages,
            )
            text = resp.content[0].text
            return text, resp.usage.input_tokens, resp.usage.output_tokens
        except APIStatusError as e:
            if e.status_code == 402:
                logger.warning(
                    "anthropic_credit_exhausted_402",
                    model=model,
                    key_index=clients.index(client) + 1,
                    trying_next=(client is not clients[-1]),
                )
                last_exc = e
                continue
            raise
    raise last_exc or RuntimeError("All Anthropic API keys exhausted")


async def _call_openai(model: str, system_prompt: str, messages: List[Dict]) -> Tuple[str, int, int]:
    """OpenAI API 호출."""
    if openai_client is None:
        logger.warning(f"OpenAI client unavailable, falling back to claude-sonnet-4-6 for model={model}")
        return await _call_anthropic('claude-sonnet-4-6', system_prompt, messages)
    all_messages = [{"role": "system", "content": system_prompt}] + messages
    resp = await openai_client.chat.completions.create(
        model=model,
        max_tokens=2000,
        messages=all_messages,
    )
    text = resp.choices[0].message.content or ""
    input_tokens = resp.usage.prompt_tokens
    output_tokens = resp.usage.completion_tokens
    return text, input_tokens, output_tokens


async def _call_anthropic_with_tools(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
    max_iterations: int = 5,
) -> Tuple[str, int, int]:
    """Tool-use 루프 포함 Anthropic 호출 (AADS-157).

    while 루프: stop_reason='tool_use' → 도구 실행 → 결과 재전달.
    stop_reason='end_turn' 또는 max_iterations 초과 시 종료.
    """
    from app.api.ceo_chat_tools import TOOL_DEFINITIONS, execute_tool

    clients = [c for c in [anthropic_client, anthropic_client_2] if c is not None]
    if not clients:
        raise RuntimeError("Anthropic API 클라이언트 없음")

    total_input = 0
    total_output = 0
    current_messages = list(messages)

    for iteration in range(max_iterations):
        last_exc: Optional[Exception] = None
        resp = None
        for client in clients:
            try:
                resp = await client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=current_messages,
                    tools=TOOL_DEFINITIONS,
                )
                break
            except APIStatusError as e:
                if e.status_code == 402:
                    last_exc = e
                    continue
                raise
        if resp is None:
            raise last_exc or RuntimeError("All Anthropic API keys exhausted")

        total_input += resp.usage.input_tokens
        total_output += resp.usage.output_tokens

        if resp.stop_reason == "end_turn":
            text = "".join(
                block.text for block in resp.content if hasattr(block, "text")
            )
            return text, total_input, total_output

        if resp.stop_reason == "tool_use":
            # assistant 메시지에 content blocks 추가
            assistant_content = []
            tool_use_blocks = []
            for block in resp.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                    tool_use_blocks.append(block)

            current_messages.append({"role": "assistant", "content": assistant_content})

            # 도구 실행 및 결과 수집
            tool_results = []
            for block in tool_use_blocks:
                logger.info(f"ceo_chat_tool_call tool={block.name} params={block.input}")
                result = await execute_tool(block.name, block.input, dsn)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            current_messages.append({"role": "user", "content": tool_results})
            continue

        # 기타 stop_reason (max_tokens 등)
        text = "".join(
            block.text for block in resp.content if hasattr(block, "text")
        )
        return text, total_input, total_output

    # max_iterations 초과
    return "[경고] 도구 호출 최대 반복 횟수 초과. 부분 결과만 반환됩니다.", total_input, total_output


async def _call_gemini(model: str, system_prompt: str, messages: List[Dict]) -> Tuple[str, int, int]:
    """Google Gemini 호출. 실패 시 Sonnet으로 fallback."""
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
        api_key = settings.GOOGLE_API_KEY.get_secret_value()
        if not api_key:
            raise ValueError("GOOGLE_API_KEY not set")

        llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            max_output_tokens=2000,
            temperature=0.1,
        )
        lc_msgs = [SystemMessage(content=system_prompt)]
        for m in messages:
            if m['role'] == 'user':
                lc_msgs.append(HumanMessage(content=m['content']))
            elif m['role'] == 'assistant':
                lc_msgs.append(AIMessage(content=m['content']))

        result = await llm.ainvoke(lc_msgs)
        text = result.content if hasattr(result, 'content') else str(result)

        usage = getattr(result, 'usage_metadata', None)
        if usage:
            in_tok  = getattr(usage, 'input_tokens', None) or getattr(usage, 'prompt_token_count', 0)
            out_tok = getattr(usage, 'output_tokens', None) or getattr(usage, 'candidates_token_count', 0)
        else:
            in_tok  = len(system_prompt.split()) + sum(len(m['content'].split()) for m in messages)
            out_tok = len(text.split())
        return text, in_tok, out_tok

    except Exception as e:
        logger.warning(f"Gemini call failed, fallback to claude-sonnet-4-6: {e}")
        return await _call_anthropic('claude-sonnet-4-6', system_prompt, messages)


# ─── Context Manager ─────────────────────────────────────────────────────
class ContextManager:
    def __init__(self, conn):
        self.conn = conn

    async def load_facts(self, categories: Optional[List[str]] = None) -> str:
        if categories:
            rows = await self.conn.fetch(
                "SELECT category, key, value FROM ceo_facts WHERE category = ANY($1) ORDER BY category, key",
                categories,
            )
        else:
            rows = await self.conn.fetch(
                "SELECT category, key, value FROM ceo_facts ORDER BY category, key"
            )
        if not rows:
            return ""
        lines = ["[인프라/프로젝트 Facts]"]
        current_cat = None
        for r in rows:
            if r['category'] != current_cat:
                current_cat = r['category']
                lines.append(f"  [{current_cat}]")
            lines.append(f"    {r['key']}: {r['value']}")
        return "\n".join(lines)

    async def load_session_summary(self, n: int = 3) -> str:
        rows = await self.conn.fetch(
            """SELECT cs.session_id, css.summary, css.key_decisions, css.pending_actions, cs.started_at
               FROM ceo_session_summaries css
               JOIN ceo_chat_sessions cs ON cs.session_id = css.session_id
               ORDER BY css.created_at DESC LIMIT $1""",
            n,
        )
        if not rows:
            return ""
        lines = [f"[최근 {len(rows)}개 세션 요약]"]
        for r in rows:
            lines.append(f"  세션 {r['session_id'][:8]}... ({r['started_at'].strftime('%m/%d %H:%M')})")
            if r['summary']:
                lines.append(f"    요약: {r['summary'][:200]}")
            if r['key_decisions']:
                lines.append(f"    결정사항: {r['key_decisions'][:150]}")
            if r['pending_actions']:
                lines.append(f"    미결사항: {r['pending_actions'][:150]}")
        return "\n".join(lines)

    async def load_active_tasks(self) -> List[Dict]:
        try:
            rows = await self.conn.fetch(
                "SELECT task_id, title, status, project FROM task_tracking "
                "WHERE status IN ('pending','running') ORDER BY created_at DESC LIMIT 10"
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def load_recent_turns(self, session_id: str, n: int = 3) -> str:
        rows = await self.conn.fetch(
            """SELECT role, content, model_used, created_at FROM ceo_chat_messages
               WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2""",
            session_id, n * 2,
        )
        if not rows:
            return ""
        rows = list(reversed(rows))
        lines = [f"[최근 대화 (최대 {n}턴)]"]
        for r in rows:
            role_label = "CEO" if r['role'] == 'user' else "AI"
            lines.append(f"  {role_label}: {r['content'][:300]}")
        return "\n".join(lines)

    async def build_context(self, session_id: str) -> str:
        """Layer 1~4 조합하여 시스템 프롬프트 구성. 예상 토큰: 3,500~5,500"""
        facts             = await self.load_facts()
        session_summaries = await self.load_session_summary(3)
        active_tasks      = await self.load_active_tasks()
        recent_turns      = await self.load_recent_turns(session_id, 3)

        parts = [
            "당신은 AADS(Autonomous AI Development System)의 CEO 어시스턴트입니다.",
            "CEO가 인프라, 프로젝트, 작업을 관리할 수 있도록 도와주는 역할을 합니다.",
            "",
        ]

        if facts:
            parts.append(facts)
            parts.append("")

        if session_summaries:
            parts.append(session_summaries)
            parts.append("")

        if active_tasks:
            parts.append(f"[현재 진행중 작업 ({len(active_tasks)}개)]")
            for t in active_tasks:
                parts.append(
                    f"  {t.get('task_id','?')}: {t.get('title','?')} "
                    f"[{t.get('status','?')}] - {t.get('project','?')}"
                )
            parts.append("")

        if recent_turns:
            parts.append(recent_turns)
            parts.append("")

        parts.append("간결하고 실용적으로 답변하세요. 지시서 생성이 필요하면 구체적인 내용을 제시하세요.")

        # Pipeline C 가이드
        parts.append("")
        parts.append("""[Pipeline C — Claude Code 자율 작업 시스템]
각 서버(211/114/68)에 설치된 Claude Code CLI에 직접 작업을 지시할 수 있습니다.

사용 가능한 도구:
- pipeline_c_start(project, instruction): 파이프라인 시작 (작업→자동검수→승인대기)
- pipeline_c_status(job_id): 진행 상황 확인
- pipeline_c_approve(job_id, approved, reason): 승인(배포) 또는 거부(원복)

프로젝트: KIS(211서버), GO100(211서버), SF(114서버), NTV2(114서버), AADS(68서버)

플로우: 작업지시 → Claude Code 자율수행 → AI 자동검수 → 재지시(필요시) → CEO 승인 대기 → 승인 시 git commit+push+서비스재시작+최종검증

CEO가 "클로드봇에게 시켜", "파이프라인 시작", "봇한테 맡겨" 등 요청하면 pipeline_c_start를 사용하세요.
간단한 조회(파일 목록, 상태 확인)는 read_remote_file/list_remote_dir로 직접 처리하세요.
코드 수정/버그 수정/리팩토링 등 복잡한 작업만 파이프라인C를 사용하세요.""")

        return "\n".join(parts)


# ─── Dashboard Collector (AADS-157) ──────────────────────────────────────
class DashboardCollector:
    """상태확인 시 6개 소스에서 데이터 자동 수집."""

    def __init__(self, conn, dsn: str):
        self.conn = conn
        self.dsn = dsn

    async def _fetch_health(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get("http://localhost:8080/api/v1/health")
                return r.text[:2000]
        except Exception as e:
            return f"(health 조회 실패: {e})"

    async def _fetch_status_md(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/STATUS.md"
                )
                return r.text[:3000] if r.status_code == 200 else f"(STATUS.md 없음: {r.status_code})"
        except Exception as e:
            return f"(STATUS.md 조회 실패: {e})"

    async def _fetch_projects(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get("https://aads.newtalk.kr/api/v1/projects")
                return r.text[:2000]
        except Exception as e:
            return f"(projects 조회 실패: {e})"

    async def _fetch_session_cost(self) -> str:
        try:
            row = await self.conn.fetchrow(
                """SELECT
                     COUNT(*) AS total_sessions,
                     COALESCE(SUM(total_cost_usd), 0) AS total_cost,
                     COALESCE(SUM(total_turns), 0) AS total_turns
                   FROM ceo_chat_sessions
                   WHERE started_at >= date_trunc('month', now())"""
            )
            return (
                f"이번달 세션: {row['total_sessions']}개, "
                f"총 비용: ${float(row['total_cost']):.4f}, "
                f"총 턴: {int(row['total_turns'])}"
            )
        except Exception as e:
            return f"(세션 비용 집계 실패: {e})"

    async def _fetch_task_tracking(self) -> str:
        try:
            rows = await self.conn.fetch(
                """SELECT task_id, title, status, project
                   FROM task_tracking
                   WHERE status IN ('pending', 'running')
                   ORDER BY created_at DESC LIMIT 10"""
            )
            if not rows:
                return "(진행중 태스크 없음)"
            lines = [f"  {r['task_id']}: [{r['status']}] {r['title']} ({r['project']})" for r in rows]
            return "\n".join(lines)
        except Exception as e:
            return f"(태스크 현황 조회 실패: {e})"

    async def collect(self) -> Dict[str, str]:
        """6개 소스 병렬 수집."""
        import asyncio
        health, status_md, projects, session_cost, task_tracking = await asyncio.gather(
            self._fetch_health(),
            self._fetch_status_md(),
            self._fetch_projects(),
            self._fetch_session_cost(),
            self._fetch_task_tracking(),
            return_exceptions=False,
        )
        return {
            "health": health,
            "status_md": status_md,
            "projects": projects,
            "session_cost": session_cost,
            "task_tracking": task_tracking,
        }


def _inject_dashboard(system_prompt: str, data: Dict[str, str]) -> str:
    """수집된 대시보드 데이터를 system_prompt에 주입."""
    dashboard_section = f"""
[실시간 대시보드 데이터 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}]

[Health 상태]
{data.get('health', '(없음)')}

[STATUS.md]
{data.get('status_md', '(없음)')}

[Projects]
{data.get('projects', '(없음)')}

[이번달 CEO Chat 비용]
{data.get('session_cost', '(없음)')}

[진행중 태스크]
{data.get('task_tracking', '(없음)')}
"""
    return system_prompt + "\n" + dashboard_section


async def _handle_health_check_intent(
    session_id: str = "",
) -> Tuple[str, int, int]:
    """AADS-166: 헬스체크 의도 처리 — /api/v1/ops/full-health 호출 후 한국어 요약."""
    import time
    start = time.time()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("http://localhost:8080/api/v1/ops/full-health")
            data = r.json() if r.status_code == 200 else {"status": "ERROR", "error": f"HTTP {r.status_code}"}
    except Exception as e:
        data = {"status": "ERROR", "error": str(e)}

    duration_ms = int((time.time() - start) * 1000)

    status = data.get("status", "UNKNOWN")
    status_emoji = {"HEALTHY": "HEALTHY", "DEGRADED": "DEGRADED", "CRITICAL": "CRITICAL"}.get(status, status)
    sections = data.get("sections", {})
    issues = data.get("issues", [])

    lines = [f"[시스템 헬스체크 결과: {status_emoji}] ({duration_ms}ms)"]
    lines.append("")

    # 파이프라인
    pipeline = sections.get("pipeline", {})
    p_status = pipeline.get("overall", "?")
    lines.append(f"  파이프라인: {'정상' if p_status == 'HEALTHY' else p_status}")
    s211 = pipeline.get("server_211", {})
    if isinstance(s211, dict) and s211.get("reachable"):
        bridge = s211.get("bridge_py", s211.get("bridge", {}))
        if isinstance(bridge, dict) and bridge.get("running"):
            lines.append(f"    bridge PID {bridge.get('pid', '?')}")

    # 인프라
    infra = sections.get("infra", {})
    i_status = infra.get("overall", "?")
    lines.append(f"  인프라: {'정상' if i_status == 'HEALTHY' else i_status}")
    for key in ["db", "ssh_211", "ssh_114", "disk_68", "disk_211", "disk_114", "memory_68"]:
        check = infra.get(key, {})
        if isinstance(check, dict) and not check.get("ok", True):
            detail = check.get("error", check.get("severity", ""))
            if key.startswith("disk_"):
                detail = f"사용률 {check.get('usage_pct', '?')}%"
            lines.append(f"    {key}: {detail}")

    # 정합성
    consistency = sections.get("consistency", {})
    c_status = consistency.get("overall", "?")
    lines.append(f"  정합성: {'정상' if c_status == 'HEALTHY' else c_status}")
    pending_sync = consistency.get("pending_sync", {})
    if isinstance(pending_sync, dict) and not pending_sync.get("ok", True):
        lines.append(f"    DB {pending_sync.get('db_queued', '?')}건 queued, 폴더 {pending_sync.get('folder_count', '?')}건")

    # 디렉티브
    directives = sections.get("directives", {})
    if isinstance(directives, dict):
        counts = []
        for s in ["pending", "running", "done"]:
            d = directives.get(s, {})
            if isinstance(d, dict):
                counts.append(f"{s}: {d.get('count', 0)}")
        if counts:
            lines.append(f"  디렉티브: {', '.join(counts)}")

    # 이슈
    if issues:
        lines.append("")
        lines.append(f"  이슈 {len(issues)}건:")
        for issue in issues[:5]:
            lines.append(f"    - [{issue.get('severity', 'info')}] {issue.get('type', '?')}: {issue.get('detail', '')[:100]}")

    response_text = "\n".join(lines)
    return response_text, 0, 0


async def _handle_execute_intent(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
) -> Tuple[str, int, int]:
    """execute 의도 처리: LLM으로 지시서 생성 → submit → 응답 반환."""
    from app.api.directives import DirectiveSubmitRequest, submit_directive_sync

    # 지시서 생성 전용 프롬프트
    directive_system = (
        system_prompt
        + "\n\n[지시서 생성 모드]\n"
        "CEO의 요청을 분석하여 D-022 포맷 지시서를 JSON으로 생성하세요.\n"
        "반드시 아래 JSON 형식만 반환하세요 (마크다운 코드블록 없이):\n"
        '{"task_id": "AADS-XXX", "project": "AADS", "priority": "P2", '
        '"size": "S", "description": "...", "success_criteria": "...", '
        '"files_owned": ["..."], "impact": "M", "effort": "M"}\n'
        "task_id는 현재 최신 번호 다음 번호를 사용하세요 (알 수 없으면 AADS-200 사용).\n"
        "size: XS/S/M/L/XL, priority: P0-CRITICAL/P1/P2/P3, impact/effort: H/M/L"
    )

    # Anthropic 모델로 지시서 JSON 생성
    tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
    clients = [c for c in [anthropic_client, anthropic_client_2] if c is not None]

    resp_text = ""
    total_input = 0
    total_output = 0
    for client in clients:
        try:
            resp = await client.messages.create(
                model=tool_model,
                max_tokens=1024,
                system=directive_system,
                messages=messages,
            )
            resp_text = resp.content[0].text
            total_input = resp.usage.input_tokens
            total_output = resp.usage.output_tokens
            break
        except APIStatusError as e:
            if e.status_code == 402:
                continue
            raise

    if not resp_text:
        return "지시서 생성 실패 (API 키 소진)", 0, 0

    # JSON 파싱
    try:
        json_match = re.search(r"\{.*\}", resp_text, re.DOTALL)
        if not json_match:
            raise ValueError("JSON 없음")
        data = json.loads(json_match.group())
    except Exception as e:
        logger.warning(f"directive_json_parse_failed: {e}, raw={resp_text[:500]}")
        return f"지시서 JSON 파싱 실패. LLM 응답:\n{resp_text}", total_input, total_output

    # 지시서 제출
    try:
        req = DirectiveSubmitRequest(
            task_id=data.get("task_id", "AADS-200"),
            project=data.get("project", "AADS"),
            priority=data.get("priority", "P2"),
            size=data.get("size", "S"),
            description=data.get("description", ""),
            success_criteria=data.get("success_criteria"),
            files_owned=data.get("files_owned"),
            impact=data.get("impact", "M"),
            effort=data.get("effort", "M"),
        )
        result = submit_directive_sync(req)
        response_text = (
            f"지시서 생성 완료.\n"
            f"  task_id: {result.task_id}\n"
            f"  파일: {result.filename}\n"
            f"  경로: {result.path}\n\n"
            f"파이프라인이 감지하면 자동 실행됩니다."
        )
    except Exception as e:
        logger.error(f"directive_submit_failed: {e}")
        response_text = (
            f"지시서 파일 생성 실패: {e}\n\n"
            f"생성된 지시서 내용 (수동 투입 가능):\n{resp_text}"
        )

    return response_text, total_input, total_output


# ─── AADS-164: Agent Individual Call Handlers ─────────────────────────────

async def _log_agent_execution(
    conn, session_id: str, agent_type: str, intent: str,
    input_summary: str, output_summary: str, status: str,
    cost_usd: float, duration_ms: int, error_message: str = None,
):
    """agent_executions 테이블에 실행 이력 저장."""
    try:
        await conn.execute(
            """INSERT INTO agent_executions
               (session_id, agent_type, intent, input_summary, output_summary, status, cost_usd, duration_ms, error_message, completed_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())""",
            session_id, agent_type, intent, input_summary[:500],
            output_summary[:2000] if output_summary else None,
            status, cost_usd, duration_ms, error_message,
        )
    except Exception as e:
        logger.warning(f"agent_execution_log_failed: {e}")


async def _handle_qa_intent(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
    session_id: str = "",
) -> Tuple[str, int, int]:
    """QA 의도: 크로스 프로젝트 감지 시 SSH 정적 분석, 아니면 qa_node + judge_node 실행."""
    import time

    start = time.time()
    user_msg = messages[-1]["content"] if messages else ""

    # 크로스 프로젝트 감지 (AADS-165)
    detected_project = _extract_project(user_msg)
    if detected_project:
        return await _handle_cross_project_qa(
            model, system_prompt, messages, dsn, session_id, detected_project, user_msg
        )

    # 기존 로컬 QA: qa_node + judge_node (AADS-164)
    from app.services.agent_state_builder import build_agent_state

    state = build_agent_state(
        description=f"CEO QA 요청: {user_msg}",
        success_criteria=["CEO가 지정한 기능/페이지의 정상 동작 확인"],
    )

    results = []
    total_cost = 0.0

    # 1) QA Agent 호출
    try:
        from app.agents.qa_agent import qa_node
        qa_result = await qa_node(state)
        state.update(qa_result)
        qa_tests = qa_result.get("qa_test_results", [])
        if qa_tests:
            last = qa_tests[-1] if isinstance(qa_tests, list) and qa_tests else qa_tests
            results.append(f"**QA 결과**: {last.get('status', 'unknown')} — 통과: {last.get('tests_passed', 0)}/{last.get('tests_total', 0)}")
        else:
            results.append("**QA 결과**: 테스트 항목 없음")
        total_cost += state.get("total_cost_usd", 0)
    except Exception as e:
        logger.error(f"qa_node_failed: {e}")
        results.append(f"**QA Agent 오류**: {str(e)[:200]}")

    # 2) Judge Agent 호출
    try:
        from app.agents.judge_agent import judge_node
        judge_result = await judge_node(state)
        verdict = judge_result.get("judge_verdict", {})
        results.append(
            f"**Judge 판정**: {verdict.get('verdict', 'N/A')} "
            f"(점수: {verdict.get('score', 0):.2f})\n"
            f"  추천: {verdict.get('recommendation', 'N/A')}"
        )
        if verdict.get("issues"):
            results.append(f"  이슈: {', '.join(verdict['issues'][:3])}")
        total_cost += judge_result.get("total_cost_usd", state.get("total_cost_usd", 0))
    except Exception as e:
        logger.error(f"judge_node_failed: {e}")
        results.append(f"**Judge Agent 오류**: {str(e)[:200]}")

    duration_ms = int((time.time() - start) * 1000)
    response_text = f"[QA + Judge 실행 완료] ({duration_ms}ms, ${total_cost:.4f})\n\n" + "\n".join(results)

    # 실행 이력 로깅 (non-blocking)
    conn = None
    try:
        conn = await asyncpg.connect(dsn=dsn)
        await _log_agent_execution(
            conn, session_id, "qa+judge", "qa", user_msg[:200],
            response_text, "success", total_cost, duration_ms,
        )
    except Exception:
        pass
    finally:
        if conn:
            await conn.close()

    # 토큰은 에이전트 내부에서 소비되므로 여기서는 0 반환 (비용은 agent 내부 추적)
    return response_text, 0, 0


async def _handle_cross_project_qa(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
    session_id: str,
    project: str,
    user_msg: str,
) -> Tuple[str, int, int]:
    """크로스 프로젝트 SSH 정적 분석 (AADS-165 1단계)."""
    import time
    from app.api.ceo_chat_tools import tool_list_remote_dir, tool_read_remote_file

    start = time.time()

    # 사용자 메시지에서 핵심어 추출 (한국어→영어 키워드 매핑)
    keyword_map = {
        "백테스트": "backtest", "매매": "trade", "주문": "order",
        "전략": "strategy", "봇": "bot", "매수": "buy", "매도": "sell",
        "잔고": "balance", "로그": "log", "설정": "config",
        "메인": "main", "서버": "server", "API": "api", "api": "api",
    }
    search_keyword = ""
    for kr, en in keyword_map.items():
        if kr in user_msg:
            search_keyword = en
            break

    # 1) 파일 탐색
    dir_result = await tool_list_remote_dir(project, "", search_keyword, 3)
    files = []
    if not dir_result.startswith("[ERROR]"):
        for line in dir_result.splitlines():
            line = line.strip()
            if line and not line.startswith("[") and line.startswith("/"):
                files.append(line)

    if not files:
        # 키워드 없이 재시도
        dir_result = await tool_list_remote_dir(project, "", "", 2)
        if not dir_result.startswith("[ERROR]"):
            for line in dir_result.splitlines():
                line = line.strip()
                if line and not line.startswith("[") and line.startswith("/"):
                    files.append(line)

    if not files:
        duration_ms = int((time.time() - start) * 1000)
        return (
            f"[{project} 크로스 프로젝트 검수] ({duration_ms}ms)\n\n"
            f"파일 탐색 결과 없음.\n{dir_result}\n\n"
            "프로젝트 경로나 키워드를 더 구체적으로 지정해주세요.",
            0, 0,
        )

    # 2) 상위 5개 파일 읽기 (Python/JS 우선)
    code_extensions = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs"}
    prioritized = sorted(
        files,
        key=lambda f: (0 if any(f.endswith(ext) for ext in code_extensions) else 1, f),
    )[:5]

    code_contents = []
    from app.api.ceo_chat_tools import _PROJECT_SERVER_MAP
    workdir = _PROJECT_SERVER_MAP.get(project, {}).get("workdir", "")
    for fpath in prioritized:
        rel_path = fpath[len(workdir):].lstrip("/") if fpath.startswith(workdir) else fpath
        content = await tool_read_remote_file(project, rel_path)
        if not content.startswith("[ERROR]"):
            code_contents.append(f"### {rel_path}\n```\n{content[:8000]}\n```")

    if not code_contents:
        duration_ms = int((time.time() - start) * 1000)
        return (
            f"[{project} 크로스 프로젝트 검수] ({duration_ms}ms)\n\n"
            f"파일 {len(prioritized)}개 발견했으나 읽기 실패.\n\n"
            "SSH 연결 또는 파일 권한 문제일 수 있습니다.",
            0, 0,
        )

    # 3) LLM 정적 분석
    analysis_messages = [
        {"role": "user", "content": (
            f"CEO가 [{project}] 프로젝트 코드 검수를 요청했습니다.\n"
            f"CEO 원문: {user_msg}\n\n"
            f"탐색된 파일 {len(files)}개 중 상위 {len(code_contents)}개 코드:\n\n"
            + "\n\n".join(code_contents)
        )},
    ]

    analysis_model = model if model.startswith("claude") else "claude-sonnet-4-6"
    try:
        response = await anthropic_client.messages.create(
            model=analysis_model,
            max_tokens=4096,
            system=_CODE_REVIEW_SYSTEM_PROMPT,
            messages=analysis_messages,
        )
        analysis_text = response.content[0].text if response.content else "(분석 결과 없음)"
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
    except Exception as e:
        logger.error(f"cross_project_qa_llm_failed: {e}")
        analysis_text = f"LLM 분석 실패: {str(e)[:300]}"
        input_tokens, output_tokens = 0, 0

    duration_ms = int((time.time() - start) * 1000)
    cost = calc_cost(analysis_model, input_tokens, output_tokens)

    response_text = (
        f"[{project} 크로스 프로젝트 정적 분석] ({duration_ms}ms, ${cost:.4f})\n"
        f"분석 대상: {', '.join(f.split('/')[-1] for f in prioritized)}\n\n"
        f"{analysis_text}"
    )

    # 세션에 분석 컨텍스트 저장 (execution_verify용)
    conn = None
    try:
        conn = await asyncpg.connect(dsn=dsn)
        await _log_agent_execution(
            conn, session_id, "cross-project-qa", "qa", user_msg[:200],
            response_text[:500], "success", cost, duration_ms,
        )
        # 세션 메모리에 분석 대상 파일 저장
        await conn.execute(
            """INSERT INTO system_memory (category, key, value)
               VALUES ('cross_project_qa', $1, $2)
               ON CONFLICT (category, key) DO UPDATE SET value = $2""",
            session_id,
            json.dumps({"project": project, "files": prioritized, "workdir": workdir}),
        )
    except Exception as e:
        logger.warning(f"cross_project_qa_log_failed: {e}")
    finally:
        if conn:
            await conn.close()

    return response_text, input_tokens, output_tokens


async def _handle_execution_verify_intent(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
    session_id: str = "",
) -> Tuple[str, int, int]:
    """실행 검증 의도: 직전 QA 세션의 프로젝트 정보로 claudebot 지시서 자동 생성 (AADS-165 3단계)."""
    from datetime import datetime

    user_msg = messages[-1]["content"] if messages else ""

    # 세션 메모리에서 직전 cross-project QA 정보 가져오기
    project_info = None
    conn = None
    try:
        conn = await asyncpg.connect(dsn=dsn)
        row = await conn.fetchrow(
            "SELECT value FROM system_memory WHERE category = 'cross_project_qa' AND key = $1",
            session_id,
        )
        if row:
            project_info = json.loads(row["value"])
    except Exception as e:
        logger.warning(f"execution_verify_load_session_failed: {e}")
    finally:
        if conn:
            await conn.close()

    if not project_info:
        # 직접 프로젝트 추출 시도
        detected = _extract_project(user_msg)
        if detected:
            from app.api.ceo_chat_tools import _PROJECT_SERVER_MAP
            mapping = _PROJECT_SERVER_MAP.get(detected, {})
            project_info = {
                "project": detected,
                "files": [],
                "workdir": mapping.get("workdir", ""),
            }
        else:
            return (
                "직전 크로스 프로젝트 QA 세션이 없습니다. "
                "먼저 'KIS 코드 검수해' 등으로 정적 분석을 실행한 후 '실행 검증해줘'를 입력하세요.",
                0, 0,
            )

    project = project_info["project"]
    files = project_info.get("files", [])
    workdir = project_info.get("workdir", "")

    from app.api.ceo_chat_tools import _PROJECT_SERVER_MAP
    mapping = _PROJECT_SERVER_MAP.get(project, {})
    server_ip = mapping.get("server", "unknown")

    # 서버 번호 매핑
    server_label = "211" if "211" in server_ip else "114" if "155" in server_ip else "68"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"{project}-VERIFY-{ts}"
    file_list = "\n".join(f"  - {f}" for f in files[:10]) if files else "  (정적 분석 시 탐색된 파일 전체)"

    directive_content = f""">>>DIRECTIVE_START
TASK_ID: {task_id}
TITLE: "{project} 코드 실행 검증 -- CEO Chat 요청"
PRIORITY: P1-HIGH
SIZE: M
MODEL: sonnet
SERVER: {server_label}
WORKDIR: {workdir}
ASSIGNEE: Claude ({server_label}, {workdir})
DESCRIPTION: |
  CEO Chat에서 정적 분석 완료된 파일들에 대한 실행 검증.
  대상 파일:
{file_list}
  수행 항목:
  1. pytest 실행 (존재 시)
  2. 코드 실행 가능 여부 확인 (import 오류, syntax 오류)
  3. 주요 함수 단위 테스트
SUCCESS_CRITERIA: |
  1. 모든 테스트 통과 또는 실패 목록 제공
  2. 실행 오류 0건 또는 오류 목록 제공
  3. 결과 보고서 reports/{task_id}-RESULT.md 생성
<<<DIRECTIVE_END"""

    # /directives/submit 으로 제출
    try:
        from app.api.directives import DirectiveSubmitRequest, submit_directive_sync
        submit_req = DirectiveSubmitRequest(
            task_id=task_id,
            project=project,
            priority="P1",
            size="M",
            description=f"CEO Chat 실행 검증 요청: {project} 코드 실행 검증",
            success_criteria="모든 테스트 통과\n실행 오류 0건\n결과 보고서 생성",
            files_owned=files[:10] if files else None,
        )
        submit_result = submit_directive_sync(submit_req)
        submit_status = "제출 완료"
    except Exception as e:
        logger.error(f"execution_verify_submit_failed: {e}")
        submit_status = f"제출 실패: {str(e)[:200]}"

    response_text = (
        f"[{project} 실행 검증 지시서 생성]\n\n"
        f"Task ID: `{task_id}`\n"
        f"대상 서버: {server_label} ({server_ip})\n"
        f"대상 경로: {workdir}\n"
        f"파일 수: {len(files)}개\n"
        f"지시서 상태: {submit_status}\n\n"
        "claudebot이 실행 검증을 시작합니다. 완료되면 결과가 자동 보고됩니다."
    )

    return response_text, 0, 0


async def _handle_design_intent(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
    session_id: str = "",
) -> Tuple[str, int, int]:
    """디자인 검수: 스크린샷 + Claude Vision 분석."""
    import time
    start = time.time()
    user_msg = messages[-1]["content"] if messages else ""

    # tool-use로 스크린샷 + Vision 분석
    design_prompt = (
        system_prompt
        + "\n\n[디자인 검수 모드]\n"
        "CEO가 디자인/UI 검수를 요청했습니다.\n"
        "1. 관련 페이지의 스크린샷을 browser_screenshot 도구로 캡처하세요.\n"
        "2. 캡처된 이미지를 분석하여 UI/UX 품질을 평가하세요.\n"
        "3. 레이아웃, 색상, 폰트, 간격, 반응형, 접근성 관점에서 피드백을 제시하세요.\n"
        "4. 개선 사항을 구체적으로 나열하세요."
    )

    tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
    response_text, input_tokens, output_tokens = await _call_anthropic_with_tools(
        tool_model, design_prompt, messages, dsn
    )

    duration_ms = int((time.time() - start) * 1000)

    conn = None
    try:
        conn = await asyncpg.connect(dsn=dsn)
        await _log_agent_execution(
            conn, session_id, "design", "design", user_msg[:200],
            response_text[:500], "success", calc_cost(tool_model, input_tokens, output_tokens),
            duration_ms,
        )
    except Exception:
        pass
    finally:
        if conn:
            await conn.close()

    return response_text, input_tokens, output_tokens


async def _handle_design_fix_intent(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
    session_id: str = "",
) -> Tuple[str, int, int]:
    """디자인 수정: Vision 분석 → Developer Agent 코드 수정."""
    import time
    from app.services.agent_state_builder import build_agent_state

    start = time.time()
    user_msg = messages[-1]["content"] if messages else ""

    # Step 1: Vision 분석 (tool-use)
    analysis_prompt = (
        system_prompt
        + "\n\n[디자인 수정 분석 모드]\n"
        "CEO가 디자인/UI 수정을 요청했습니다.\n"
        "1. 관련 페이지의 스크린샷을 browser_screenshot 도구로 캡처하세요.\n"
        "2. 현재 디자인 문제점을 분석하세요.\n"
        "3. 수정이 필요한 CSS/HTML/컴포넌트를 구체적으로 나열하세요.\n"
        "결과를 JSON으로 요약하세요: {\"issues\": [...], \"fix_targets\": [{\"file\": \"...\", \"change\": \"...\"}]}"
    )

    tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
    analysis_text, in_tok, out_tok = await _call_anthropic_with_tools(
        tool_model, analysis_prompt, messages, dsn
    )

    # Step 2: Developer Agent로 수정 시도
    dev_result_text = ""
    try:
        from app.agents.developer import developer_node
        state = build_agent_state(
            description=f"디자인 수정 요청: {user_msg}\n\n분석 결과:\n{analysis_text[:2000]}",
            success_criteria=["디자인 이슈 수정 코드 생성"],
        )
        dev_result = await developer_node(state)
        files = dev_result.get("generated_files", [])
        if files:
            dev_result_text = f"\n\n**Developer Agent 코드 생성**: {len(files)}개 파일"
            for f in files[:3]:
                path = f.get("path", f.get("name", "unknown"))
                dev_result_text += f"\n  - {path}"
        else:
            dev_result_text = "\n\n**Developer Agent**: 코드 생성 없음 (수동 수정 필요)"
    except Exception as e:
        logger.error(f"developer_node_failed_in_design_fix: {e}")
        dev_result_text = f"\n\n**Developer Agent 오류**: {str(e)[:200]}"

    duration_ms = int((time.time() - start) * 1000)
    response_text = f"[디자인 수정 분석 + 코드 생성] ({duration_ms}ms)\n\n{analysis_text}{dev_result_text}"

    conn = None
    try:
        conn = await asyncpg.connect(dsn=dsn)
        await _log_agent_execution(
            conn, session_id, "design+developer", "design_fix", user_msg[:200],
            response_text[:500], "success",
            calc_cost(tool_model, in_tok, out_tok), duration_ms,
        )
    except Exception:
        pass
    finally:
        if conn:
            await conn.close()

    return response_text, in_tok, out_tok


async def _handle_architect_intent(
    model: str,
    system_prompt: str,
    messages: List[Dict],
    dsn: str,
    session_id: str = "",
) -> Tuple[str, int, int]:
    """설계 검토: Architect Agent로 시스템 설계 JSON 생성."""
    import time
    from app.services.agent_state_builder import build_agent_state

    start = time.time()
    user_msg = messages[-1]["content"] if messages else ""

    state = build_agent_state(
        description=f"CEO 설계 검토 요청: {user_msg}",
        success_criteria=["시스템 설계 JSON 생성", "DB 스키마, API 구조, 파일 구조 포함"],
    )

    try:
        from app.agents.architect_agent import architect_node
        arch_result = await architect_node(state)
        design = arch_result.get("architect_design", {})
        total_cost = arch_result.get("total_cost_usd", state.get("total_cost_usd", 0))

        if design:
            # 설계 JSON을 읽기 좋게 포맷
            design_text = json.dumps(design, ensure_ascii=False, indent=2)
            if len(design_text) > 3000:
                design_text = design_text[:3000] + "\n... (truncated)"
            response_text = f"[Architect Agent 설계 완료] (${total_cost:.4f})\n\n```json\n{design_text}\n```"
        else:
            response_text = "[Architect Agent] 설계 결과 없음"

    except Exception as e:
        logger.error(f"architect_node_failed: {e}")
        response_text = f"[Architect Agent 오류] {str(e)[:300]}"
        total_cost = 0

    duration_ms = int((time.time() - start) * 1000)

    conn = None
    try:
        conn = await asyncpg.connect(dsn=dsn)
        await _log_agent_execution(
            conn, session_id, "architect", "architect", user_msg[:200],
            response_text[:500], "success" if "오류" not in response_text else "error",
            total_cost, duration_ms,
        )
    except Exception:
        pass
    finally:
        if conn:
            await conn.close()

    return response_text, 0, 0


# ─── 세션 요약 생성 ───────────────────────────────────────────────────────
async def generate_session_summary(conn, session_id: str) -> None:
    """Gemini Flash로 세션 요약 생성 후 DB 저장 (비용 최소화)."""
    messages = await conn.fetch(
        "SELECT role, content FROM ceo_chat_messages WHERE session_id = $1 ORDER BY created_at",
        session_id,
    )
    if not messages:
        return

    conversation_text = "\n".join([f"{r['role'].upper()}: {r['content']}" for r in messages])
    prompt = f"""다음 대화를 분석하여 JSON 형식으로 요약하세요:

{conversation_text[:3000]}

응답 형식:
{{
  "summary": "한두 문장 요약",
  "key_decisions": "주요 결정사항 (쉼표 구분)",
  "pending_actions": "미완료 작업 (쉼표 구분)"
}}"""

    try:
        text, _, _ = await call_llm(
            'gemini-2.5-flash',
            '당신은 회의록 요약 전문가입니다.',
            [{"role": "user", "content": prompt}],
        )
        # JSON 추출
        import re
        text_clean = text.strip()
        if text_clean.startswith('```'):
            text_clean = re.sub(r'^```(?:json)?\s*', '', text_clean)
            text_clean = re.sub(r'\s*```$', '', text_clean)
        json_match = re.search(r'\{.*\}', text_clean, re.DOTALL)
        data = json.loads(json_match.group()) if json_match else {
            "summary": text_clean[:200], "key_decisions": "", "pending_actions": ""
        }

        await conn.execute(
            """INSERT INTO ceo_session_summaries (session_id, summary, key_decisions, pending_actions)
               VALUES ($1, $2, $3, $4)""",
            session_id,
            data.get('summary', ''),
            data.get('key_decisions', ''),
            data.get('pending_actions', ''),
        )
        await conn.execute(
            """UPDATE ceo_chat_sessions SET summary = $1, ended_at = now(), status = 'closed'
               WHERE session_id = $2""",
            data.get('summary', ''),
            session_id,
        )
    except Exception as e:
        logger.warning(f"Session summary generation failed: {e}")
        await conn.execute(
            "UPDATE ceo_chat_sessions SET ended_at = now(), status = 'closed' WHERE session_id = $1",
            session_id,
        )


# ─── 엔드포인트 ───────────────────────────────────────────────────────────
@router.post("/ceo-chat/message")
async def send_ceo_message(req: CeoChatRequest):
    """CEO 메시지 전송 → 컨텍스트 빌드 → 모델 분기 → 응답 저장."""
    conn = await _get_conn()
    try:
        session_id = req.session_id
        if session_id == "auto":
            session_id = str(uuid.uuid4())[:16]

        existing = await conn.fetchrow(
            "SELECT session_id FROM ceo_chat_sessions WHERE session_id = $1", session_id
        )
        if not existing:
            await conn.execute(
                "INSERT INTO ceo_chat_sessions (session_id) VALUES ($1)", session_id
            )

        # 컨텍스트 빌드 (AADS-190: memory_recall 통합)
        ctx_mgr = ContextManager(conn)
        system_prompt = await ctx_mgr.build_context(session_id)
        active_tasks = await ctx_mgr.load_active_tasks()

        # AADS-190: 메모리 자동 주입 (5섹션: 세션요약/CEO선호/도구전략/활성Directive/학습사항)
        try:
            from app.core.memory_recall import build_memory_context
            _memory_block = await build_memory_context(session_id=session_id, project_id=None)
            if _memory_block:
                system_prompt += "\n\n" + _memory_block
                logger.debug(f"ceo_chat_memory_injected chars={len(_memory_block)}")
        except Exception as _mem_err:
            logger.warning(f"ceo_chat_memory_injection_failed: {_mem_err}")

        # 모델 선택: CEO가 직접 지정하면 패스스루, "mixture"/None이면 자동 라우팅 (AADS-156)
        if req.model and req.model != "mixture":
            model = req.model
        else:
            model = route_model(req.message)

        # Intent 분류 (AADS-157)
        intent = classify_intent(req.message)
        logger.info(f"ceo_chat_intent intent={intent} model={model} session={session_id}")

        # 이전 메시지 로드 (최근 10턴 = 20 rows)
        prev_msgs = await conn.fetch(
            """SELECT role, content FROM ceo_chat_messages
               WHERE session_id = $1 ORDER BY created_at DESC LIMIT 20""",
            session_id,
        )
        prev_msgs = list(reversed(prev_msgs))
        messages = [{"role": r['role'], "content": r['content']} for r in prev_msgs]
        messages.append({"role": "user", "content": req.message})

        # Intent 기반 LLM 호출 (AADS-157 + AADS-164)
        dsn = settings.DATABASE_URL or settings.SUPABASE_DIRECT_URL
        if intent == "qa":
            # AADS-164: QA Agent + Judge (+ AADS-165 크로스 프로젝트)
            response_text, input_tokens, output_tokens = await _handle_qa_intent(
                model, system_prompt, messages, dsn, session_id
            )
        elif intent == "execution_verify":
            # AADS-165: 실행 검증 지시서 자동 생성
            response_text, input_tokens, output_tokens = await _handle_execution_verify_intent(
                model, system_prompt, messages, dsn, session_id
            )
        elif intent == "design":
            # AADS-164: 스크린샷 + Vision 분석
            response_text, input_tokens, output_tokens = await _handle_design_intent(
                model, system_prompt, messages, dsn, session_id
            )
        elif intent == "design_fix":
            # AADS-164: 디자인 분석 + Developer 코드 수정
            response_text, input_tokens, output_tokens = await _handle_design_fix_intent(
                model, system_prompt, messages, dsn, session_id
            )
        elif intent == "architect":
            # AADS-164: Architect Agent 설계
            response_text, input_tokens, output_tokens = await _handle_architect_intent(
                model, system_prompt, messages, dsn, session_id
            )
        elif intent == "health_check":
            # AADS-166: 헬스체크
            response_text, input_tokens, output_tokens = await _handle_health_check_intent(
                session_id
            )
        elif intent == "dashboard":
            # DashboardCollector + tool-use
            collector = DashboardCollector(conn, dsn)
            dashboard_data = await collector.collect()
            enriched_prompt = _inject_dashboard(system_prompt, dashboard_data)
            tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
            response_text, input_tokens, output_tokens = await _call_anthropic_with_tools(
                tool_model, enriched_prompt, messages, dsn
            )
        elif intent == "browser":
            # 브라우저 자동화 tool-use (AADS-159)
            tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
            response_text, input_tokens, output_tokens = await _call_anthropic_with_tools(
                tool_model, system_prompt, messages, dsn
            )
        elif intent == "pipeline_c":
            # Pipeline C: 자율 작업 파이프라인 (tool-use 활성화)
            pipeline_prompt = (
                system_prompt
                + "\n\n[Pipeline C 모드]\n"
                "CEO가 Claude Code 자율 작업을 요청했습니다.\n"
                "1. 작업 시작: pipeline_c_start 도구를 사용하세요.\n"
                "2. 상태 확인: pipeline_c_status 도구를 사용하세요.\n"
                "3. 승인/거부: pipeline_c_approve 도구를 사용하세요.\n"
                "프로젝트명을 메시지에서 추출하고, 구체적 지시를 instruction에 전달하세요.\n"
                "승인 요청이 오면 변경사항(git diff)을 먼저 확인 후 CEO에게 보고하세요."
            )
            tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
            response_text, input_tokens, output_tokens = await _call_anthropic_with_tools(
                tool_model, pipeline_prompt, messages, dsn, max_iterations=8
            )
        elif intent in ("diagnosis", "research"):
            # tool-use 활성화 (Anthropic 전용)
            tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
            response_text, input_tokens, output_tokens = await _call_anthropic_with_tools(
                tool_model, system_prompt, messages, dsn
            )
        elif intent == "execute":
            # 지시서 자동 생성 + submit
            response_text, input_tokens, output_tokens = await _handle_execute_intent(
                model, system_prompt, messages, dsn
            )
        else:
            # strategy: 현행 유지 (tool-use 없이 대화)
            response_text, input_tokens, output_tokens = await call_llm(model, system_prompt, messages)

        cost = calc_cost(model, input_tokens, output_tokens)

        # 메시지 저장
        await conn.execute(
            """INSERT INTO ceo_chat_messages (session_id, role, content, model_used, input_tokens, output_tokens, cost_usd)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            session_id, 'user', req.message, None, None, None, None,
        )
        await conn.execute(
            """INSERT INTO ceo_chat_messages (session_id, role, content, model_used, input_tokens, output_tokens, cost_usd)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            session_id, 'assistant', response_text, model, input_tokens, output_tokens, cost,
        )

        # 세션 통계 업데이트
        await conn.execute(
            """UPDATE ceo_chat_sessions SET
               total_turns = total_turns + 1,
               total_input_tokens = total_input_tokens + $1,
               total_output_tokens = total_output_tokens + $2,
               total_cost_usd = total_cost_usd + $3
               WHERE session_id = $4""",
            input_tokens, output_tokens, cost, session_id,
        )

        # 10턴마다 자동 요약
        session_row = await conn.fetchrow(
            "SELECT total_turns FROM ceo_chat_sessions WHERE session_id = $1", session_id
        )
        if session_row and session_row['total_turns'] % 10 == 0:
            await generate_session_summary(conn, session_id)

        return {
            "session_id": session_id,
            "response": response_text,
            "model_used": _model_display_name(model),
            "model_id": model,
            "intent": intent,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
            "active_tasks": active_tasks,
        }
    finally:
        await conn.close()


@router.get("/ceo-chat/models")
async def get_supported_models():
    """지원 모델 목록 반환 (AADS-156: 28개)."""
    return {
        "models": SUPPORTED_MODELS,
        "total": len(SUPPORTED_MODELS),
        "by_provider": {
            "anthropic": [m for m in SUPPORTED_MODELS if m["provider"] == "anthropic"],
            "openai":    [m for m in SUPPORTED_MODELS if m["provider"] == "openai"],
            "google":    [m for m in SUPPORTED_MODELS if m["provider"] == "google"],
        },
    }


@router.get("/ceo-chat/sessions")
async def get_ceo_sessions():
    """세션 목록 조회."""
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """SELECT session_id, started_at, summary, total_cost_usd, total_turns, status
               FROM ceo_chat_sessions ORDER BY started_at DESC LIMIT 50"""
        )
        sessions = []
        for r in rows:
            d = dict(r)
            if d.get('started_at'):
                d['started_at'] = d['started_at'].isoformat()
            d['total_cost_usd'] = float(d['total_cost_usd'] or 0)
            sessions.append(d)
        return {"sessions": sessions}
    finally:
        await conn.close()


@router.get("/ceo-chat/sessions/{session_id}")
async def get_ceo_session(session_id: str):
    """특정 세션 메시지 목록."""
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """SELECT role, content, model_used, cost_usd, created_at
               FROM ceo_chat_messages WHERE session_id = $1 ORDER BY created_at""",
            session_id,
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = []
        for r in rows:
            d = dict(r)
            if d.get('created_at'):
                d['created_at'] = d['created_at'].isoformat()
            d['cost_usd'] = float(d['cost_usd'] or 0)
            messages.append(d)
        return {"session_id": session_id, "messages": messages}
    finally:
        await conn.close()


@router.post("/ceo-chat/end-session")
async def end_ceo_session(req: CeoEndSessionRequest):
    """세션 종료 + Gemini Flash로 자동 요약 생성."""
    conn = await _get_conn()
    try:
        existing = await conn.fetchrow(
            "SELECT session_id FROM ceo_chat_sessions WHERE session_id = $1", req.session_id
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Session not found")
        await generate_session_summary(conn, req.session_id)
        return {"status": "ok", "session_id": req.session_id, "message": "Session ended and summarized"}
    finally:
        await conn.close()


@router.get("/ceo-chat/cost-summary")
async def get_ceo_cost_summary():
    """오늘/이번주/이번달 비용 요약 + 모델별 분포."""
    conn = await _get_conn()
    try:
        today_row = await conn.fetchrow(
            """SELECT COUNT(*) AS turns, COALESCE(SUM(cost_usd),0) AS cost
               FROM ceo_chat_messages
               WHERE role='assistant' AND created_at >= date_trunc('day', now())"""
        )
        week_row = await conn.fetchrow(
            """SELECT COUNT(*) AS turns, COALESCE(SUM(cost_usd),0) AS cost
               FROM ceo_chat_messages
               WHERE role='assistant' AND created_at >= date_trunc('week', now())"""
        )
        month_row = await conn.fetchrow(
            """SELECT COUNT(*) AS turns, COALESCE(SUM(cost_usd),0) AS cost
               FROM ceo_chat_messages
               WHERE role='assistant' AND created_at >= date_trunc('month', now())"""
        )

        model_rows = await conn.fetch(
            """SELECT model_used, COALESCE(SUM(cost_usd),0) AS cost
               FROM ceo_chat_messages
               WHERE role='assistant' AND created_at >= date_trunc('month', now())
               GROUP BY model_used"""
        )
        by_model: Dict[str, float] = {}
        for r in model_rows:
            key = r['model_used'] or 'unknown'
            display = _model_display_name(key)
            by_model[display] = float(r['cost'])

        total_month = float(month_row['cost'])
        return {
            "today":      {"turns": int(today_row['turns']), "cost": round(float(today_row['cost']), 4)},
            "this_week":  {"turns": int(week_row['turns']),  "cost": round(float(week_row['cost']), 4)},
            "this_month": {"turns": int(month_row['turns']), "cost": round(total_month, 4)},
            "by_model": {k: round(v, 4) for k, v in by_model.items()},
            "monthly_budget_usd": 63.0,
            "monthly_budget_used_pct": round(total_month / 63.0 * 100, 1),
        }
    finally:
        await conn.close()
