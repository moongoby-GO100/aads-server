"""
AADS-185: 모델 선택기 — LiteLLM vs 직접 Anthropic SDK 분기
- Gemini 인텐트 (casual, greeting): LiteLLM 경유
- Claude 인텐트: Anthropic SDK 직접 (Tool Use + Extended Thinking + Prompt Caching 지원)
- Gemini Direct (grounding, deep_research): gemini_search_service / gemini_research_service
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any, AsyncGenerator, Dict, List, Optional

import time as _time_mod

import httpx
from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError, RateLimitError
from app.config import Settings
from app.services.intent_router import IntentResult

logger = logging.getLogger(__name__)

settings = Settings()
_anthropic = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")

# AADS-186E-2: Extended Thinking 전역 스위치 (기본 활성화)
_EXTENDED_THINKING_ENABLED = os.getenv("EXTENDED_THINKING_ENABLED", "true").lower() == "true"

# 모델별 비용 (per 1M tokens, USD)
_COST_MAP = {
    "claude-opus":            (5.0,  25.0),   # Opus 4.6 실제 가격
    "claude-sonnet":          (3.0,  15.0),
    "claude-haiku":           (1.0,   5.0),   # Haiku 4.5 실제 가격
    "gemini-flash":           (0.075, 0.3),
    "gemini-flash-lite":      (0.01,  0.04),
    "gemini-pro":             (1.25,  5.0),
    "gemini-3-flash-preview": (0.1,   0.4),
}

# LiteLLM alias → Anthropic model ID
_ANTHROPIC_MODEL_ID = {
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-opus":   "claude-opus-4-6",
    "claude-haiku":  "claude-haiku-4-5-20251001",
}

# Gemini 모델 (LiteLLM 경유)
_GEMINI_MODELS = {"gemini-flash", "gemini-flash-lite", "gemini-pro", "gemini-3-flash-preview"}

# Gemini Thinking 모델 — reasoning_effort=low + 높은 max_tokens 필요
_GEMINI_THINKING_MODELS = {"gemini-pro", "gemini-flash", "gemini-3-flash-preview"}


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> Decimal:
    in_rate, out_rate = _COST_MAP.get(model, (3.0, 15.0))
    return Decimal(str(round(in_tokens * in_rate / 1_000_000 + out_tokens * out_rate / 1_000_000, 6)))


async def call_stream(
    intent_result: IntentResult,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model_override: Optional[str] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    인텐트 결과에 따라 LiteLLM 또는 Anthropic SDK로 SSE 스트리밍.

    Yields dict with keys:
      type: 'delta' | 'thinking' | 'tool_use' | 'tool_result' | 'done' | 'error'
      content: str (delta/error)
      thinking: str (thinking delta)
      tool_name: str
      tool_input: dict
      tool_use_id: str
      model: str
      cost: str
      input_tokens: int
      output_tokens: int
    """
    model = model_override or intent_result.model

    # Gemini 모델 → LiteLLM 경유 (실패 시 Claude Haiku 폴백)
    if model in _GEMINI_MODELS:
        _had_error = False
        async for event in _stream_litellm(model, system_prompt, messages):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"gemini_fallback: {model} failed, falling back to claude-haiku")
                break
            yield event
        if _had_error:
            # Gemini 실패 → Claude Haiku로 폴백 (가장 저렴한 Claude)
            _fallback_intent = IntentResult(
                intent=intent_result.intent,
                model="claude-haiku",
                use_tools=intent_result.use_tools,
                tool_group=intent_result.tool_group,
            )
            yield {"type": "delta", "content": ""}  # 스트림 리셋
            async for event in _stream_anthropic(_fallback_intent, "claude-haiku", system_prompt, messages, tools, session_id=session_id):
                yield event
        return

    # Claude 모델 → Anthropic SDK 직접 (3회 재시도 후에도 실패 시 Gemini Flash 폴백)
    _had_error = False
    _error_content = ""
    async for event in _stream_anthropic(intent_result, model, system_prompt, messages, tools, session_id=session_id):
        if event.get("type") == "error":
            _had_error = True
            _error_content = event.get("content", "")
            logger.error(f"claude_fallback_to_gemini: {model} failed after retries ({_error_content[:200]})")
            break
        yield event
    if _had_error:
        # Claude 3회 재시도 실패 → Gemini로 폴백 (도구 포함)
        _fallback_prompt = system_prompt + "\n\n[SYSTEM] Claude API 장애로 Gemini로 전환되었습니다. 동일한 도구를 사용할 수 있습니다. 정확한 데이터 기반으로 답변하세요."
        yield {"type": "delta", "content": f"[Claude 일시 장애 → Gemini 전환]\n\n"}
        # error_log에 기록
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO error_log (error_type, source, server, message, stack_trace, created_at) VALUES ($1, $2, $3, $4, $5, NOW())",
                    "claude_api_fallback", "model_selector", "aads-server",
                    f"Claude {model} → Gemini 전환 (3회 재시도 실패)",
                    _error_content[:500],
                )
        except Exception as _log_err:
            logger.warning(f"error_log insert failed: {_log_err}")
        # Gemini도 도구 사용 가능 — LiteLLM function calling 지원
        async for event in _stream_litellm("gemini-3-flash-preview", _fallback_prompt, messages, tools=tools):
            yield event


def _convert_content_for_openai(content: Any) -> Any:
    """Anthropic content 블록 배열을 OpenAI/LiteLLM 포맷으로 변환."""
    if not isinstance(content, list):
        return content
    result = []
    for block in content:
        if not isinstance(block, dict):
            result.append({"type": "text", "text": str(block)})
        elif block.get("type") == "text":
            result.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                data_url = f"data:{source['media_type']};base64,{source['data']}"
                result.append({"type": "image_url", "image_url": {"url": data_url}})
    return result if len(result) != 1 or result[0].get("type") != "text" else result[0]["text"]


async def _stream_litellm(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """LiteLLM 프록시를 통한 스트리밍 (Gemini function calling 지원)."""
    # messages에서 기존 system role 제거 후 새 system 프롬프트 추가
    clean_msgs = [m for m in messages if m.get("role") != "system"]
    # Anthropic content 블록 → OpenAI 포맷 변환 (이미지 포함 시)
    clean_msgs = [
        {**m, "content": _convert_content_for_openai(m["content"])} for m in clean_msgs
    ]
    msgs = [{"role": "system", "content": system_prompt}] + clean_msgs

    full_text = ""
    input_tokens = 0
    output_tokens = 0

    # Thinking 모델: reasoning_effort=low로 사고 토큰 절감 + max_tokens 확대
    is_thinking = model in _GEMINI_THINKING_MODELS
    max_tokens = 32768 if is_thinking else 16384
    extra_params: Dict[str, Any] = {}
    if is_thinking:
        extra_params["reasoning_effort"] = "low"

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            req_body: Dict[str, Any] = {
                "model": model,
                "messages": msgs,
                "max_tokens": max_tokens,
                "stream": True,
                **extra_params,
            }
            # Gemini function calling — OpenAI 포맷 tools 전달
            if tools:
                _oai_tools = []
                for t in tools:
                    if t.get("input_schema"):
                        _oai_tools.append({
                            "type": "function",
                            "function": {
                                "name": t["name"],
                                "description": t.get("description", ""),
                                "parameters": t["input_schema"],
                            }
                        })
                if _oai_tools:
                    req_body["tools"] = _oai_tools
            async with client.stream(
                "POST",
                f"{LITELLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
                json=req_body,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {"type": "error", "content": f"LiteLLM error {resp.status_code}: {body.decode()[:200]}"}
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except Exception:
                        continue

                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason", "")

                    text = delta.get("content", "")
                    if text:
                        full_text += text
                        yield {"type": "delta", "content": text}

                    # Gemini function call 응답 처리
                    _tool_calls = delta.get("tool_calls", [])
                    for _tc in _tool_calls:
                        _fn = _tc.get("function", {})
                        _fn_name = _fn.get("name", "")
                        _fn_args = _fn.get("arguments", "{}")
                        if _fn_name:
                            try:
                                import json as _j
                                _args = _j.loads(_fn_args) if isinstance(_fn_args, str) else _fn_args
                                # 실제 도구 실행
                                from app.api.ceo_chat_tools import execute_tool as _exec_tool
                                _tool_result = await _exec_tool(_fn_name, _args, "", "")
                                yield {"type": "tool_use", "tool_name": _fn_name, "tool_use_id": _tc.get("id", ""), "tool_input": _args}
                                yield {"type": "tool_result", "tool_name": _fn_name, "content": str(_tool_result)[:3000]}
                                # 도구 결과를 텍스트로 AI에게 다시 전달하지 않음 (단일 턴)
                                # 대신 결과를 직접 yield하여 프론트에 표시
                            except Exception as _te:
                                logger.warning(f"gemini_tool_call_error: {_fn_name}: {_te}")
                                yield {"type": "delta", "content": f"\n[도구 {_fn_name} 실행 실패: {str(_te)[:100]}]\n"}

                    # 토큰 집계 (usage 포함 시)
                    usage = chunk.get("usage", {})
                    if usage:
                        input_tokens = usage.get("prompt_tokens", input_tokens)
                        output_tokens = usage.get("completion_tokens", output_tokens)

    except Exception as e:
        logger.error(f"model_selector litellm error: {e}")
        yield {"type": "error", "content": str(e)}
        return

    cost = _estimate_cost(model, input_tokens, output_tokens)
    yield {
        "type": "done",
        "model": model,
        "cost": str(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _trim_tool_loop_context(
    current_messages: list,
    current_turn: int,
    max_budget: int = 120_000,
) -> list:
    """도구 루프 중 컨텍스트 토큰 예산 관리.

    CEO 메시지(user role)는 절대 삭제하지 않음.
    도구 결과는 F5(tool_archive)에 원본 보관되므로 축소 안전.

    단계별 축소:
    - > max_budget(120K): 10턴 이전 tool_result를 1줄 요약으로 교체
    - > max_budget+30K(150K): 15턴 이전 assistant 500자 절삭
    - > max_budget+50K(170K): 최근 20턴만 유지, 나머지 드롭 + 요약 메시지
    """
    # Estimate total tokens in current_messages
    total_text = ""
    for m in current_messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_text += str(block.get("content", "")) + str(block.get("text", ""))
                else:
                    total_text += str(block)
        elif isinstance(content, str):
            total_text += content

    est_tokens = len(total_text.encode("utf-8")) // 3

    if est_tokens <= max_budget:
        return current_messages

    logger.warning(f"trim_tool_loop: {est_tokens:,} tokens > {max_budget:,}, trimming (turn={current_turn})")

    result = list(current_messages)

    # Phase 1: > 120K — Replace old tool_results with placeholders
    if est_tokens > max_budget:
        cutoff = max(0, len(result) - current_turn * 2 - 20)  # ~10 turns back roughly
        for i in range(cutoff):
            m = result[i]
            content = m.get("content", "")
            if m.get("role") == "user" and isinstance(content, list):
                # tool_result blocks
                new_blocks = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        new_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "[이전 도구 결과 축소됨. 원본은 tool_archive에 보관. 필요시 도구 재호출.]",
                        })
                    else:
                        new_blocks.append(block)
                result[i] = {**m, "content": new_blocks}
            elif m.get("role") == "user" and isinstance(content, str) and "[시스템 도구 조회 결과" in content:
                # Compressed tool result string
                result[i] = {**m, "content": "[이전 도구 결과 축소됨. 필요시 재호출.]"}

    # Re-estimate after Phase 1
    total_text2 = ""
    for m in result:
        c = m.get("content", "")
        if isinstance(c, list):
            for b in c:
                total_text2 += str(b.get("content", "")) + str(b.get("text", "")) if isinstance(b, dict) else str(b)
        elif isinstance(c, str):
            total_text2 += c
    est2 = len(total_text2.encode("utf-8")) // 3

    # Phase 2: > 150K — Truncate old assistant messages
    if est2 > max_budget + 30_000:
        cutoff2 = max(0, len(result) - 30)  # Keep last 15 turns (30 messages)
        for i in range(cutoff2):
            m = result[i]
            if m.get("role") == "assistant":
                content = m.get("content", "")
                if isinstance(content, str) and len(content) > 500:
                    result[i] = {**m, "content": content[:500] + "\n\n[...응답 축소됨...]"}
                elif isinstance(content, list):
                    new_blocks = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if len(text) > 500:
                                new_blocks.append({**block, "text": text[:500] + "\n\n[...응답 축소됨...]"})
                            else:
                                new_blocks.append(block)
                        else:
                            new_blocks.append(block)
                    result[i] = {**m, "content": new_blocks}

    # Phase 3: > 170K — Keep only last 20 turns
    total_text3 = ""
    for m in result:
        c = m.get("content", "")
        if isinstance(c, list):
            for b in c:
                total_text3 += str(b.get("content", "")) + str(b.get("text", "")) if isinstance(b, dict) else str(b)
        elif isinstance(c, str):
            total_text3 += c
    est3 = len(total_text3.encode("utf-8")) // 3

    if est3 > max_budget + 50_000:
        keep_count = 40  # ~20 turns
        if len(result) > keep_count:
            dropped = len(result) - keep_count
            summary_msg = {
                "role": "user",
                "content": f"[이전 {dropped}개 메시지 생략됨. 핵심 사항은 memory_facts에 보존됨. 최근 대화만 표시됩니다.]",
            }
            result = [summary_msg] + result[-keep_count:]
            logger.warning(f"trim_tool_loop_emergency: dropped {dropped} messages, keeping {keep_count}")

    return result


async def _stream_anthropic(
    intent_result: IntentResult,
    model_alias: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Anthropic SDK 직접 스트리밍 (Tool Use + Extended Thinking + Prompt Caching)."""
    model_id = _ANTHROPIC_MODEL_ID.get(model_alias, "claude-sonnet-4-6")

    # AADS-186E-2: Extended Thinking — Opus/Sonnet 4.6 Adaptive Thinking
    use_thinking = (
        _EXTENDED_THINKING_ENABLED
        and intent_result.use_extended_thinking
        and model_alias in ("claude-opus", "claude-sonnet")
    )
    max_tokens = 128000 if use_thinking else 16384

    # 시스템 프롬프트 (Prompt Caching: Layer 1 정적 부분에 cache_control)
    system_blocks = _build_system_with_cache(system_prompt)

    # Adaptive Thinking (4.6 모델 권장) — 모델이 자동으로 사고 깊이 결정
    thinking_config = None
    _output_config = None
    if use_thinking:
        thinking_config = {"type": "adaptive"}
        _output_config = {"effort": "high"}  # low/medium/high/max

    full_text = ""
    thinking_text = ""
    input_tokens = 0
    output_tokens = 0

    # Tool Use 루프 — 절대 상한 + wall-clock 타임아웃
    _MAX_TOOL_TURNS = int(os.getenv("MAX_TOOL_TURNS", "500"))
    _TOOL_TURN_EXTEND = 50  # 자동 연장 턴
    _WALL_CLOCK_TIMEOUT = int(os.getenv("TOOL_LOOP_TIMEOUT_SEC", "1800"))  # 30분 절대 타임아웃
    _wall_clock_start = __import__("time").monotonic()
    # 방어: messages에서 role="system" 필터링 — Claude API는 system을 top-level 파라미터로만 허용
    current_messages = [m for m in messages if m.get("role") != "system"]
    # 방어: 마지막 메시지가 user가 아니면 Claude API "must end with user message" 에러 방지
    if current_messages and current_messages[-1].get("role") != "user":
        current_messages.append({"role": "user", "content": "계속해주세요."})
    tool_calls_made = []
    _consecutive_yellow = 0  # Yellow 등급 도구 연속 호출 카운터
    _consecutive_errors = 0  # 도구 연속 에러 카운터
    _total_errors = 0  # 도구 총 에러 수
    _same_tool_error_count: Dict[str, int] = {}  # 도구별 에러 횟수 (같은 도구 반복 실패 감지)
    # Green(읽기) 도구: 관대한 제한 — 대용량 파일 탐색 시 연속 호출 정상
    _GREEN_TOOLS = {
        "read_remote_file", "list_remote_dir", "read_github_file", "read_task_logs",
        "query_database", "query_project_database", "recall_notes", "search_chat_history",
        "check_task_status", "pipeline_c_status", "dashboard_query", "capture_screenshot",
    }
    _CONSECUTIVE_ERROR_WARN = 10   # 연속 10회 에러 시 경고 주입 (차단 없음)
    _CONSECUTIVE_ERROR_STOP = 999  # 사실상 비활성 — 연속 에러로 중단하지 않음
    _TOTAL_ERROR_STOP = 999        # 사실상 비활성 — 총 에러로 중단하지 않음
    _SAME_TOOL_ERROR_LIMIT = 10    # Yellow 도구: 10회 연속 에러 시 경고 (차단 아님)
    _SAME_TOOL_ERROR_LIMIT_GREEN = 999  # Green 도구: 제한 없음
    _YELLOW_TOOLS = {
        "write_remote_file", "patch_remote_file", "run_remote_command",
        "git_remote_add", "git_remote_commit", "git_remote_push",
        "git_remote_create_branch", "deep_crawl", "deep_research",
        "spawn_subagent", "spawn_parallel_subagents",
    }
    _YELLOW_CONSECUTIVE_LIMIT = 100  # 전체 턴 상한(100)과 동일 — 사실상 무제한
    _effective_max_turns = _MAX_TOOL_TURNS
    _turn = 0

    while _turn < _effective_max_turns:
        # Wall-clock 타임아웃 체크 (30분 기본)
        _elapsed = __import__("time").monotonic() - _wall_clock_start
        if _elapsed > _WALL_CLOCK_TIMEOUT:
            logger.warning(f"wall_clock_timeout: {_elapsed:.0f}s > {_WALL_CLOCK_TIMEOUT}s, forcing stop at turn {_turn}")
            yield {"type": "timeout", "content": f"⏱ 응답 시간 초과 ({_elapsed/60:.0f}분). 현재까지 결과를 반환합니다."}
            break

        api_kwargs: Dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": current_messages,
        }
        if tools:
            # 도구 정의 캐싱 (AADS-190: Prompt Caching 적용)
            try:
                from app.core.cache_config import build_cached_tools
                api_kwargs["tools"] = build_cached_tools(tools)
            except Exception:
                api_kwargs["tools"] = tools
            # Layer ③: 인텐트별 동적 tool_choice (AADS-188C Phase 3 + Priority)
            # force_any: 반드시 도구 호출해야 하는 인텐트 (데이터 조회 필수)
            # auto: 도구 사용 여부를 AI가 판단 (대부분 인텐트)
            # 생략: 도구 불필요 인텐트
            _intent = intent_result.intent
            if _turn == 0:
                _force_tool_intents = (
                    # Tier 1: 데이터 조회 필수
                    "status_check", "dashboard", "task_query", "task_history",
                    "health_check", "all_service_status", "cost_report",
                    # Tier 2: 분석 필수
                    "service_inspection", "code_explorer", "analyze_changes",
                    # Tier 4: 검색 필수
                    "search", "url_read",
                    # Tier 6: 브라우저 명시 요청
                    "browser",
                )
                _no_tool_intents = ("greeting", "casual")
                _auto_tool_intents = (
                    # AI가 도구 필요 여부 판단하는 인텐트
                    "cto_code_analysis", "cto_strategy", "cto_verify", "cto_impact",
                    "code_task", "directive", "directive_gen", "cto_directive",
                    "complex_analysis", "strategy", "planning", "decision",
                    "architect", "design", "design_fix", "cto_tech_debt",
                    "execute", "code_modify", "server_file",
                )
                if _intent in _force_tool_intents and intent_result.use_tools:
                    api_kwargs["tool_choice"] = {"type": "any"}
                elif _intent in _no_tool_intents:
                    pass  # tool_choice 생략
                elif _intent in _auto_tool_intents and intent_result.use_tools:
                    api_kwargs["tool_choice"] = {"type": "auto"}
                elif intent_result.use_tools:
                    api_kwargs["tool_choice"] = {"type": "auto"}
        # Beta features — extra_headers로 전달 (SDK 0.84+에서 betas 직접 파라미터 미지원)
        _betas = []
        if thinking_config:
            api_kwargs["thinking"] = thinking_config
            if _output_config:
                api_kwargs["output_config"] = _output_config
            _betas.append("interleaved-thinking-2025-05-14")
        if _betas:
            api_kwargs["extra_headers"] = {
                **(api_kwargs.get("extra_headers") or {}),
                "anthropic-beta": ",".join(_betas),
            }
        # Extended Thinking + tool_choice="any" 비호환 — auto로 복귀
        if thinking_config and "tool_choice" in api_kwargs:
            del api_kwargs["tool_choice"]

        # 디버그: API 호출 전 payload 요약 로깅
        _n_msgs = len(api_kwargs.get("messages", []))
        _n_tools = len(api_kwargs.get("tools", []) or [])
        _has_thinking = "thinking" in api_kwargs
        _tool_choice = api_kwargs.get("tool_choice", {}).get("type", "none")
        if _turn == 0:
            logger.info(f"anthropic_api_call: model={model_id} msgs={_n_msgs} tools={_n_tools} thinking={_has_thinking} tool_choice={_tool_choice} turn={_turn}")
            # 메시지 role 시퀀스 검증
            _roles = [m.get("role") for m in api_kwargs.get("messages", [])]
            if _roles:
                logger.info(f"anthropic_msg_roles: {_roles[:20]}{'...' if len(_roles)>20 else ''}")
                # content 타입 검증
                for _mi, _m in enumerate(api_kwargs.get("messages", [])[:5]):
                    _ct = type(_m.get("content")).__name__
                    _preview = str(_m.get("content", ""))[:80] if isinstance(_m.get("content"), str) else f"[{_ct}] len={len(_m.get('content', []))}" if isinstance(_m.get("content"), list) else str(type(_m.get("content")))
                    logger.info(f"anthropic_msg[{_mi}]: role={_m.get('role')} content_type={_ct} preview={_preview}")

        # 재시도 로직: 일시적 에러(429/529/503/네트워크)는 최대 3회 재시도
        _RETRYABLE_STATUS = {429, 503, 529}
        _MAX_RETRIES = 3
        _retry_attempt = 0
        _last_error = None
        final_msg = None

        while _retry_attempt <= _MAX_RETRIES:
            try:
                async with _anthropic.messages.stream(**api_kwargs) as stream:
                    async for event in stream:
                        event_type = type(event).__name__

                        if event_type == "RawContentBlockDeltaEvent":
                            delta = event.delta
                            delta_type = type(delta).__name__
                            if delta_type == "TextDelta":
                                full_text += delta.text
                                yield {"type": "delta", "content": delta.text}
                            elif delta_type == "ThinkingDelta":
                                thinking_text += delta.thinking
                                yield {"type": "thinking", "thinking": delta.thinking}
                            elif delta_type == "InputJsonDelta":
                                pass  # tool_use 입력 JSON 누적 (스트림 종료 후 처리)

                    final_msg = await stream.get_final_message()
                break  # 성공 → 재시도 루프 탈출

            except (RateLimitError, APIConnectionError) as e:
                _retry_attempt += 1
                _last_error = e
                _status = getattr(e, 'status_code', 0)
                if _retry_attempt <= _MAX_RETRIES:
                    _wait = min(2 ** _retry_attempt, 10)  # 2, 4, 8초 (최대 10초)
                    logger.warning(f"claude_retry: attempt {_retry_attempt}/{_MAX_RETRIES}, status={_status}, wait={_wait}s, error={str(e)[:100]}")
                    yield {"type": "heartbeat"}  # SSE 연결 유지
                    await asyncio.sleep(_wait)
                else:
                    logger.error(f"claude_retry_exhausted: {_MAX_RETRIES} retries failed, status={_status}, error={str(e)[:100]}")
                    yield {"type": "error", "content": str(e)}
                    return

            except APIStatusError as e:
                _retry_attempt += 1
                _last_error = e
                _status = getattr(e, 'status_code', 0)
                if _status in _RETRYABLE_STATUS and _retry_attempt <= _MAX_RETRIES:
                    _wait = min(2 ** _retry_attempt, 10)
                    logger.warning(f"claude_retry: attempt {_retry_attempt}/{_MAX_RETRIES}, status={_status}, wait={_wait}s, error={str(e)[:100]}")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                else:
                    # 영구적 에러 (400, 401 등) 또는 재시도 소진
                    _err_body = getattr(e, 'body', None) or getattr(e, 'response', None)
                    logger.error(f"model_selector anthropic error: status={_status}, error={e}, body={str(_err_body)[:500]}, model={model_id}, msgs={len(current_messages)}, tools={len(tools or [])}")
                    yield {"type": "error", "content": str(e)}
                    return

            except Exception as e:
                _retry_attempt += 1
                _last_error = e
                if _retry_attempt <= _MAX_RETRIES:
                    _wait = min(2 ** _retry_attempt, 10)
                    logger.warning(f"claude_retry_unexpected: attempt {_retry_attempt}/{_MAX_RETRIES}, wait={_wait}s, error={str(e)[:80]}")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                    full_text = ""  # 부분 응답 리셋
                else:
                    logger.error(f"model_selector anthropic unexpected after {_MAX_RETRIES} retries: {e}")
                    yield {"type": "error", "content": str(e)}
                    return

        if final_msg is None:
            logger.error(f"model_selector: final_msg is None after retries, last_error={_last_error}")
            yield {"type": "error", "content": f"Claude API 응답 없음 (재시도 {_MAX_RETRIES}회 소진)"}
            return

        input_tokens += final_msg.usage.input_tokens
        output_tokens += final_msg.usage.output_tokens
        # Prompt Caching 히트율 로깅
        _cache_read = getattr(final_msg.usage, 'cache_read_input_tokens', 0) or 0
        _cache_create = getattr(final_msg.usage, 'cache_creation_input_tokens', 0) or 0
        if _cache_read or _cache_create:
            logger.info(f"prompt_cache: read={_cache_read} create={_cache_create} input={input_tokens} turn={_turn}")

        # Tool Use 처리
        tool_use_blocks = [b for b in final_msg.content if b.type == "tool_use"]
        if not tool_use_blocks:
            # 빈 응답 자동 재시도: 도구가 있는데 사용 안 하고 너무 짧은 응답 → 도구 강제 호출
            if _turn == 0 and tools and len(full_text) < 100 and intent_result.use_tools:
                logger.warning(f"empty_response_retry: '{full_text[:50]}' ({len(full_text)} chars), forcing tool_choice=any")
                # 응답 리셋 후 tool_choice=any로 재시도
                full_text = ""
                yield {"type": "delta", "content": ""}  # 프론트 스트림 리셋 신호
                api_kwargs["tool_choice"] = {"type": "any"}
                if "thinking" in api_kwargs:
                    del api_kwargs["tool_choice"]  # thinking과 tool_choice=any 비호환
                _turn += 1
                continue  # while 루프 재시도
            break  # 정상 종료

        # 도구 실행 (heartbeat 포함 — SSE 연결 유지)
        from app.services.tool_executor import ToolExecutor, current_chat_session_id as _cv_sid
        executor = ToolExecutor()
        if any(tu.name.startswith("pipeline_c") for tu in tool_use_blocks):
            logger.info(f"[DIAG] call_stream tool exec: ContextVar session_id='{_cv_sid.get('')}'")

        tool_results = []
        for tu in tool_use_blocks:
            # Yellow 도구 연속 실행 제한 체크
            if tu.name in _YELLOW_TOOLS:
                _consecutive_yellow += 1
            else:
                _consecutive_yellow = 0

            if _consecutive_yellow >= _YELLOW_CONSECUTIVE_LIMIT:
                logger.warning(f"yellow_consecutive_limit: {_consecutive_yellow} calls, pausing for CEO confirm")
                yield {
                    "type": "yellow_limit",
                    "content": f"쓰기 도구가 연속 {_consecutive_yellow}회 호출되었습니다. 계속 진행할까요?",
                    "tool_name": tu.name,
                    "consecutive_count": _consecutive_yellow,
                }
                # 제한 리셋 (경고 후 계속 진행 — 프론트에서 차단 UI 표시)
                _consecutive_yellow = 0

            yield {
                "type": "tool_use",
                "tool_name": tu.name,
                "tool_input": tu.input,
                "tool_use_id": tu.id,
            }
            tool_calls_made.append(tu.name)

            # 도구 실행 — 별도 asyncio.Task + Event 기반 heartbeat (P0-FIX: shield→Event 전환)
            # asyncio.shield 패턴 대신 Event + done_callback으로 도구 블로킹과 heartbeat 완전 분리
            _LONG_TOOLS = {"deep_research", "deep_crawl", "spawn_subagent", "spawn_parallel_subagents", "pipeline_c_execute"}
            _tool_timeout = 600 if tu.name in _LONG_TOOLS else 120
            _HB_TOOL_SEC = 8.0  # heartbeat 간격 (초)
            task = asyncio.create_task(executor.execute(tu.name, tu.input))
            _tool_start = __import__("time").monotonic()

            # Event 기반: 도구 완료 시 콜백으로 이벤트 설정 → heartbeat 루프 즉시 탈출
            _tool_done_evt = asyncio.Event()
            task.add_done_callback(lambda _: _tool_done_evt.set())

            while not _tool_done_evt.is_set():
                try:
                    await asyncio.wait_for(_tool_done_evt.wait(), timeout=_HB_TOOL_SEC)
                    break  # 도구 완료 신호 수신
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    _elapsed = __import__("time").monotonic() - _tool_start
                    if _elapsed > _tool_timeout:
                        task.cancel()
                        logger.warning(f"tool_timeout: {tu.name} exceeded {_tool_timeout}s, cancelled")
                        break
                except (asyncio.CancelledError, Exception):
                    break
            try:
                result_str = task.result() if task.done() and not task.cancelled() else json.dumps({"error": f"tool execution timeout ({_tool_timeout}s)", "tool": tu.name})
            except asyncio.CancelledError:
                result_str = json.dumps({"error": f"tool execution timeout ({_tool_timeout}s)", "tool": tu.name})
            except Exception as exc:
                logger.warning(f"tool execution error: tool={tu.name} error={exc}")
                result_str = json.dumps({"error": str(exc), "tool": tu.name})

            # 도구 결과 자동 압축 (컨텍스트에 넣기 전)
            try:
                from app.services.context_compressor import compress_tool_output
                compressed_str = compress_tool_output(tu.name, result_str)
            except Exception:
                compressed_str = result_str  # fallback: 원본 유지

            # 도구 에러 감지 — Green/Yellow 구분 카운팅
            _is_green = tu.name in _GREEN_TOOLS
            _is_tool_error = (
                (isinstance(result_str, str) and ("[ERROR]" in result_str or '"error"' in result_str[:50]))
                or (isinstance(result_str, dict) and "error" in result_str)
            )
            if _is_tool_error:
                _consecutive_errors += 1
                _total_errors += 1
                _same_tool_error_count[tu.name] = _same_tool_error_count.get(tu.name, 0) + 1
            else:
                _consecutive_errors = 0  # 성공하면 연속 에러 리셋
                _same_tool_error_count[tu.name] = 0  # 해당 도구 에러 카운트 리셋

            yield {
                "type": "tool_result",
                "tool_name": tu.name,
                "tool_use_id": tu.id,
                "content": result_str,  # 프론트에는 원본 전달
            }
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": compressed_str,  # 컨텍스트에는 압축본
            })

            # 같은 도구 반복 실패 감지 — 해당 도구만 차단 메시지 주입
            _same_err = _same_tool_error_count.get(tu.name, 0)
            _same_limit = _SAME_TOOL_ERROR_LIMIT_GREEN if _is_green else _SAME_TOOL_ERROR_LIMIT
            if _same_err >= _same_limit and _same_err % _same_limit == 0:
                # 경고만 주입 (차단하지 않음) — AI가 참고하여 전략 변경 유도
                tool_results[-1]["content"] = (
                    compressed_str + f"\n\n⚠️ {tu.name}이 {_same_err}회 연속 실패 중. "
                    "다른 파라미터나 다른 접근법도 검토해보세요."
                )
                logger.warning(f"same_tool_error_warn: {tu.name}={_same_err}, turn={_turn}")

            # 연속 에러 경고/중단 (Green 도구는 관대, Yellow 도구는 엄격)
            _eff_stop = _CONSECUTIVE_ERROR_STOP if not _is_green else _CONSECUTIVE_ERROR_STOP * 2
            _eff_warn = _CONSECUTIVE_ERROR_WARN if not _is_green else _CONSECUTIVE_ERROR_WARN * 2

            if _consecutive_errors >= _eff_stop or _total_errors >= _TOTAL_ERROR_STOP:
                _err_msg = (
                    f"⚠️ 도구 호출이 연속 {_consecutive_errors}회 실패했습니다 (총 {_total_errors}회). "
                    "다른 접근 방식을 시도하거나, 현재까지의 결과를 정리하여 응답하세요. "
                    "동일한 도구를 같은 방식으로 반복 호출하지 마세요."
                )
                # L-08: 중복 tool_use_id 방지 — 기존 결과 교체
                tool_results[-1]["content"] = _err_msg
                tool_results[-1]["is_error"] = True
                logger.warning(f"error_circuit_breaker: consecutive={_consecutive_errors}, total={_total_errors}, tool={tu.name}, turn={_turn}")
                break  # 이 턴의 남은 도구 실행 스킵
            elif _consecutive_errors >= _eff_warn:
                # 경고 주입 — AI가 다른 방식을 시도하도록 유도
                tool_results[-1]["content"] = (
                    compressed_str + f"\n\n⚠️ 연속 {_consecutive_errors}회 도구 에러. "
                    "같은 방식 재시도 금지. 다른 도구나 다른 파라미터를 시도하세요."
                )
            # F5: Tool Result Archive — 도구 결과 전문 보관 (백그라운드)
            try:
                from app.services.tool_archive import archive_tool_result as _archive
                _f5_sid = _cv_sid.get("") if _cv_sid else ""
                if _f5_sid:
                    from app.core.db_pool import get_pool as _get_pool_f5
                    _f5_pool = _get_pool_f5()
                    async with _f5_pool.acquire() as _f5c:
                        _f5_mid = await _f5c.fetchval(
                            "SELECT id::text FROM chat_messages WHERE session_id = $1::uuid AND role = 'user' ORDER BY created_at DESC LIMIT 1",
                            _f5_sid,
                        )
                    if _f5_mid:
                        asyncio.create_task(_archive(_f5_mid, tu.id, tu.name, dict(tu.input), result_str))
            except Exception:
                pass

        # 메시지에 AI 응답 + 도구 결과 추가
        # L-07: SDK ContentBlock → dict 직렬화 (재전송 안정성)
        _serialized = []
        for _blk in (final_msg.content if isinstance(final_msg.content, list) else [final_msg.content]):
            if hasattr(_blk, "model_dump"):
                _serialized.append(_blk.model_dump())
            elif isinstance(_blk, dict):
                _serialized.append(_blk)
            else:
                _serialized.append({"type": "text", "text": str(_blk)})
        current_messages = current_messages + [
            {"role": "assistant", "content": _serialized},
            {"role": "user", "content": tool_results},
        ]

        # Layer A: 도구 루프 토큰 예산 관리 (120K)
        current_messages = _trim_tool_loop_context(current_messages, _turn)

        # CEO 인터럽트 체크: 도구 실행 완료 후, 다음 API 호출 전
        if session_id:
            from app.core.interrupt_queue import has_interrupt, pop_interrupts
            if has_interrupt(session_id):
                interrupts = pop_interrupts(session_id)
                interrupt_text = "\n".join(interrupts)
                current_messages.append({
                    "role": "user",
                    "content": f"[CEO 추가 지시] 작업 도중 CEO가 새로운 지시를 보냈습니다. 현재까지의 작업 결과를 고려하고, 이 새 지시를 반영하여 다음 행동을 판단하세요. CEO 지시가 기존 작업과 충돌하면 CEO 지시를 우선합니다.\n\n{interrupt_text}"
                })
                yield {"type": "interrupt_applied", "content": "CEO 추가 지시 반영 중..."}

        _turn += 1

        # 도구 턴 한도 도달 시 CEO 승인 요청 이벤트 발행 + 자동 연장
        if _turn >= _effective_max_turns and tool_use_blocks:
            logger.warning(f"tool_turn_limit: {_turn}/{_effective_max_turns} turns used, extending by {_TOOL_TURN_EXTEND}")
            _effective_max_turns += _TOOL_TURN_EXTEND
            # 무제한 — compaction이 컨텍스트 자동 관리
            yield {
                "type": "tool_turn_limit",
                "content": f"도구 호출이 {_turn}회에 도달했습니다. {_TOOL_TURN_EXTEND}턴 자동 연장합니다.",
                "current_turn": _turn,
                "extended_to": _effective_max_turns,
            }

    cost = _estimate_cost(model_alias, input_tokens, output_tokens)
    yield {
        "type": "done",
        "model": model_alias,
        "cost": str(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_summary": thinking_text[:2000] if thinking_text else None,
        "tools_called": tool_calls_made,
    }


def _build_system_with_cache(system_prompt: str) -> List[Dict[str, Any]]:
    """
    시스템 프롬프트를 정적/동적 파트로 분리하고 정적 파트에 cache_control 적용.
    """
    # "## 현재 상태" 구분자로 Layer 1 / Layer 2 분리
    sep = "\n\n## 현재 상태"
    if sep in system_prompt:
        idx = system_prompt.index(sep)
        static_part = system_prompt[:idx]
        dynamic_part = system_prompt[idx:]
        return [
            {
                "type": "text",
                "text": static_part,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": dynamic_part,
            },
        ]
    # 분리 불가 시 단일 블록
    return [{"type": "text", "text": system_prompt}]
