"""PC Agent Ollama bridge commands."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import urllib.error
import urllib.request
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:11434"
_MAX_TIMEOUT_SECONDS = 1800.0


def _timeout(params: Dict[str, Any], default: float = 300.0) -> float:
    try:
        value = float(params.get("timeout_seconds", default) or default)
    except (TypeError, ValueError):
        value = default
    return max(1.0, min(value, _MAX_TIMEOUT_SECONDS))


def _run_command(args: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    kwargs: Dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def _post_json(url: str, payload: Dict[str, Any], timeout_seconds: float) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def _ok_from_process(result: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    return {
        "exit_code": result.returncode,
        "output": result.stdout[-12000:] if result.stdout else "",
        "error_output": result.stderr[-4000:] if result.stderr else "",
    }


def _ns_to_seconds(value: Any) -> float:
    try:
        return round(float(value or 0) / 1_000_000_000, 3)
    except (TypeError, ValueError):
        return 0.0


def _metrics(data: Dict[str, Any]) -> Dict[str, Any]:
    eval_count = int(data.get("eval_count") or 0)
    eval_seconds = _ns_to_seconds(data.get("eval_duration"))
    total_seconds = _ns_to_seconds(data.get("total_duration"))
    return {
        "total_seconds": total_seconds,
        "load_seconds": _ns_to_seconds(data.get("load_duration")),
        "prompt_tokens": int(data.get("prompt_eval_count") or 0),
        "prompt_seconds": _ns_to_seconds(data.get("prompt_eval_duration")),
        "output_tokens": eval_count,
        "output_seconds": eval_seconds,
        "tokens_per_second": round(eval_count / eval_seconds, 2) if eval_seconds > 0 else 0.0,
    }


async def version(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = await asyncio.to_thread(_run_command, ["ollama", "--version"], _timeout(params, 30.0))
        payload = _ok_from_process(result)
        return {"status": "success" if result.returncode == 0 else "error", "data": payload}
    except Exception as exc:
        logger.warning("ollama_version_failed: %s", exc)
        return {"status": "error", "data": {"error": str(exc)}}


async def list_models(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = await asyncio.to_thread(_run_command, ["ollama", "list"], _timeout(params, 30.0))
        payload = _ok_from_process(result)
        return {"status": "success" if result.returncode == 0 else "error", "data": payload}
    except Exception as exc:
        logger.warning("ollama_list_failed: %s", exc)
        return {"status": "error", "data": {"error": str(exc)}}


async def ps(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = await asyncio.to_thread(_run_command, ["ollama", "ps"], _timeout(params, 30.0))
        payload = _ok_from_process(result)
        return {"status": "success" if result.returncode == 0 else "error", "data": payload}
    except Exception as exc:
        logger.warning("ollama_ps_failed: %s", exc)
        return {"status": "error", "data": {"error": str(exc)}}


async def pull(params: Dict[str, Any]) -> Dict[str, Any]:
    model = str(params.get("model") or "").strip()
    if not model:
        return {"status": "error", "data": {"error": "model is required"}}

    if bool(params.get("background")):
        kwargs: Dict[str, Any] = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                ["ollama", "pull", model],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                **kwargs,
            )
            return {"status": "success", "data": {"model": model, "pid": proc.pid, "background": True}}
        except Exception as exc:
            logger.warning("ollama_pull_background_failed: %s", exc)
            return {"status": "error", "data": {"error": str(exc), "model": model}}

    try:
        result = await asyncio.to_thread(_run_command, ["ollama", "pull", model], _timeout(params, 900.0))
        payload = _ok_from_process(result)
        payload["model"] = model
        return {"status": "success" if result.returncode == 0 else "error", "data": payload}
    except subprocess.TimeoutExpired:
        return {"status": "error", "data": {"error": "ollama pull timeout", "model": model}}
    except Exception as exc:
        logger.warning("ollama_pull_failed: %s", exc)
        return {"status": "error", "data": {"error": str(exc), "model": model}}


async def chat(params: Dict[str, Any]) -> Dict[str, Any]:
    model = str(params.get("model") or "").strip()
    messages = params.get("messages") or []
    if not model:
        return {"status": "error", "data": {"error": "model is required"}}
    if not isinstance(messages, list):
        return {"status": "error", "data": {"error": "messages must be a list"}}

    options: Dict[str, Any] = {}
    if "temperature" in params:
        options["temperature"] = params.get("temperature")
    if "max_tokens" in params:
        options["num_predict"] = params.get("max_tokens")
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if "think" in params:
        payload["think"] = bool(params.get("think"))
    if options:
        payload["options"] = options

    base_url = str(params.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
    try:
        data = await asyncio.to_thread(
            _post_json,
            f"{base_url}/api/chat",
            payload,
            _timeout(params, 300.0),
        )
        message = data.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else ""
        if not content and isinstance(message, dict):
            content = message.get("thinking") or message.get("reasoning") or ""
        return {
            "status": "success",
            "data": {
                "model": data.get("model") or model,
                "content": content or "",
                "metrics": _metrics(data),
                "raw": data,
            },
        }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return {"status": "error", "data": {"error": f"ollama HTTP {exc.code}: {body[:500]}"}}
    except Exception as exc:
        logger.warning("ollama_chat_failed: %s", exc)
        return {"status": "error", "data": {"error": str(exc)}}


async def benchmark(params: Dict[str, Any]) -> Dict[str, Any]:
    model = str(params.get("model") or "gemma4:e4b").strip()
    prompts = params.get("prompts")
    if not isinstance(prompts, list) or not prompts:
        prompts = [
            "AADS의 PC Ollama bridge 용도를 한국어로 3문장으로 설명하세요.",
            "다음 작업을 실행 순서로 정리하세요: 모델 설치, 브릿지 연결, 속도 측정, 품질 평가.",
            "Python에서 리스트 중복 제거 함수를 짧게 작성하고 시간복잡도를 설명하세요.",
        ]

    results: list[Dict[str, Any]] = []
    for prompt in prompts:
        response = await chat({
            "model": model,
            "messages": [{"role": "user", "content": str(prompt)}],
            "temperature": params.get("temperature", 0.2),
            "max_tokens": params.get("max_tokens", 256),
            "timeout_seconds": params.get("timeout_seconds", 300),
            "base_url": params.get("base_url", _DEFAULT_BASE_URL),
        })
        item = {
            "prompt": str(prompt),
            "status": response.get("status"),
            "data": response.get("data", {}),
        }
        results.append(item)
        if response.get("status") != "success":
            break

    successful = [item for item in results if item.get("status") == "success"]
    metrics_items = [
        (item.get("data") or {}).get("metrics") or {}
        for item in successful
    ]
    total_seconds = sum(float(item.get("total_seconds") or 0) for item in metrics_items)
    output_tokens = sum(int(item.get("output_tokens") or 0) for item in metrics_items)
    output_seconds = sum(float(item.get("output_seconds") or 0) for item in metrics_items)
    summary = {
        "model": model,
        "success_count": len(successful),
        "total_count": len(results),
        "total_seconds": round(total_seconds, 3),
        "output_tokens": output_tokens,
        "tokens_per_second": round(output_tokens / output_seconds, 2) if output_seconds > 0 else 0.0,
    }
    status = "success" if len(successful) == len(results) else "error"
    return {"status": status, "data": {"summary": summary, "results": results}}
