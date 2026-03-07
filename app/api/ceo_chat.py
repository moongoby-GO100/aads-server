"""
CEO Chat v2 - 계층 메모리 + 컨텍스트 DB + 모델 분기 엔진
T-073: Context Manager + Model Router + Session Memory
AADS-156: 모델 라우팅 수정 + 전체 지원 모델 업데이트 + 402 fallback
AADS-157: Intent Classifier + DashboardCollector + Tool-use 루프 + Directive Submit

모델 라우터:
  complex  → claude-opus-4-6
  code     → claude-sonnet-4-6
  simple   → gemini-2.5-flash
  default  → claude-sonnet-4-6

Intent Classifier (5분류):
  dashboard  → DashboardCollector + tool-use
  diagnosis  → tool-use 활성화
  research   → tool-use 활성화
  execute    → 지시서 자동 생성 + /directives/submit
  strategy   → 현행 유지 (tool-use 없이 대화)
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


# ─── Intent Classifier (AADS-157) ────────────────────────────────────────
_INTENT_PATTERNS: Dict[str, List[str]] = {
    "dashboard": ["상태", "확인", "보고", "현황", "서버", "대시보드", "요약", "overview"],
    "diagnosis": ["왜", "안돼", "오류", "에러", "문제", "분석", "실패", "죽었", "죽어", "안됨", "error", "fail"],
    "research":  ["검색", "조사", "비교", "찾아", "최신", "찾아봐", "알아봐", "어떤", "무엇"],
    "execute":   ["만들어", "수정해", "고쳐", "배포", "진행", "승인", "작성해", "추가해", "구현", "지시서"],
    "strategy":  ["기획", "방향", "전략", "의도", "검토", "설계", "아키텍처", "계획"],
}


def classify_intent(message: str) -> str:
    """메시지 의도 5분류. 우선순위: execute > dashboard > diagnosis > research > strategy."""
    for intent in ["execute", "dashboard", "diagnosis", "research", "strategy"]:
        if any(kw in message for kw in _INTENT_PATTERNS[intent]):
            return intent
    return "strategy"


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
                logger.info("ceo_chat_tool_call", tool=block.name, params=block.input)
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
                r = await client.get("https://aads.newtalk.kr/api/v1/health")
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

        # 컨텍스트 빌드
        ctx_mgr = ContextManager(conn)
        system_prompt = await ctx_mgr.build_context(session_id)
        active_tasks = await ctx_mgr.load_active_tasks()

        # 모델 선택: CEO가 직접 지정하면 패스스루, "mixture"/None이면 자동 라우팅 (AADS-156)
        if req.model and req.model != "mixture":
            model = req.model
        else:
            model = route_model(req.message)

        # Intent 분류 (AADS-157)
        intent = classify_intent(req.message)
        logger.info("ceo_chat_intent", intent=intent, model=model, session=session_id)

        # 이전 메시지 로드 (최근 10턴 = 20 rows)
        prev_msgs = await conn.fetch(
            """SELECT role, content FROM ceo_chat_messages
               WHERE session_id = $1 ORDER BY created_at DESC LIMIT 20""",
            session_id,
        )
        prev_msgs = list(reversed(prev_msgs))
        messages = [{"role": r['role'], "content": r['content']} for r in prev_msgs]
        messages.append({"role": "user", "content": req.message})

        # Intent 기반 LLM 호출 (AADS-157)
        dsn = settings.DATABASE_URL or settings.SUPABASE_DIRECT_URL
        if intent == "dashboard":
            # DashboardCollector + tool-use
            collector = DashboardCollector(conn, dsn)
            dashboard_data = await collector.collect()
            enriched_prompt = _inject_dashboard(system_prompt, dashboard_data)
            tool_model = model if model.startswith("claude") else "claude-sonnet-4-6"
            response_text, input_tokens, output_tokens = await _call_anthropic_with_tools(
                tool_model, enriched_prompt, messages, dsn
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
