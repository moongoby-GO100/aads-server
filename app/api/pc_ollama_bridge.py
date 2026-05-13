"""OpenAI-compatible bridge for CEO PC Ollama models.

LiteLLM can only call HTTP endpoints reachable from the Docker network. CEO PC
Ollama is reached through PC Agent, so this router presents a narrow
/chat/completions surface for LiteLLM and delegates execution to PC Agent.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter(prefix="/pc-ollama", tags=["pc-ollama"])

_MODEL_MAP = {
    "pc-gemma4-e2b": "gemma4:e2b",
    "gemma4:e2b": "gemma4:e2b",
    "pc-gemma4-e4b": "gemma4:e4b",
    "gemma4:e4b": "gemma4:e4b",
    "pc-gemma4-26b": "gemma4:26b",
    "pc-gemma4-26b-a4b": "gemma4:26b",
    "gemma4:26b": "gemma4:26b",
    "pc-gemma4-31b": "gemma4:31b",
    "gemma4:31b": "gemma4:31b",
    "pc-qwen3-0.6b": "qwen3:0.6b",
    "qwen3:0.6b": "qwen3:0.6b",
    "pc-qwen3-1.7b": "qwen3:1.7b",
    "qwen3:1.7b": "qwen3:1.7b",
    "pc-qwen3-4b": "qwen3:4b",
    "qwen3:4b": "qwen3:4b",
    "pc-qwen3-8b": "qwen3:8b",
    "qwen3:8b": "qwen3:8b",
    "pc-qwen3-14b": "qwen3:14b",
    "qwen3:14b": "qwen3:14b",
    "pc-qwen3-30b": "qwen3:30b",
    "pc-qwen3-30b-a3b": "qwen3:30b",
    "qwen3:30b": "qwen3:30b",
    "pc-qwen2.5vl-3b": "qwen2.5vl:3b",
    "qwen2.5vl:3b": "qwen2.5vl:3b",
    "pc-qwen2.5vl-7b": "qwen2.5vl:7b",
    "qwen2.5vl:7b": "qwen2.5vl:7b",
}


def _bridge_token() -> str:
    return os.getenv("PC_OLLAMA_BRIDGE_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or ""


def _check_auth(authorization: str | None) -> None:
    expected = _bridge_token()
    if not expected:
        raise HTTPException(status_code=503, detail="PC Ollama bridge token is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="invalid PC Ollama bridge token")


def _string_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in {"text", "input_text"}:
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def _normalize_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    normalized: list[dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip()
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        content = _string_content(msg.get("content"))
        if role == "tool":
            role = "user"
            content = f"[tool_result]\n{content}"
        normalized.append({"role": role, "content": content})
    return normalized


def _finish_reason(content: str) -> str:
    return "stop" if content.strip() else "length"


async def _run_pc_ollama_chat(payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.pc_agent_manager import pc_agent_manager

    display_model = str(payload.get("model") or "").strip()
    ollama_model = _MODEL_MAP.get(display_model)
    if not ollama_model:
        raise HTTPException(status_code=404, detail=f"unsupported PC Ollama model: {display_model}")

    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    max_tokens = payload.get("max_tokens") or options.get("num_predict") or 2048
    temperature = payload.get("temperature", options.get("temperature", 0.2))
    timeout_seconds = float(payload.get("timeout_seconds") or 300)

    result = await pc_agent_manager.execute_routed_command(
        command_type="ollama_chat",
        params={
            "model": ollama_model,
            "messages": _normalize_messages(payload.get("messages")),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout_seconds": timeout_seconds,
            "think": bool(payload.get("think", False)),
        },
        job_type="pc_ollama",
        required_capabilities=["pc_ollama"],
        queue_if_busy=True,
        wait_for_turn=True,
        queue_wait_timeout_seconds=min(timeout_seconds, 120.0),
        lease_ttl_seconds=int(timeout_seconds) + 30,
        command_timeout_seconds=timeout_seconds,
    )
    if result.get("status") != "success":
        detail = result.get("message") or result.get("detail") or result.get("error_code") or "PC Ollama route failed"
        raise HTTPException(status_code=503, detail=detail)

    data = (result.get("result") or {}).get("result") or {}
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    return {
        "display_model": display_model,
        "ollama_model": ollama_model,
        "content": str(data.get("content") or ""),
        "prompt_tokens": int(raw.get("prompt_eval_count") or 0),
        "completion_tokens": int(raw.get("eval_count") or 0),
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    payload = await request.json()
    result = await _run_pc_ollama_chat(payload)
    response_id = f"chatcmpl-pc-ollama-{uuid.uuid4().hex[:16]}"
    created = int(time.time())
    content = result["content"]

    if bool(payload.get("stream")):
        async def _events():
            model = result["display_model"]
            role_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"
            if content:
                text_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n"
            done_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": _finish_reason(content)}],
            }
            yield f"data: {json.dumps(done_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_events(), media_type="text/event-stream")

    total_tokens = result["prompt_tokens"] + result["completion_tokens"]
    return JSONResponse(
        {
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": result["display_model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": _finish_reason(content),
                }
            ],
            "usage": {
                "prompt_tokens": result["prompt_tokens"],
                "completion_tokens": result["completion_tokens"],
                "total_tokens": total_tokens,
            },
        }
    )
