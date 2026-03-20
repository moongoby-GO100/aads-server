"""
AADS-185: 모델 선택기 — Claude CLI Relay + LiteLLM 분기
- Claude 인텐트: Claude Code CLI 경유 (MCP 도구 브릿지, OAuth 토큰 자동 관리)
- Gemini 인텐트 (casual, greeting): LiteLLM 경유
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

# LiteLLM 경유: OAuth 토큰은 LiteLLM이 관리 (sync_litellm_oauth.sh)
_LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
_LITELLM_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")

class _StripAuthTransport(httpx.AsyncBaseTransport):
    """SDK 자동 Authorization 헤더 제거 — LiteLLM x-api-key만 사용."""
    def __init__(self):
        self._inner = httpx.AsyncHTTPTransport()
    async def handle_async_request(self, request):
        raw = [(k, v) for k, v in request.headers.raw if k.lower() != b"authorization"]
        request.headers = httpx.Headers(raw)
        return await self._inner.handle_async_request(request)

def _get_anthropic_client() -> AsyncAnthropic:
    """LiteLLM 경유 Anthropic 클라이언트 반환."""
    return AsyncAnthropic(
        api_key=_LITELLM_API_KEY,
        base_url=_LITELLM_URL,
        http_client=httpx.AsyncClient(transport=_StripAuthTransport()),
        max_retries=5,
    )

def _switch_oat_token():
    """LiteLLM 경유이므로 토큰 스위치는 sync_litellm_oauth.sh가 처리. 여기선 no-op."""
    logger.warning("oat_token_switch: LiteLLM managed — run sync_litellm_oauth.sh")
    return False

_anthropic = _get_anthropic_client()

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")

# Claude CLI Relay (호스트에서 실행, Docker → host.docker.internal)
_CLAUDE_RELAY_URL = os.getenv("CLAUDE_RELAY_URL", "http://host.docker.internal:8199")
_CLAUDE_CLI_ENABLED = os.getenv("CLAUDE_CLI_ENABLED", "true").lower() == "true"

# Agent SDK OAuth 토큰 자동 교대
_KEY_NAVER = os.getenv("ANTHROPIC_API_KEY", "")
_KEY_GMAIL = os.getenv("ANTHROPIC_API_KEY_FALLBACK", "")
_KEY_LABELS = {}  # {key_prefix: label}
if _KEY_NAVER:
    _KEY_LABELS[_KEY_NAVER[:20]] = "Naver"
if _KEY_GMAIL:
    _KEY_LABELS[_KEY_GMAIL[:20]] = "Gmail"

# 키 순서 (런타임 변경 가능)
_ANTHROPIC_KEYS = [k for k in [_KEY_NAVER, _KEY_GMAIL] if k]


def get_key_order() -> List[Dict[str, str]]:
    """현재 키 순서 반환 (프론트 표시용)."""
    result = []
    for k in _ANTHROPIC_KEYS:
        label = _KEY_LABELS.get(k[:20], "Unknown")
        result.append({"label": label, "prefix": k[:12] + "..."})
    return result


def set_key_order(primary: str) -> bool:
    """키 순서 변경. primary='naver' 또는 'gmail'."""
    global _ANTHROPIC_KEYS
    if primary.lower() == "naver" and _KEY_NAVER:
        _ANTHROPIC_KEYS = [k for k in [_KEY_NAVER, _KEY_GMAIL] if k]
        logger.info(f"key_order_changed: Naver first")
        return True
    elif primary.lower() == "gmail" and _KEY_GMAIL:
        _ANTHROPIC_KEYS = [k for k in [_KEY_GMAIL, _KEY_NAVER] if k]
        logger.info(f"key_order_changed: Gmail first")
        return True
    return False

# AADS session_id → CLI session_id 매핑 (대화 이어가기용)
_cli_session_map: Dict[str, str] = {}  # {aads_session_id: cli_session_id}


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
    "gemini-3-flash-preview": (0.5,   3.0),   # 2026-03-12 공식 가격
    "gemini-3.1-flash-lite-preview": (0.25, 1.5),  # 최고 효율 (thinking 포함)
    "gemini-3.1-pro-preview": (2.0,  12.0),
    "gemini-2.5-flash":       (0.15,  0.6),   # thinking 별도 $3.50 (여기선 non-thinking만)
    "gemini-2.5-flash-lite":  (0.04,  0.1),
}

# LiteLLM alias → Anthropic model ID
_ANTHROPIC_MODEL_ID = {
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-opus":   "claude-opus-4-6",
    "claude-haiku":  "claude-haiku-4-5-20251001",
}

# Gemini 모델 (LiteLLM 경유)
_GEMINI_MODELS = {"gemini-flash", "gemini-flash-lite", "gemini-pro", "gemini-3-flash-preview", "gemini-3.1-flash-lite-preview", "gemini-3.1-pro-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite"}

# Gemini Thinking 모델 — reasoning_effort=low + 높은 max_tokens 필요
_GEMINI_THINKING_MODELS = {"gemini-pro", "gemini-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite-preview", "gemini-3.1-pro-preview", "gemini-2.5-flash"}


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

    # model_override가 구체적 모델명(claude-sonnet-4-6 등)이면 LiteLLM alias로 변환
    _OVERRIDE_TO_ALIAS = {
        "claude-sonnet-4-6": "claude-sonnet", "claude-sonnet-4-5": "claude-sonnet",
        "claude-opus-4-6": "claude-opus", "claude-opus-4-5": "claude-opus",
        "claude-haiku-4-5": "claude-haiku",
        "mixture": "claude-sonnet",  # 프론트엔드 자동 라우팅 (레거시)
        "auto": "claude-sonnet",    # 프론트엔드 자동 라우팅 (채팅 UI)
    }
    if model in _OVERRIDE_TO_ALIAS:
        model = _OVERRIDE_TO_ALIAS[model]
    # 안전망: 알 수 없는 모델명이 CLI relay를 우회하지 않도록 기본값 적용
    if model not in _GEMINI_MODELS and model not in _ANTHROPIC_MODEL_ID:
        logger.warning(f"unknown_model_fallback: '{model}' → 'claude-sonnet'")
        model = "claude-sonnet"

    # Claude 모델 → 3단계 폴백: CLI Relay → Agent SDK → Gemini
    if model not in _GEMINI_MODELS and model in _ANTHROPIC_MODEL_ID:

        # 1단계: CLI Relay (호스트 최신 CLI, OAuth 안정)
        _had_error = False
        async for event in _stream_cli_relay(model, system_prompt, messages, tools=tools, session_id=session_id):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"cli_relay_error: {model} failed, trying Agent SDK — {event.get('content', '')[:100]}")
                break
            yield event
        if not _had_error:
            return

        # 2단계: LiteLLM Anthropic 직접 (10회 재시도 + 듀얼키 자동전환)
        _had_error = False
        logger.info(f"cli_relay_failed: trying LiteLLM Anthropic direct for {model}")
        async for event in _stream_litellm_anthropic(model, system_prompt, messages, tools=tools, session_id=session_id):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"litellm_anthropic_error: {model} failed, trying Gemini — {event.get('content', '')[:100]}")
                break
            yield event
        if not _had_error:
            return

        # 3단계: Gemini (최종 안전망) — error_log 기록
        yield {"type": "delta", "content": "[Claude 장애 → Gemini 전환]\n\n"}
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO error_log (error_type, source, server, message, stack_trace, created_at) VALUES ($1, $2, $3, $4, $5, NOW())",
                    "claude_api_fallback", "model_selector.cli_relay_path", "aads-server",
                    f"Claude {model} → Gemini 전환 (CLI Relay + LiteLLM Anthropic 모두 실패)",
                    "",
                )
        except Exception as _log_err:
            logger.warning(f"error_log insert failed: {_log_err}")
        async for event in _stream_litellm("gemini-3.1-flash-lite-preview", system_prompt, messages, tools=tools):
            yield event
        return

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
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """LiteLLM 프록시를 통한 스트리밍.
    Claude 모델: /v1/messages?beta=true (Anthropic 네이티브, 도구 호환성 보장)
    Gemini 모델: /chat/completions (OpenAI 호환)
    """
    _is_claude = model in _ANTHROPIC_MODEL_ID

    if _is_claude:
        async for event in _stream_litellm_anthropic(model, system_prompt, messages, tools, session_id=session_id):
            yield event
    else:
        async for event in _stream_litellm_openai(model, system_prompt, messages, tools):
            yield event


async def _stream_litellm_anthropic(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Claude 모델 → LiteLLM /v1/messages?beta=true (멀티턴 도구 루프 지원)."""
    current_msgs = [m for m in messages if m.get("role") != "system"]
    litellm_model = _ANTHROPIC_MODEL_ID.get(model, model)
    _display_model = litellm_model

    full_text = ""
    input_tokens = 0
    output_tokens = 0
    _MAX_RETRIES = 10
    _MAX_TOOL_TURNS = 50
    _headers = {
        "x-api-key": LITELLM_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    _url = f"{LITELLM_BASE_URL}/v1/messages?beta=true"

    yield {"type": "model_info", "model": _display_model}

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            for _turn in range(_MAX_TOOL_TURNS):
                req_body: Dict[str, Any] = {
                    "model": litellm_model,
                    "system": system_prompt,
                    "messages": current_msgs,
                    "max_tokens": 16384,
                    "stream": True,
                }
                if tools:
                    req_body["tools"] = tools

                # 재시도 루프
                resp_ctx = None
                for _attempt in range(_MAX_RETRIES):
                    resp_ctx = client.stream("POST", _url, headers=_headers, json=req_body)
                    resp = await resp_ctx.__aenter__()
                    if resp.status_code == 200:
                        break
                    await resp.aread()
                    await resp_ctx.__aexit__(None, None, None)
                    resp_ctx = None
                    if _attempt < _MAX_RETRIES - 1:
                        logger.warning(f"litellm_anthropic_retry: turn={_turn} attempt={_attempt+1}/{_MAX_RETRIES} status={resp.status_code}")
                        await asyncio.sleep(0.3)
                    else:
                        yield {"type": "error", "content": f"Claude API {resp.status_code} after {_MAX_RETRIES} retries"}
                        return

                if resp_ctx is None:
                    yield {"type": "error", "content": "no response"}
                    return

                # SSE 파싱
                _tool_uses = []  # 이번 턴의 도구 호출들
                _assistant_content = []  # assistant 메시지 content 블록

                try:
                    _current_tool_id = ""
                    _current_tool_name = ""
                    _current_tool_input_json = ""
                    _current_text = ""

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except Exception:
                            continue

                        evt_type = event.get("type", "")

                        if evt_type == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                if _current_text:
                                    _assistant_content.append({"type": "text", "text": _current_text})
                                    _current_text = ""
                                _current_tool_id = cb.get("id", "")
                                _current_tool_name = cb.get("name", "")
                                _current_tool_input_json = ""

                        elif evt_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    full_text += text
                                    _current_text += text
                                    yield {"type": "delta", "content": text}
                            elif delta.get("type") == "input_json_delta":
                                _current_tool_input_json += delta.get("partial_json", "")

                        elif evt_type == "content_block_stop":
                            if _current_tool_name:
                                _args = json.loads(_current_tool_input_json) if _current_tool_input_json else {}
                                _tool_uses.append({
                                    "id": _current_tool_id,
                                    "name": _current_tool_name,
                                    "input": _args,
                                })
                                _assistant_content.append({
                                    "type": "tool_use",
                                    "id": _current_tool_id,
                                    "name": _current_tool_name,
                                    "input": _args,
                                })
                                _current_tool_name = ""
                                _current_tool_id = ""
                                _current_tool_input_json = ""

                        elif evt_type == "message_delta":
                            usage = event.get("usage", {})
                            output_tokens += usage.get("output_tokens", 0)

                        elif evt_type == "message_start":
                            msg = event.get("message", {})
                            usage = msg.get("usage", {})
                            input_tokens += usage.get("input_tokens", 0)

                finally:
                    await resp_ctx.__aexit__(None, None, None)

                # 남은 텍스트 블록 추가
                if _current_text:
                    _assistant_content.append({"type": "text", "text": _current_text})

                # 도구 호출이 없으면 종료
                if not _tool_uses:
                    break

                # 도구 실행 + tool_results 구성
                tool_results = []
                from app.api.ceo_chat_tools import execute_tool as _exec_tool
                for tu in _tool_uses:
                    yield {"type": "tool_use", "tool_name": tu["name"], "tool_use_id": tu["id"], "tool_input": tu["input"]}
                    try:
                        result = await _exec_tool(tu["name"], tu["input"], "", session_id or "")
                        yield {"type": "tool_result", "tool_name": tu["name"], "content": str(result)[:3000]}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": str(result)[:10000],
                        })
                    except Exception as _te:
                        logger.warning(f"tool_error: {tu['name']}: {_te}")
                        yield {"type": "delta", "content": f"\n[도구 {tu['name']} 실패: {str(_te)[:80]}]\n"}
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": f"Error: {str(_te)[:500]}",
                            "is_error": True,
                        })
                    # heartbeat (SSE 연결 유지)
                    yield {"type": "heartbeat"}

                # 다음 턴: assistant 메시지 + tool_results 추가
                current_msgs = current_msgs + [
                    {"role": "assistant", "content": _assistant_content},
                    {"role": "user", "content": tool_results},
                ]

                # CEO 인터럽트 체크 (BUG-1 FIX: LiteLLM 경로에서도 인터럽트 반영)
                if session_id:
                    from app.core.interrupt_queue import has_interrupt, pop_interrupts
                    if has_interrupt(session_id):
                        interrupts = pop_interrupts(session_id)
                        interrupt_text = "\n".join(i["content"] for i in interrupts)
                        # 인터럽트 첨부파일 → Vision content blocks
                        _intr_content: list | str = f"[CEO 추가 지시] 작업 도중 CEO가 새로운 지시를 보냈습니다. 현재까지의 작업 결과를 고려하고, 이 새 지시를 반영하여 다음 행동을 판단하세요. CEO 지시가 기존 작업과 충돌하면 CEO 지시를 우선합니다.\n\n{interrupt_text}"
                        _intr_images = []
                        for _intr_item in interrupts:
                            for att in _intr_item.get("attachments", []):
                                if att.get("type") == "image" and att.get("base64"):
                                    _intr_images.append({
                                        "type": "image_url",
                                        "image_url": {"url": f"data:{att.get('media_type', 'image/png')};base64,{att['base64']}"},
                                    })
                        if _intr_images:
                            _intr_content = [{"type": "text", "text": _intr_content}] + _intr_images
                        current_msgs.append({"role": "user", "content": _intr_content})
                        # 각 interrupt마다 개별 이벤트 yield (프론트 큐 동기화용)
                        for _intr_item in interrupts:
                            yield {"type": "interrupt_applied", "content": _intr_item["content"][:100]}

    except Exception as e:
        logger.error(f"model_selector litellm_anthropic error: {e}")
        yield {"type": "error", "content": str(e)}
        return

    cost = _estimate_cost(model, input_tokens, output_tokens)
    yield {
        "type": "done",
        "model": _display_model,
        "cost": str(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


async def _stream_litellm_openai(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Gemini 등 비-Claude 모델 → LiteLLM /chat/completions (OpenAI 호환 포맷)."""
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
                                _tool_result = await _exec_tool(_fn_name, _args, "", session_id or "")
                                yield {"type": "tool_use", "tool_name": _fn_name, "tool_use_id": _tc.get("id", ""), "tool_input": _args}
                                yield {"type": "tool_result", "tool_name": _fn_name, "content": str(_tool_result)[:3000]}
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


async def _stream_cli_relay(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """CLI Relay 서버(host.docker.internal:8199)를 통한 스트리밍.

    호스트 최신 CLI를 사용하므로 OAuth 인증 안정성이 높음.
    NDJSON 응답을 파싱하여 AADS SSE 이벤트로 변환.
    """
    sdk_model = _ANTHROPIC_MODEL_ID.get(model, model)

    # 세션 이어가기 여부
    _has_resume = bool(_cli_session_map.get(session_id)) if session_id else False
    messages_text = _format_messages_as_text(messages, has_resume=_has_resume)

    req_body = {
        "model": model,
        "system_prompt": system_prompt,
        "messages_text": messages_text,
        "session_id": session_id or "",
    }

    yield {"type": "model_info", "model": sdk_model}

    # CLI가 에러를 텍스트로 반환하는 패턴 감지 (529, 401 등)
    _CLI_ERROR_PATTERNS = [
        "api error:", "overloaded", "529", "503",
        "authentication_failed", "401", "unauthorized",
        "rate_limit", "429", "rate limit",
        "credit", "402",
    ]

    full_text = ""
    _tool_id_to_name: Dict[str, str] = {}
    _captured_cli_sid = _cli_session_map.get(session_id, "") if session_id else ""

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            # Health check first (빠른 실패)
            try:
                hc = await client.get(f"{_CLAUDE_RELAY_URL}/health", timeout=5.0)
                if hc.status_code != 200:
                    yield {"type": "error", "content": f"CLI Relay not healthy: {hc.status_code}"}
                    return
            except Exception as hc_err:
                yield {"type": "error", "content": f"CLI Relay unreachable: {hc_err}"}
                return

            async with client.stream(
                "POST",
                f"{_CLAUDE_RELAY_URL}/stream",
                json=req_body,
                timeout=httpx.Timeout(600.0, connect=10.0),
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {"type": "error", "content": f"CLI Relay {resp.status_code}: {body.decode()[:200]}"}
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # result 이벤트에서 is_error 체크 (CLI가 529 등으로 실패 시)
                    if event.get("type") == "result" and event.get("is_error"):
                        error_text = event.get("result", "CLI error")
                        logger.warning(f"cli_relay_result_error: {error_text[:100]}")
                        yield {"type": "error", "content": error_text}
                        return

                    # _map_cli_event 재사용하여 NDJSON → AADS 이벤트 변환
                    mapped = _map_cli_event(event)
                    if mapped is None:
                        # init 이벤트에서 session_id 캡처
                        evt_type = event.get("type", "")
                        if evt_type == "system" and event.get("subtype") == "init":
                            sid = event.get("session_id")
                            if sid:
                                _captured_cli_sid = sid
                        continue

                    for aads_evt in mapped:
                        evt_type = aads_evt.get("type", "")

                        # tool_use에서 tool_id→name 매핑 저장
                        if evt_type == "tool_use":
                            tid = aads_evt.get("tool_use_id", "")
                            tname = aads_evt.get("tool_name", "")
                            if tid and tname:
                                _tool_id_to_name[tid] = tname

                        # tool_result에 tool_name 복원
                        if evt_type == "tool_result" and not aads_evt.get("tool_name"):
                            tid = aads_evt.get("tool_use_id", "")
                            aads_evt["tool_name"] = _tool_id_to_name.get(tid, "")

                        # delta 텍스트 누적 (에러 패턴 검사는 result.is_error로 대체)
                        if evt_type == "delta":
                            delta_text = aads_evt.get("content", "")
                            full_text += delta_text

                        # done 이벤트에서 session_id 캡처 (result 이벤트)
                        if evt_type == "done":
                            result_sid = event.get("session_id")
                            if result_sid:
                                _captured_cli_sid = result_sid

                        yield aads_evt

    except httpx.ConnectError as e:
        yield {"type": "error", "content": f"CLI Relay connect failed: {e}"}
        return
    except httpx.ReadTimeout:
        yield {"type": "error", "content": "CLI Relay timeout (600s)"}
        return
    except Exception as e:
        logger.error(f"cli_relay_error: {e}")
        yield {"type": "error", "content": str(e)}
        return

    # 세션 매핑 저장
    if session_id and _captured_cli_sid:
        _cli_session_map[session_id] = _captured_cli_sid
        logger.info(f"cli_relay_session_map: aads={session_id[:8]} -> cli={_captured_cli_sid[:8]}")


async def _stream_agent_sdk(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Claude Agent SDK를 통한 스트리밍 (토큰 자동 교대).

    Agent SDK → 번들 CLI → Anthropic API (OAuth 직접 인증).
    Naver 토큰 우선 → 실패 시 Gmail 토큰 폴백.
    MCP 브릿지로 55개 도구 사용. 세션 자동 관리.
    """
    sdk_model = _ANTHROPIC_MODEL_ID.get(model, model)

    # 세션 이어가기 여부에 따라 메시지 포맷 결정
    _has_resume = bool(_cli_session_map.get(session_id)) if session_id else False
    user_message = _format_messages_as_text(messages, has_resume=_has_resume)

    # 토큰 교대: Naver → Gmail → Naver → Gmail ... (3라운드, 총 6회)
    keys = _ANTHROPIC_KEYS if _ANTHROPIC_KEYS else [os.getenv("ANTHROPIC_API_KEY", "")]
    keys = [k for k in keys if k]
    _MAX_ROUNDS = 3  # 라운드 수 (각 라운드에서 모든 키 시도)
    _RETRYABLE_PATTERNS = [
        "rate_limit", "429", "rate limit",
        "overloaded", "529", "503", "overloaded",
        "credit", "402", "credit",
        "authentication", "401", "unauthorized",
        "exit code 1", "no connected db",
        "server_error", "500", "internal",
        "timeout", "connection",
    ]

    yield {"type": "model_info", "model": sdk_model}

    _attempt = 0
    _last_error = ""
    for _round in range(_MAX_ROUNDS):
        for key_idx, api_key in enumerate(keys):
            _attempt += 1
            _key_label = "Naver" if "5ZEDHaA7" in api_key else "Gmail"

            error_msg = ""
            async for evt in _run_agent_sdk_with_key(
                api_key, sdk_model, system_prompt, user_message, session_id,
            ):
                if evt.get("type") == "error":
                    error_msg = evt.get("content", "")
                    break
                yield evt

            if not error_msg:
                return  # 성공

            _last_error = error_msg
            is_retryable = any(p in error_msg.lower() for p in _RETRYABLE_PATTERNS)

            if not is_retryable:
                # 재시도 불가 에러 (구문 오류 등) — 즉시 중단
                logger.error(f"token_fatal: {_key_label} attempt={_attempt} error={error_msg[:100]}")
                yield {"type": "error", "content": error_msg}
                return

            logger.warning(f"token_retry: {_key_label} attempt={_attempt}/{_MAX_ROUNDS*len(keys)} round={_round+1} error={error_msg[:80]}")
            yield {"type": "heartbeat"}

        # 라운드 사이 대기 (지수 백오프: 1초, 2초, 4초)
        if _round < _MAX_ROUNDS - 1:
            _wait = min(2 ** _round, 4)
            logger.info(f"token_round_wait: round={_round+1} wait={_wait}s before next round")
            await asyncio.sleep(_wait)

    # 모든 라운드 소진
    yield {"type": "error", "content": f"All OAuth tokens exhausted after {_attempt} attempts: {_last_error[:100]}"}


async def _run_agent_sdk_with_key(
    api_key: str,
    sdk_model: str,
    system_prompt: str,
    user_message: str,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """단일 API 키로 Agent SDK 실행. 세션 이어가기(--resume) 지원."""
    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions

    # MCP config: 컨테이너 내부에서 직접 브릿지 실행
    _mcp_cfg = json.dumps({
        "mcpServers": {
            "aads-tools": {
                "command": "python",
                "args": ["-m", "mcp_servers.aads_tools_bridge"],
                "cwd": "/app",
                "env": {"AADS_SESSION_ID": session_id or ""},
            }
        }
    })

    # 세션 이어가기: AADS session_id → CLI session_id 매핑
    cli_session_id = _cli_session_map.get(session_id) if session_id else None

    # Agent 팀 정의: 조사/개발/QA 서브에이전트
    from claude_agent_sdk import AgentDefinition
    _agents = {
        "researcher": AgentDefinition(
            description="코드 탐색, DB 조회, 로그 분석, 서버 상태 확인 등 조사가 필요할 때 사용. 여러 파일/DB를 병렬로 조사할 때 효율적.",
            prompt=(
                "당신은 시스템 조사 전문가입니다. "
                "MCP 도구(read_remote_file, query_db, query_project_database, search_logs, list_remote_dir, git_remote_status)를 사용하여 "
                "요청된 정보를 정확하게 수집하고 구조화된 보고서로 반환하세요. "
                "추측하지 말고 반드시 도구로 확인한 데이터만 보고하세요."
            ),
            model="sonnet",
        ),
        "developer": AgentDefinition(
            description="코드 수정, 파일 작성, 패치 적용, git 커밋/푸시 등 개발 작업이 필요할 때 사용.",
            prompt=(
                "당신은 풀스택 개발자입니다. "
                "MCP 도구(write_remote_file, patch_remote_file, run_remote_command, git_remote_add, git_remote_commit, git_remote_push)를 사용하여 "
                "요청된 코드 변경을 정확하게 수행하세요. "
                "변경 전 반드시 현재 코드를 read_remote_file로 확인하고, 변경 후 검증하세요."
            ),
            model="sonnet",
        ),
        "qa": AgentDefinition(
            description="테스트 실행, 변경사항 검증, 서비스 헬스체크, 에러 확인 등 품질 검증이 필요할 때 사용.",
            prompt=(
                "당신은 QA 엔지니어입니다. "
                "MCP 도구를 사용하여 시스템 상태를 검증하고, 에러를 탐지하고, 변경사항이 정상 반영되었는지 확인하세요. "
                "문제 발견 시 구체적인 에러 내용과 재현 경로를 보고하세요."
            ),
            model="sonnet",
        ),
    }

    opts = ClaudeAgentOptions(
        model=sdk_model,
        max_turns=200,
        permission_mode="acceptEdits",
        cwd="/app",
        system_prompt=system_prompt,
        mcp_servers=_mcp_cfg,
        agents=_agents,
        allowed_tools=["Agent", "mcp__aads-tools__*"],
        disallowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep",
                          "WebFetch", "WebSearch", "NotebookEdit"],
        env={"ANTHROPIC_API_KEY": api_key},
    )
    # --resume: 이전 대화가 있으면 이어가기
    if cli_session_id:
        opts.resume = cli_session_id
        logger.info(f"agent_sdk_resume: aads={session_id[:8]} cli={cli_session_id[:8]}")

    full_text = ""
    tools_called_list: List[str] = []
    _tool_id_to_name: Dict[str, str] = {}
    total_cost = 0.0
    in_tokens = 0
    out_tokens = 0
    _captured_cli_sid = cli_session_id or ""  # resume 시 기존 ID 유지

    try:
        async for msg in sdk_query(prompt=user_message, options=opts):
            msg_type = type(msg).__name__

            if msg_type == "AssistantMessage":
                # 에러 응답 감지 (CLI가 에러를 assistant 메시지로 반환하는 경우)
                for block in msg.content:
                    block_type = type(block).__name__
                    if block_type == "TextBlock":
                        text = block.text or ""
                        if text.startswith("API Error:") or "authentication_failed" in text:
                            yield {"type": "error", "content": text}
                            return
                        if text:
                            full_text += text
                            yield {"type": "delta", "content": text}
                    elif block_type == "ToolUseBlock":
                        raw_name = block.name or ""
                        tool_name = raw_name.split("__")[-1] if raw_name.startswith("mcp__") else raw_name
                        tool_id = block.id or ""
                        if tool_id and tool_name:
                            _tool_id_to_name[tool_id] = tool_name
                        tools_called_list.append(tool_name)
                        yield {
                            "type": "tool_use",
                            "tool_name": tool_name,
                            "tool_use_id": tool_id,
                            "tool_input": block.input if hasattr(block, "input") else {},
                        }
                    elif block_type == "ThinkingBlock":
                        thinking = getattr(block, "thinking", "")
                        if thinking:
                            yield {"type": "thinking", "thinking": thinking}

            elif msg_type == "UserMessage":
                content = msg.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_id = block.get("tool_use_id", "")
                            tool_name = _tool_id_to_name.get(tool_id, "")
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_content = "\n".join(
                                    b.get("text", "") for b in result_content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            yield {
                                "type": "tool_result",
                                "tool_name": tool_name,
                                "tool_use_id": tool_id,
                                "content": str(result_content)[:3000],
                            }
                yield {"type": "heartbeat"}

            elif msg_type == "SystemMessage":
                # init 이벤트에서 CLI session_id 캡처
                data = getattr(msg, "data", None) or {}
                if isinstance(data, dict) and data.get("session_id"):
                    _captured_cli_sid = data["session_id"]
                yield {"type": "heartbeat"}

            elif msg_type == "ResultMessage":
                is_error = getattr(msg, "is_error", False)
                if is_error:
                    yield {"type": "error", "content": getattr(msg, "result", "SDK error")}
                    return
                total_cost = getattr(msg, "total_cost_usd", 0) or 0
                usage = getattr(msg, "usage", {}) or {}
                in_tokens = usage.get("input_tokens", 0) or 0
                out_tokens = usage.get("output_tokens", 0) or 0
                # ResultMessage에서도 session_id 캡처 (폴백)
                result_sid = getattr(msg, "session_id", None)
                if result_sid:
                    _captured_cli_sid = result_sid

    except Exception as e:
        error_str = str(e)
        logger.error(f"agent_sdk_key_error: {error_str[:200]}")
        yield {"type": "error", "content": error_str}
        return

    # 세션 매핑 저장 (대화 이어가기용)
    if session_id and _captured_cli_sid:
        _cli_session_map[session_id] = _captured_cli_sid
        logger.info(f"agent_sdk_session_map: aads={session_id[:8]} -> cli={_captured_cli_sid[:8]}")

    # done 이벤트
    cost = total_cost if total_cost else float(_estimate_cost("claude-sonnet", in_tokens, out_tokens))
    yield {
        "type": "done",
        "model": sdk_model,
        "cost": str(round(cost, 6)),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "tools_called": tools_called_list,
    }


def _format_messages_as_text(messages: List[Dict[str, Any]], has_resume: bool = False) -> str:
    """메시지 배열 → 텍스트 변환.

    has_resume=True: CLI가 이전 대화를 기억하므로 최신 user 메시지만 전달.
    has_resume=False: 대화 기록 포함 (최근 40개 메시지, CLI에 컨텍스트 제공).
    """

    def _extract_text(content) -> str:
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        tc = block.get("content", "")
                        if isinstance(tc, str):
                            parts.append("[도구결과] %s" % tc[:500])
                        elif isinstance(tc, list):
                            for b in tc:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    parts.append("[도구결과] %s" % b.get("text", "")[:500])
                    elif block.get("type") == "tool_use":
                        parts.append("[도구호출: %s]" % block.get("name", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        elif isinstance(content, str):
            return content
        return str(content)

    # --resume 있으면: 최신 user 메시지만
    if has_resume:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                text = _extract_text(msg.get("content", ""))
                if text.strip():
                    return text

    # --resume 없으면: 최근 대화 기록 포함 (최대 40개 메시지)
    recent = messages[-40:] if len(messages) > 40 else messages
    parts = []
    for msg in recent:
        role = msg.get("role", "user")
        if role == "system":
            continue
        content = _extract_text(msg.get("content", ""))
        if not content.strip():
            continue
        # 긴 응답은 축소
        if role == "assistant" and len(content) > 1000:
            content = content[:800] + "\n...[응답 축소]..." + content[-200:]
        role_label = "CEO" if role == "user" else "AI"
        parts.append("[%s]\n%s" % (role_label, content))
    return "\n\n".join(parts)


def _map_cli_event(event: dict) -> Optional[List[Dict[str, Any]]]:
    """Claude CLI NDJSON 이벤트 → AADS SSE 이벤트 리스트로 변환.

    Returns None if event should be skipped, otherwise a list of AADS events.
    """
    evt_type = event.get("type", "")

    # init 이벤트 — 스킵
    if evt_type == "system" and event.get("subtype") == "init":
        return None

    # assistant 메시지 — 텍스트/도구 추출
    if evt_type == "assistant":
        msg = event.get("message", {})
        content_blocks = msg.get("content", [])
        events = []
        for block in content_blocks:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        events.append({"type": "delta", "content": text})
                elif block.get("type") == "tool_use":
                    # MCP 접두사 제거: mcp__aads-tools__query_db → query_db
                    raw_name = block.get("name", "")
                    tool_name = raw_name.split("__")[-1] if raw_name.startswith("mcp__") else raw_name
                    events.append({
                        "type": "tool_use",
                        "tool_name": tool_name,
                        "tool_use_id": block.get("id", ""),
                        "tool_input": block.get("input", {}),
                    })
        # usage 정보 (부분)
        usage = msg.get("usage", {})
        if usage.get("input_tokens") or usage.get("output_tokens"):
            # heartbeat로 진행 표시
            events.append({"type": "heartbeat"})
        return events if events else None

    # user 이벤트 — MCP 도구 결과 (CLI가 tool_result를 user 메시지로 감싸서 전달)
    if evt_type == "user":
        msg = event.get("message", {})
        content_blocks = msg.get("content", [])
        events = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # tool_result 내부의 텍스트 추출
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = "\n".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                tool_use_id = block.get("tool_use_id", "")
                events.append({
                    "type": "tool_result",
                    "tool_name": "",  # _stream_claude_cli에서 복원
                    "tool_use_id": tool_use_id,
                    "content": str(result_content)[:3000],
                })
        return events if events else None

    # tool_result 이벤트 (직접 형식 — 폴백)
    if evt_type == "tool_result":
        tool_name = event.get("tool_name", "")
        content = event.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        return [{
            "type": "tool_result",
            "tool_name": tool_name,
            "content": str(content)[:3000],
        }]

    # result 이벤트 — 최종 완료
    if evt_type == "result":
        usage = event.get("usage", {})
        model = ""
        # modelUsage에서 모델명과 토큰 추출
        model_usage = event.get("modelUsage", {})
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        total_cost = event.get("total_cost_usd", 0)
        for m, mu in model_usage.items():
            model = m.split("[")[0]  # "claude-opus-4-6[1m]" → "claude-opus-4-6"
            in_tokens = mu.get("inputTokens", in_tokens)
            out_tokens = mu.get("outputTokens", out_tokens)
            if mu.get("costUSD"):
                total_cost = mu["costUSD"]
            break

        # result 텍스트 (CLI가 최종 결과를 result 필드에도 넣음)
        result_text = event.get("result", "")
        events = []
        # result에 텍스트가 있고 아직 delta로 안 보냈으면 보내기
        # (보통 assistant 이벤트에서 이미 보냄 — 중복 방지를 위해 스킵)
        events.append({
            "type": "done",
            "model": model,
            "cost": str(round(total_cost, 6)),
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
        })
        return events

    # 그 외: heartbeat
    return [{"type": "heartbeat"}]


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
    global _anthropic
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

    # 스트리밍 시작 시 모델 정보 전송 (프론트에서 대답 중 모델명 표시용)
    _display_model = _ANTHROPIC_MODEL_ID.get(model_alias, model_alias)
    yield {"type": "model_info", "model": _display_model}

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

        # 재시도 로직: 일시적 에러(400/429/529/503/네트워크)는 최대 10회 재시도
        # 400: Anthropic API 간헐적 invalid_request_error (2026-03 발생, ~50% 실패율)
        _RETRYABLE_STATUS = {400, 429, 503, 529}
        _MAX_RETRIES = 10
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
                # 429: 토큰 자동 스위치 시도
                if _status == 429 and _switch_oat_token():
                    _anthropic = _get_anthropic_client()
                    api_kwargs["_client_refreshed"] = True
                    logger.warning(f"oat_switch_on_429: switched token, retrying")
                    _retry_attempt -= 1  # 스위치 후 재시도 카운트 차감
                if _retry_attempt <= _MAX_RETRIES:
                    _wait = min(2 ** _retry_attempt, 10)
                    logger.warning(f"claude_retry: attempt {_retry_attempt}/{_MAX_RETRIES}, status={_status}, wait={_wait}s, error={str(e)[:100]}")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                else:
                    logger.error(f"claude_retry_exhausted: {_MAX_RETRIES} retries failed, status={_status}, error={str(e)[:100]}")
                    yield {"type": "error", "content": str(e)}
                    return

            except APIStatusError as e:
                _retry_attempt += 1
                _last_error = e
                _status = getattr(e, 'status_code', 0)
                # 402(크레딧 소진): 토큰 자동 스위치
                if _status == 402 and _switch_oat_token():
                    _anthropic = _get_anthropic_client()
                    logger.warning(f"oat_switch_on_402: credit exhausted, switched token")
                    _retry_attempt -= 1
                if _status in _RETRYABLE_STATUS and _retry_attempt <= _MAX_RETRIES:
                    # 400: 간헐적 에러 → 짧은 대기, 429/503: rate limit → 지수 백오프
                    _wait = 0.3 if _status == 400 else min(2 ** _retry_attempt, 10)
                    logger.warning(f"claude_retry: attempt {_retry_attempt}/{_MAX_RETRIES}, status={_status}, wait={_wait}s")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                elif _status == 402:
                    # 402는 재시도 대상에 추가
                    _wait = 2
                    logger.warning(f"claude_retry_402: attempt {_retry_attempt}, switching token and retrying")
                    yield {"type": "heartbeat"}
                    await asyncio.sleep(_wait)
                else:
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
        # L-07 fix: SDK ContentBlock → API 허용 필드만 추출 (parsed_output 등 제거)
        _serialized = []
        for _blk in (final_msg.content if isinstance(final_msg.content, list) else [final_msg.content]):
            _blk_type = getattr(_blk, "type", None) or (isinstance(_blk, dict) and _blk.get("type")) or ""
            if _blk_type == "text":
                _serialized.append({"type": "text", "text": getattr(_blk, "text", "") or (_blk.get("text", "") if isinstance(_blk, dict) else str(_blk))})
            elif _blk_type == "tool_use":
                _serialized.append({
                    "type": "tool_use",
                    "id": getattr(_blk, "id", "") or (_blk.get("id", "") if isinstance(_blk, dict) else ""),
                    "name": getattr(_blk, "name", "") or (_blk.get("name", "") if isinstance(_blk, dict) else ""),
                    "input": getattr(_blk, "input", {}) or (_blk.get("input", {}) if isinstance(_blk, dict) else {}),
                })
            elif isinstance(_blk, dict):
                # 알 수 없는 타입은 허용 필드만 전달
                _clean = {k: v for k, v in _blk.items() if k in ("type", "text", "id", "name", "input", "content")}
                _serialized.append(_clean if _clean else {"type": "text", "text": str(_blk)})
            else:
                _serialized.append({"type": "text", "text": str(_blk)})
        current_messages = current_messages + [
            {"role": "assistant", "content": _serialized},
            {"role": "user", "content": tool_results},
        ]

        # Layer A: 도구 루프 토큰 예산 관리 (120K)
        current_messages = _trim_tool_loop_context(current_messages, _turn)

        # api_kwargs에 업데이트된 messages 반영 (참조가 끊어지므로 명시적 갱신)
        api_kwargs["messages"] = current_messages

        # CEO 인터럽트 체크: 도구 실행 완료 후, 다음 API 호출 전
        if session_id:
            from app.core.interrupt_queue import has_interrupt, pop_interrupts
            if has_interrupt(session_id):
                interrupts = pop_interrupts(session_id)
                interrupt_text = "\n".join(i["content"] for i in interrupts)
                # 인터럽트 첨부파일 → Claude Vision content blocks
                _intr_content: list | str = f"[CEO 추가 지시] 작업 도중 CEO가 새로운 지시를 보냈습니다. 현재까지의 작업 결과를 고려하고, 이 새 지시를 반영하여 다음 행동을 판단하세요. CEO 지시가 기존 작업과 충돌하면 CEO 지시를 우선합니다.\n\n{interrupt_text}"
                _intr_images = []
                for _intr_item in interrupts:
                    for att in _intr_item.get("attachments", []):
                        if att.get("type") == "image" and att.get("base64"):
                            _intr_images.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": att.get("media_type", "image/png"),
                                    "data": att["base64"],
                                },
                            })
                if _intr_images:
                    _intr_content = [{"type": "text", "text": _intr_content}] + _intr_images
                current_messages.append({"role": "user", "content": _intr_content})
                # 각 interrupt마다 개별 이벤트 yield (프론트 큐 동기화용)
                for _intr_item in interrupts:
                    yield {"type": "interrupt_applied", "content": _intr_item["content"][:100]}

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
    # 프론트 표시용: alias(claude-opus) → 실제 모델ID(claude-opus-4-6)
    _display_model = _ANTHROPIC_MODEL_ID.get(model_alias, model_alias)
    yield {
        "type": "done",
        "model": _display_model,
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
