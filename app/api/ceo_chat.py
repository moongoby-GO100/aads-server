"""
CEO Chat v2 - 계층 메모리 + 컨텍스트 DB + 모델 분기 엔진
T-073: Context Manager + Model Router + Session Memory

모델 라우터:
  complex  → claude-opus-4   (claude-opus-4-5)
  code     → claude-sonnet-4 (claude-sonnet-4-5)
  simple   → gemini-2.0-flash
  default  → claude-sonnet-4 (claude-sonnet-4-5)
"""
import json
import uuid
import logging
import asyncpg
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from anthropic import AsyncAnthropic
from app.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()
router = APIRouter()
anthropic_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())


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


# ─── Model Router ────────────────────────────────────────────────────────
def route_model(message: str) -> str:
    """메시지 키워드에 따라 최적 모델 선택 (T-073 지시서 기준)."""
    simple    = ['실행해', '결과', '상태', '확인', '스크린샷', '봐', '알려']
    code      = ['수정해', '만들어', '추가해', '지시서', '코드', '고쳐', '수정']
    complex_kw = ['설계', '분석', '개선안', '보고', '아키텍처', '전략', '검토', '평가']
    if any(p in message for p in complex_kw):
        return 'claude-opus-4-5'
    if any(p in message for p in code):
        return 'claude-sonnet-4-5'
    if any(p in message for p in simple):
        return 'gemini-2.0-flash'
    return 'claude-sonnet-4-5'


# ─── 비용 계산 ────────────────────────────────────────────────────────────
MODEL_PRICING = {
    'claude-opus-4-5':   {'input': 15.0,  'output': 75.0},
    'claude-sonnet-4-5': {'input': 3.0,   'output': 15.0},
    'gemini-2.0-flash':  {'input': 0.075, 'output': 0.30},
    'gemini-1.5-flash':  {'input': 0.075, 'output': 0.30},
}

# 지시서 표시용 모델명
MODEL_DISPLAY = {
    'claude-opus-4-5':   'claude-opus-4',
    'claude-sonnet-4-5': 'claude-sonnet-4',
    'gemini-2.0-flash':  'gemini-2.0-flash',
    'gemini-1.5-flash':  'gemini-2.0-flash',
}


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {'input': 3.0, 'output': 15.0})
    return (input_tokens * pricing['input'] + output_tokens * pricing['output']) / 1_000_000


# ─── LLM 호출 ─────────────────────────────────────────────────────────────
async def call_llm(model: str, system_prompt: str, messages: List[Dict]) -> Tuple[str, int, int]:
    """모델에 따라 적합한 API 호출 → (응답텍스트, input_tokens, output_tokens)"""
    if model.startswith('gemini'):
        return await _call_gemini(model, system_prompt, messages)
    return await _call_anthropic(model, system_prompt, messages)


async def _call_anthropic(model: str, system_prompt: str, messages: List[Dict]) -> Tuple[str, int, int]:
    resp = await anthropic_client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=messages,
    )
    text = resp.content[0].text
    return text, resp.usage.input_tokens, resp.usage.output_tokens


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
        logger.warning(f"Gemini call failed, fallback to Sonnet: {e}")
        return await _call_anthropic('claude-sonnet-4-5', system_prompt, messages)


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
        facts            = await self.load_facts()
        session_summaries = await self.load_session_summary(3)
        active_tasks     = await self.load_active_tasks()
        recent_turns     = await self.load_recent_turns(session_id, 3)

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
            'gemini-2.0-flash',
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

        # 모델 선택: CEO가 직접 지정하면 override, 아니면 자동 라우팅 (T-104)
        MODEL_ID_MAP = {
            "claude-opus-4-6":   "claude-opus-4-5",
            "claude-sonnet-4-6": "claude-sonnet-4-5",
            "gemini-2.0-flash":  "gemini-2.0-flash",
            "gpt-5-mini":        "claude-sonnet-4-5",  # fallback: GPT-5 mini 미지원 시 Sonnet
            "mixture":           None,  # 자동 라우팅
        }
        if req.model and req.model in MODEL_ID_MAP and MODEL_ID_MAP[req.model] is not None:
            model = MODEL_ID_MAP[req.model]
        else:
            model = route_model(req.message)

        # 이전 메시지 로드 (최근 10턴 = 20 rows)
        prev_msgs = await conn.fetch(
            """SELECT role, content FROM ceo_chat_messages
               WHERE session_id = $1 ORDER BY created_at DESC LIMIT 20""",
            session_id,
        )
        prev_msgs = list(reversed(prev_msgs))
        messages = [{"role": r['role'], "content": r['content']} for r in prev_msgs]
        messages.append({"role": "user", "content": req.message})

        # LLM 호출
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
            "model_used": MODEL_DISPLAY.get(model, model),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
            "active_tasks": active_tasks,
        }
    finally:
        await conn.close()


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
            display = MODEL_DISPLAY.get(key, key)
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
