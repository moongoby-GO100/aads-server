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
    # 2026-10-20 CEO 지시로 27B 를 bonsai-2:27b(8.3GB, 32.5 tok/s) 로 교체했다.
    # 기존 별칭 pc-qwen38-27b 는 검수 보조(runner_review_advisory_config)·LiteLLM 이
    # 쓰므로 이름은 유지하고 실제 대상만 옮긴다. 옛 hf.co IQ2_XS 태그(10.2 tok/s)는 제거.
    "pc-qwen38-27b": "bonsai-2:27b",
    "pc-bonsai2-27b": "bonsai-2:27b",
    "bonsai-2:27b": "bonsai-2:27b",
    "pc-qwen35-9b": "qwen3.5:9b",
    "qwen3.5:9b": "qwen3.5:9b",
}

# Bonsai README 권장 비사고 모드 샘플링. 호출자가 지정하지 않은 값에만 적용한다.
_NON_THINKING_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5}
_NON_THINKING_TARGETS = frozenset({"bonsai-2:27b", "qwen3.5:9b"})


_PEER_HOP_KEY = "_pc_ollama_peer_hop"
_PEER_BASE_URLS = ("http://aads-server:8080", "http://aads-server-green:8080")


_PEER_ROUTING_ERROR_CODES = frozenset({"PC_AGENT_OFFLINE", "NO_CAPABLE_AGENT"})
_NO_RETRY_ERROR_CODES = frozenset({"COMMAND_TIMEOUT"})
_AGENT_ATTEMPT_LIMIT = 3


def _peer_bases() -> list[str]:
    local = str(os.getenv("AADS_CONTAINER_NAME", "") or "").strip()
    return [base for base in _PEER_BASE_URLS if not local or base.split("//", 1)[-1].split(":", 1)[0] != local]


async def _forward_to_peer(
    payload: dict[str, Any],
    detail: str,
    *,
    error_code: str = "",
    agent_failed: bool = False,
    errors: list[str] | None = None,
) -> dict[str, Any] | None:
    """PC Agent WebSocket 이 붙어 있는 반대편 슬롯으로 한 번만 넘긴다.

    PC Agent 연결은 `pc_agent_manager` 의 **프로세스 내** 레지스트리라, blue/green
    중 소켓을 쥔 쪽만 실행할 수 있다. 활성 슬롯이 바뀌어도 소켓은 옛 컨테이너에
    남아 있어서, LiteLLM 이 어느 쪽을 잡느냐에 따라 503 'no online PC agent' 가
    절반씩 났다(2026-09-21 실측: 200/503/200). 한 홉만 넘겨 그 틈을 없앤다.

    넘기는 경우: 로컬에 agent 가 없거나(라우팅 오류) 로컬 agent 를 다 시도하고도
    실패했을 때(`agent_failed`). 자기 자신은 건너뛰고, `_PEER_HOP_KEY` 때문에
    받은 쪽이 다시 넘기지 않는다. 반대편이 실패한 이유는 `errors` 에 모은다 —
    그렇지 않으면 최종 503 이 로컬의 'no online PC agent' 로 보여 진짜 원인을 가린다.
    """
    if payload.get(_PEER_HOP_KEY):
        return None
    routing_failure = (
        "no online PC agent" in str(detail)
        or str(error_code or "") in _PEER_ROUTING_ERROR_CODES
    )
    if not (routing_failure or agent_failed):
        return None

    import httpx

    forwarded = {key: value for key, value in payload.items() if key != "stream"}
    forwarded[_PEER_HOP_KEY] = True
    token = _bridge_token()
    for base in _peer_bases():
        try:
            async with httpx.AsyncClient(timeout=310.0) as client:
                response = await client.post(
                    f"{base}/pc-ollama/v1/chat/completions",
                    json=forwarded,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code != 200:
                if errors is not None:
                    try:
                        peer_detail = response.json().get("detail")
                    except Exception:
                        peer_detail = None
                    errors.append(f"{base.split('//', 1)[-1]} {response.status_code} {peer_detail or ''}".strip())
                continue
            body = response.json()
            choices = body.get("choices") or []
            message = (choices[0].get("message") if choices else {}) or {}
            usage = body.get("usage") or {}
            return {
                "display_model": body.get("model") or payload.get("model"),
                "ollama_model": _MODEL_MAP.get(str(payload.get("model") or ""), ""),
                "content": str(message.get("content") or ""),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            }
        except Exception as exc:  # 반대편도 죽어 있으면 원래 503 을 그대로 올린다
            if errors is not None:
                errors.append(f"{base.split('//', 1)[-1]} {type(exc).__name__}")
            continue
    return None


def _bridge_token() -> str:
    return os.getenv("PC_OLLAMA_BRIDGE_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or ""


def _check_auth(authorization: str | None) -> None:
    expected = _bridge_token()
    if not expected:
        raise HTTPException(status_code=503, detail="PC Ollama bridge token is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="invalid PC Ollama bridge token")


def _ollama_image(value: Any) -> str | None:
    """Return an Ollama-compatible base64 image without fetching remote URLs."""
    if isinstance(value, dict):
        value = value.get("url") or value.get("data") or value.get("image_data")
    if not isinstance(value, str):
        return None
    image = value.strip()
    if not image:
        return None
    if image.startswith("data:image/") and ";base64," in image:
        return image.split(";base64,", 1)[1]
    # Raw base64 is accepted for PC Agent/Ollama callers. HTTP URLs and
    # server-local paths are intentionally not dereferenced by the bridge.
    if "://" in image or image.startswith(("/", "\\")):
        return None
    return image


def _content_parts(content: Any) -> tuple[str, list[str]]:
    images: list[str] = []
    if isinstance(content, str):
        return content, images
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in {"text", "input_text"}:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") in {"image_url", "input_image", "image"}:
                    image = _ollama_image(
                        item.get("image_url") or item.get("image_data") or item.get("data")
                    )
                    if image:
                        images.append(image)
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part), images
    return (json.dumps(content, ensure_ascii=False) if content is not None else ""), images


def _string_content(content: Any) -> str:
    return _content_parts(content)[0]


def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip()
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        content, images = _content_parts(msg.get("content"))
        for raw_image in msg.get("images") or []:
            image = _ollama_image(raw_image)
            if image:
                images.append(image)
        if role == "tool":
            role = "user"
            content = f"[tool_result]\n{content}"
        normalized_msg: dict[str, Any] = {"role": role, "content": content}
        if images:
            normalized_msg["images"] = images[:4]
        normalized.append(normalized_msg)
    return normalized


def _finish_reason(content: str) -> str:
    return "stop" if content.strip() else "length"


def _alternate_agent_ids(manager: Any, tried: set[str]) -> list[str]:
    """아직 시도하지 않은, pc_ollama 능력이 있는 온라인 agent 목록."""
    ids: list[str] = []
    for status in manager.list_agent_statuses():
        agent_id = str(status.get("agent_id") or "")
        caps = {str(cap).lower() for cap in status.get("capabilities") or []}
        if agent_id and agent_id not in tried and status.get("status") == "online" and "pc_ollama" in caps:
            ids.append(agent_id)
    return ids


async def _run_pc_ollama_chat(payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.pc_agent_manager import pc_agent_manager

    display_model = str(payload.get("model") or "").strip()
    ollama_model = _MODEL_MAP.get(display_model)
    if not ollama_model:
        raise HTTPException(status_code=404, detail=f"unsupported PC Ollama model: {display_model}")

    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    max_tokens = payload.get("max_tokens") or options.get("num_predict") or 2048
    tuned = _NON_THINKING_SAMPLING if ollama_model in _NON_THINKING_TARGETS else {}
    temperature = payload.get("temperature", options.get("temperature", tuned.get("temperature", 0.2)))
    sampling = {
        key: payload.get(key, options.get(key, tuned[key]))
        for key in ("top_p", "presence_penalty")
        if key in tuned or key in payload or key in options
    }
    timeout_seconds = float(payload.get("timeout_seconds") or 300)

    async def _execute(agent_id: str = "") -> dict[str, Any]:
        return await pc_agent_manager.execute_routed_command(
            command_type="ollama_chat",
            params={
                "model": ollama_model,
                "messages": _normalize_messages(payload.get("messages")),
                "temperature": temperature,
                **sampling,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
                "think": bool(payload.get("think", False)),
            },
            agent_id=agent_id,
            job_type="pc_ollama",
            required_capabilities=["pc_ollama"],
            queue_if_busy=True,
            wait_for_turn=True,
            queue_wait_timeout_seconds=min(timeout_seconds, 120.0),
            lease_ttl_seconds=int(timeout_seconds) + 30,
            command_timeout_seconds=timeout_seconds,
        )

    result = await _execute()
    # 라우터는 부하가 가장 낮은 PC 를 고른다. 그 PC 에 모델이 없어 오류가 나면
    # 모델을 가진 CEO PC 가 같은 슬롯에 붙어 있어도 쓰이지 못했다(2026-10-20 실측).
    tried: set[str] = set()
    attempts = 1
    while result.get("status") != "success" and attempts < _AGENT_ATTEMPT_LIMIT:
        failed_agent = str((result.get("lease") or {}).get("agent_id") or "")
        if not failed_agent or str(result.get("error_code") or "") in _NO_RETRY_ERROR_CODES:
            break
        tried.add(failed_agent)
        alternates = _alternate_agent_ids(pc_agent_manager, tried)
        if not alternates:
            break
        attempts += 1
        result = await _execute(alternates[0])
    if result.get("status") != "success":
        agent_failed = bool((result.get("lease") or {}).get("agent_id")) and (
            str(result.get("error_code") or "") not in _NO_RETRY_ERROR_CODES
        )
        detail = result.get("message") or result.get("detail") or result.get("error_code") or "PC Ollama route failed"
        peer_errors: list[str] = []
        peer = await _forward_to_peer(
            payload,
            detail,
            error_code=str(result.get("error_code") or ""),
            agent_failed=agent_failed,
            errors=peer_errors,
        )
        if peer is not None:
            return peer
        if peer_errors:
            detail = f"{detail} (peer: {'; '.join(peer_errors)})"
        raise HTTPException(status_code=503, detail=detail)

    data = (result.get("result") or {}).get("result") or {}
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}

    # 2026-09-21: Ollama 가 생성을 취소하면 HTTP 200 에 `done:false` 로 끝나고
    # eval_count 등 metrics 가 통째로 빠진다(서버 로그 `srv stop: cancel task`).
    # 이걸 성공으로 넘기면 잘린 답이 완성된 답으로 둔갑한다 — 실제로 그렇게
    # 오판했다(고친 커밋 8597f615). 취소는 실패로 올려 폴백 체인으로 넘긴다.
    if "done" in raw and not raw.get("done"):
        raise HTTPException(
            status_code=503,
            detail=f"PC Ollama generation cancelled (done=false, model={ollama_model})",
        )

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
