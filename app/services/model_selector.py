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

import httpx
from anthropic import AsyncAnthropic, APIStatusError
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
    "claude-opus":            (15.0, 75.0),
    "claude-sonnet":          (3.0,  15.0),
    "claude-haiku":           (0.25, 1.25),
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
            async for event in _stream_anthropic(_fallback_intent, "claude-haiku", system_prompt, messages, tools):
                yield event
        return

    # Claude 모델 → Anthropic SDK 직접 (실패 시 Gemini Flash 폴백)
    _had_error = False
    async for event in _stream_anthropic(intent_result, model, system_prompt, messages, tools):
        if event.get("type") == "error":
            _had_error = True
            _error_content = event.get("content", "")
            logger.warning(f"claude_fallback: {model} failed ({_error_content[:100]}), falling back to gemini-flash")
            break
        yield event
    if _had_error:
        # Claude 실패 → Gemini 3 Flash로 폴백 (도구 미지원 경량 모드)
        yield {"type": "delta", "content": "[Claude API 일시 장애, Gemini로 전환]\n\n"}
        async for event in _stream_litellm("gemini-3-flash-preview", system_prompt, messages):
            yield event


async def _stream_litellm(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
) -> AsyncGenerator[Dict[str, Any], None]:
    """LiteLLM 프록시를 통한 스트리밍."""
    msgs = [{"role": "system", "content": system_prompt}] + messages

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

                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        full_text += text
                        yield {"type": "delta", "content": text}

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


async def _stream_anthropic(
    intent_result: IntentResult,
    model_alias: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Anthropic SDK 직접 스트리밍 (Tool Use + Extended Thinking + Prompt Caching)."""
    model_id = _ANTHROPIC_MODEL_ID.get(model_alias, "claude-sonnet-4-6")

    # AADS-186E-2: Extended Thinking — Opus 전용, 환경변수로 제어
    use_thinking = (
        _EXTENDED_THINKING_ENABLED
        and intent_result.use_extended_thinking
        and model_alias == "claude-opus"
    )
    max_tokens = 64000 if use_thinking else 16384

    # 시스템 프롬프트 (Prompt Caching: Layer 1 정적 부분에 cache_control)
    system_blocks = _build_system_with_cache(system_prompt)

    # #26: Extended Thinking 설정 (환경변수 오버라이드)
    thinking_config = None
    if use_thinking:
        _thinking_budget = int(os.getenv("MAX_THINKING_TOKENS", "8000"))
        thinking_config = {"type": "enabled", "budget_tokens": _thinking_budget}

    full_text = ""
    thinking_text = ""
    input_tokens = 0
    output_tokens = 0

    # Tool Use 루프 (최대 20회 — 무한 대화 지원)
    _MAX_TOOL_TURNS = int(os.getenv("MAX_TOOL_TURNS", "20"))
    _TOOL_TURN_EXTEND = 10  # CEO 승인 시 추가 턴
    current_messages = list(messages)
    tool_calls_made = []
    _consecutive_yellow = 0  # Yellow 등급 도구 연속 호출 카운터
    _YELLOW_TOOLS = {
        "write_remote_file", "patch_remote_file", "run_remote_command",
        "git_remote_add", "git_remote_commit", "git_remote_push",
        "git_remote_create_branch", "deep_crawl", "deep_research",
        "spawn_subagent", "spawn_parallel_subagents",
    }
    _YELLOW_CONSECUTIVE_LIMIT = 5
    _effective_max_turns = _MAX_TOOL_TURNS
    _turn = 0

    while _turn < _effective_max_turns:
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
        if thinking_config:
            api_kwargs["thinking"] = thinking_config
            api_kwargs["betas"] = ["interleaved-thinking-2025-05-14"]
            # Extended Thinking + tool_choice="any" 비호환 — auto로 복귀
            if "tool_choice" in api_kwargs:
                del api_kwargs["tool_choice"]

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

        except APIStatusError as e:
            logger.error(f"model_selector anthropic error: {e}")
            yield {"type": "error", "content": str(e)}
            return
        except Exception as e:
            logger.error(f"model_selector anthropic unexpected: {e}")
            yield {"type": "error", "content": str(e)}
            return

        input_tokens = final_msg.usage.input_tokens
        output_tokens = final_msg.usage.output_tokens
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
        from app.services.tool_executor import ToolExecutor
        executor = ToolExecutor()

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

            # tool 실행 중 5초마다 heartbeat yield (SSE 타임아웃 방지)
            task = asyncio.create_task(executor.execute(tu.name, tu.input))
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                except Exception:
                    break
            try:
                result_str = task.result() if task.done() and not task.cancelled() else '{"error": "tool execution failed"}'
            except Exception as exc:
                logger.warning(f"tool execution error: tool={tu.name} error={exc}")
                result_str = json.dumps({"error": str(exc), "tool": tu.name})

            # 도구 결과 자동 압축 (컨텍스트에 넣기 전)
            try:
                from app.services.context_compressor import compress_tool_output
                compressed_str = compress_tool_output(tu.name, result_str)
            except Exception:
                compressed_str = result_str  # fallback: 원본 유지

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

        # 메시지에 AI 응답 + 도구 결과 추가
        current_messages = current_messages + [
            {"role": "assistant", "content": final_msg.content},
            {"role": "user", "content": tool_results},
        ]

        _turn += 1

        # 도구 턴 한도 도달 시 CEO 승인 요청 이벤트 발행 + 자동 연장
        if _turn >= _effective_max_turns and tool_use_blocks:
            logger.warning(f"tool_turn_limit: {_turn}/{_effective_max_turns} turns used, extending by {_TOOL_TURN_EXTEND}")
            _effective_max_turns += _TOOL_TURN_EXTEND
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
