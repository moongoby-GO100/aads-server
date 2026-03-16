"""
AADS-186E-3: 자율 실행 루프 — 복잡한 멀티스텝 작업 수행
MAX_ITERATIONS=60, COST_LIMIT_PER_TASK=$10.0
위험 도구(submit_directive 등) 자동 실행 금지.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)

# 모듈 레벨 임포트 (mock 가능하도록)
try:
    from app.services.model_selector import call_stream
    from app.services.tool_executor import ToolExecutor
    from app.services.intent_router import IntentResult
except ImportError:
    call_stream = None  # type: ignore[assignment]
    ToolExecutor = None  # type: ignore[assignment]
    IntentResult = None  # type: ignore[assignment]


# ─── 상수 ─────────────────────────────────────────────────────────────────────

_MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "25"))  # L1: 환경변수화
_COST_LIMIT_PER_TASK = 10.0  # USD
_DANGEROUS_TOOLS = frozenset({
    "submit_directive", "directive_create",  # 파이프라인 재귀 방지만 유지
})

# M1: LLM 재시도 설정
_LLM_MAX_RETRIES = 3
_LLM_RETRY_BASE_DELAY = 3  # 초 (지수 백오프: 3, 6, 12)


# ─── SSE 이벤트 헬퍼 ──────────────────────────────────────────────────────────

def _sse(event_type: str, payload: Any) -> str:
    """SSE data 라인 생성."""
    if isinstance(payload, str):
        data = {"type": event_type, "content": payload}
    else:
        data = {"type": event_type, **payload}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ─── 비용 계산 ────────────────────────────────────────────────────────────────

_COST_PER_1M: Dict[str, Dict[str, float]] = {
    "claude-opus":   {"in": 5.0,   "out": 25.0},   # Opus 4.6 실제 가격
    "claude-sonnet": {"in": 3.0,   "out": 15.0},
    "claude-haiku":  {"in": 1.0,   "out": 5.0},    # Haiku 4.5 실제 가격
}


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if "opus" in model:
        rates = _COST_PER_1M["claude-opus"]
    elif "haiku" in model:
        rates = _COST_PER_1M["claude-haiku"]
    else:
        rates = _COST_PER_1M["claude-sonnet"]
    return (input_tokens * rates["in"] + output_tokens * rates["out"]) / 1_000_000


# ─── AutonomousExecutor ───────────────────────────────────────────────────────

class AutonomousExecutor:
    """
    자율 도구 루프 실행기.
    복잡한 멀티스텝 작업을 LLM + 도구 루프로 독립 수행.
    SSE 이벤트로 진행 상황 실시간 표시.
    """

    MAX_ITERATIONS: int = _MAX_ITERATIONS
    COST_LIMIT_PER_TASK: float = _COST_LIMIT_PER_TASK

    def __init__(self, max_iterations: Optional[int] = None, cost_limit: Optional[float] = None):
        self.max_iterations = max_iterations if max_iterations is not None else self.MAX_ITERATIONS
        self.cost_limit = cost_limit if cost_limit is not None else self.COST_LIMIT_PER_TASK

    async def execute_task(
        self,
        task_description: str,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        model: str = "claude-sonnet",
        system_prompt: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        자율 도구 루프.

        Args:
            task_description: 수행할 작업 설명
            tools: Anthropic Tool Use 포맷 도구 목록
            messages: 현재까지의 메시지 히스토리
            model: 사용할 모델
            system_prompt: 시스템 프롬프트

        Yields:
            SSE 이벤트 문자열
        """
        # 모듈 레벨에서 임포트한 변수 사용 (mock 가능)
        import app.services.autonomous_executor as _self_module
        _call_stream = _self_module.call_stream
        _ToolExecutor = _self_module.ToolExecutor
        _IntentResult = _self_module.IntentResult

        tool_executor = _ToolExecutor()
        iteration = 0
        total_cost = 0.0
        full_response = ""

        # ContextVar 전파 진단
        from app.services.tool_executor import current_chat_session_id
        _diag_sid = current_chat_session_id.get("")
        logger.info(f"[DIAG] AutonomousExecutor.execute_task: ContextVar session_id='{_diag_sid}'")

        # 작업 시작 메시지 추가
        work_messages = list(messages)
        quality_scores: List[float] = []  # P3: track per-iteration quality for stop check
        if task_description and (not work_messages or work_messages[-1].get("role") != "user"):
            work_messages.append({"role": "user", "content": task_description})

        # IntentResult 생성 (model_selector 호환)
        intent_result = _IntentResult(
            intent="complex_analysis",
            model=model,
            use_tools=bool(tools),
            tool_group="all",
            use_extended_thinking=False,
            use_gemini_direct=False,
        )

        while iteration < self.max_iterations:
            # 비용 상한 체크
            if total_cost >= self.cost_limit:
                yield _sse("cost_limit", {
                    "message": f"비용 한도 도달 (${total_cost:.4f})",
                    "total_cost": total_cost,
                    "iterations": iteration,
                })
                return

            iteration += 1
            iter_response = ""
            iter_tool_calls: List[Dict[str, Any]] = []
            stop_reason = "end_turn"
            iter_input_tokens = 0
            iter_output_tokens = 0

            # M1: LLM 호출 (지수 백오프 재시도)
            _llm_success = False
            _fr_checkpoint = len(full_response)  # C9: retry 시 full_response 롤백 지점
            for _llm_attempt in range(_LLM_MAX_RETRIES):
                try:
                    async for event in _call_stream(
                        intent_result=intent_result,
                        system_prompt=system_prompt or "",
                        messages=work_messages,
                        tools=tools or None,
                        model_override=None,
                    ):
                        etype = event.get("type", "")
                        if etype == "delta":
                            content = event.get("content", "")
                            iter_response += content
                            full_response += content
                            yield _sse("delta", {"content": content, "iteration": iteration})
                        elif etype == "tool_use":
                            tool_call = {
                                "id": event.get("tool_use_id", ""),
                                "name": event.get("tool_name", ""),
                                "input": event.get("tool_input", {}),
                            }
                            iter_tool_calls.append(tool_call)
                            stop_reason = "tool_use"
                            yield _sse("tool_use", {
                                "tool_name": event["tool_name"],
                                "tool_use_id": event.get("tool_use_id", ""),
                                "iteration": iteration,
                            })
                        elif etype == "heartbeat":
                            # 도구 실행 중 heartbeat 전달 — SSE 연결 유지 (AADS 채팅 품질 고도화)
                            yield _sse("heartbeat", {})
                        elif etype == "tool_result":
                            yield _sse("tool_result", {
                                "tool_name": event.get("tool_name", ""),
                                "content": str(event.get("content", ""))[:300],
                                "iteration": iteration,
                            })
                        elif etype == "done":
                            iter_input_tokens = event.get("input_tokens", 0) or 0
                            iter_output_tokens = event.get("output_tokens", 0) or 0
                            stop_reason = event.get("stop_reason", stop_reason)
                        elif etype == "error":
                            yield _sse("error", {"content": event.get("content", "LLM 오류"), "iteration": iteration})
                            return
                    _llm_success = True
                    break
                except Exception as e:
                    if _llm_attempt < _LLM_MAX_RETRIES - 1:
                        _delay = _LLM_RETRY_BASE_DELAY * (2 ** _llm_attempt)
                        logger.warning(f"autonomous_executor LLM retry iter={iteration} attempt={_llm_attempt+1}/{_LLM_MAX_RETRIES} error={e} delay={_delay}s")
                        await asyncio.sleep(_delay)
                        # 재시도 시 iter_response/tool_calls 초기화
                        iter_response = ""
                        iter_tool_calls = []
                        full_response = full_response[:_fr_checkpoint]  # C9: full_response 롤백
                        continue
                    logger.error(f"autonomous_executor LLM error iter={iteration} after {_LLM_MAX_RETRIES} retries: {e}")
                    yield _sse("error", {"content": f"LLM 오류 ({_LLM_MAX_RETRIES}회 재시도 실패): {e}", "iteration": iteration})
                    return
            if not _llm_success:
                return

            # 비용 누적
            iter_cost = _calc_cost(model, iter_input_tokens, iter_output_tokens)
            total_cost += iter_cost

            # P3: 품질 점수 추적 + should_stop_generation 체크
            if iter_response and len(iter_response) >= 100:
                try:
                    from app.services.self_evaluator import evaluate_response, should_stop_generation
                    _eval_score = await evaluate_response(
                        user_message=task_description or "",
                        ai_response=iter_response,
                        message_id=str(uuid.uuid4()),
                        session_id=None,
                    )
                    if _eval_score is not None:
                        quality_scores.append(_eval_score)
                        _should_stop, _stop_reason = should_stop_generation(quality_scores)
                        if _should_stop:
                            logger.warning(
                                f"quality_stop: autonomous_executor iteration {iteration} — {_stop_reason}"
                            )
                            yield _sse("quality_stop", {
                                "message": f"품질 저하로 중단: {_stop_reason}",
                                "scores": quality_scores[-5:],
                                "iterations": iteration,
                                "total_cost": total_cost,
                            })
                            return
                except Exception as _qe:
                    logger.debug(f"autonomous_executor quality check skipped: {_qe}")

            # 도구 사용 없으면 종료
            if stop_reason == "end_turn" or not iter_tool_calls:
                yield _sse("complete", {
                    "content": full_response,
                    "iterations": iteration,
                    "total_cost": total_cost,
                })
                if task_id:
                    try:
                        from app.services.task_logger import emit_task_log, emit_task_completed
                        await emit_task_log(task_id, "info", f"작업 완료 ({iteration}회 반복, ${total_cost:.4f})", "done")
                        await emit_task_completed(task_id, "done", f"{iteration}회 반복 완료")
                    except Exception:
                        pass
                # 에이전트 작업 완료 → 발견사항 메모리 자동 기록 (AADS-186E Task4)
                try:
                    from app.core.memory_recall import save_observation
                    if iteration >= 2 and full_response:
                        _summary = full_response[:200].replace("\n", " ")
                        await save_observation(
                            category="discovery",
                            key=f"agent_task_{uuid.uuid4().hex[:6]}_{iteration}iter",
                            content=f"에이전트 {iteration}회 반복 완료: {_summary}",
                            source="autonomous_executor",
                            confidence=0.4,
                        )
                except Exception as _mem_err:
                    logger.warning(f"autonomous_executor memory save error: {_mem_err}")
                return

            # 도구 실행 (어시스턴트 응답 먼저 추가)
            assistant_content: List[Dict[str, Any]] = []
            if iter_response:
                assistant_content.append({"type": "text", "text": iter_response})
            for tc in iter_tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                })
            work_messages.append({"role": "assistant", "content": assistant_content})

            # 도구 결과 수집
            tool_results: List[Dict[str, Any]] = []
            for tc in iter_tool_calls:
                tool_name = tc["name"]
                tool_input = tc["input"]
                tool_id = tc["id"]

                # 실시간 로그 발행
                if task_id:
                    try:
                        from app.services.task_logger import emit_task_log
                        await emit_task_log(task_id, "command", f"🔍 {tool_name} 실행 중...", f"iteration_{iteration}")
                    except Exception:
                        pass

                # 위험 도구 확인
                if tool_name in _DANGEROUS_TOOLS:
                    yield _sse("confirm_required", {
                        "tool_name": tool_name,
                        "message": f"'{tool_name}' 실행 확인 필요 — 자율 루프에서 자동 실행 금지",
                        "iteration": iteration,
                    })
                    tool_result_content = f"[차단됨] '{tool_name}'은 자율 루프에서 자동 실행 금지. CEO 확인 필요."
                else:
                    try:
                        tool_result_content = await asyncio.wait_for(
                            tool_executor.execute(tool_name, tool_input),
                            timeout=60.0,
                        )
                        yield _sse("tool_result", {
                            "tool_name": tool_name,
                            "tool_use_id": tool_id,
                            "summary": str(tool_result_content)[:300],
                            "iteration": iteration,
                        })
                        # 도구 결과 로그
                        if task_id:
                            try:
                                from app.services.task_logger import emit_task_log
                                await emit_task_log(task_id, "output", f"✅ {tool_name}: {str(tool_result_content)[:200]}", f"iteration_{iteration}")
                            except Exception:
                                pass
                    except asyncio.TimeoutError:
                        tool_result_content = json.dumps({"error": "timeout", "tool": tool_name})
                        logger.warning(f"autonomous_executor tool timeout: {tool_name}")
                    except Exception as e:
                        tool_result_content = json.dumps({"error": str(e), "tool": tool_name})
                        logger.error(f"autonomous_executor tool error: {tool_name}: {e}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": str(tool_result_content),
                })

            # 도구 결과 메시지 추가
            work_messages.append({"role": "user", "content": tool_results})

            # C8: 메시지 히스토리 슬라이딩 윈도우 (처음 2개 + 최근 N개 유지)
            # assistant/tool_result 쌍이 깨지지 않도록 짝수 단위로 자름
            if len(work_messages) > 22:
                keep_recent = 20
                tail = work_messages[-keep_recent:]
                # 첫 메시지가 tool_result(user)면 쌍이 깨진 것 → 1개 더 포함
                if tail and tail[0].get("role") == "user" and isinstance(tail[0].get("content"), list):
                    # tool_result 메시지 — 앞의 assistant 메시지도 포함
                    if keep_recent < len(work_messages) - 2:
                        tail = work_messages[-(keep_recent + 1):]
                work_messages = work_messages[:2] + tail

        # 최대 반복 도달
        yield _sse("max_iterations", {
            "message": f"최대 반복 도달 ({self.max_iterations})",
            "total_cost": total_cost,
            "iterations": iteration,
        })
        # 최대 반복 도달도 메모리에 기록 (AADS-186E Task4)
        try:
            from app.core.memory_recall import save_observation
            await save_observation(
                category="recurring_issue",
                key="agent_max_iterations",
                content=f"에이전트 최대 반복({self.max_iterations}) 도달 — 비용: ${total_cost:.4f}",
                source="autonomous_executor",
                confidence=0.5,
            )
        except Exception:
            pass


# ─── 주간 브리핑 헬퍼 ─────────────────────────────────────────────────────────

async def generate_weekly_briefing() -> str:
    """
    주간 CEO 브리핑 자율 생성.
    6개 프로젝트 변경 분석 + 비용 + 기술부채 → Gemini Flash 종합.
    비용 상한: $0.50 (Gemini Flash 사용).
    """
    from app.services.code_explorer_service import CodeExplorerService
    import os

    sections: List[str] = []

    # 1. 6개 프로젝트 변경 분석
    explorer = CodeExplorerService()
    projects = ["AADS", "KIS", "GO100", "SF", "NTV2", "NAS"]
    change_summaries: List[str] = []
    for proj in projects:
        try:
            report = await asyncio.wait_for(
                explorer.analyze_recent_changes(proj, days=7),
                timeout=30.0,
            )
            if not report.error:
                change_summaries.append(report.summary)
            else:
                change_summaries.append(f"## {proj} — 변경 없음 또는 접근 불가")
        except Exception:
            change_summaries.append(f"## {proj} — 분석 실패")

    sections.append("### 프로젝트 변경 요약\n" + "\n".join(change_summaries))

    # 2. 비용 요약 (최근 7일)
    cost_txt = "비용 조회 불가"
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(cost_usd),0) AS wk_cost,"
                " COUNT(*) AS msg_cnt FROM chat_messages"
                " WHERE created_at > now() - interval '7 days'"
            )
            if row:
                cost_txt = f"7일 비용: ${float(row['wk_cost']):.3f} ({row['msg_cnt']}건)"
    except Exception:
        pass
    sections.append(f"### 비용\n{cost_txt}")

    # 3. 기술부채 요약 (CTO 모드)
    try:
        from app.services.cto_mode import CTOMode
        cto = CTOMode()
        debt_result = await asyncio.wait_for(
            cto.track_tech_debt("AADS"),
            timeout=20.0,
        )
        if debt_result:
            debt_summary = str(debt_result)[:500]
            sections.append(f"### 기술 부채\n{debt_summary}")
    except Exception:
        pass

    # 4. Gemini Flash로 종합
    full_context = "\n\n".join(sections)
    briefing = full_context  # 기본값: 원본 텍스트

    try:
        import httpx
        litellm_url = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
        litellm_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
        async with httpx.AsyncClient(timeout=30.0) as c:
            resp = await c.post(
                f"{litellm_url}/chat/completions",
                headers={"Authorization": f"Bearer {litellm_key}"},
                json={
                    "model": "gemini-flash-lite",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "CEO용 주간 브리핑 보고서 작성자. "
                                "한국어, 간결하게, 핵심만. 마크다운 형식."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"다음 데이터를 바탕으로 주간 CEO 브리핑을 작성하라:\n\n{full_context[:3000]}"
                            ),
                        },
                    ],
                    "max_tokens": 800,
                    "temperature": 0.3,
                },
            )
            if resp.status_code == 200:
                briefing = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"generate_weekly_briefing gemini error: {e}")

    # 5. session_notes에 저장
    try:
        from app.services.memory_manager import get_memory_manager
        from datetime import datetime
        from zoneinfo import ZoneInfo
        mgr = get_memory_manager()
        now_str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
        await mgr.save_note(
            title=f"주간 CEO 브리핑 {now_str}",
            content=briefing[:500],
            category="decision",
        )
    except Exception:
        pass

    return briefing


# ─── 싱글턴 ──────────────────────────────────────────────────────────────────

_executor: Optional[AutonomousExecutor] = None


def get_autonomous_executor() -> AutonomousExecutor:
    """AutonomousExecutor 싱글턴 반환."""
    global _executor
    if _executor is None:
        _executor = AutonomousExecutor()
    return _executor
