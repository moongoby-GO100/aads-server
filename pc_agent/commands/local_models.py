"""Safe local model queue handlers for CEO PC.

These handlers are intentionally conservative. They can inspect local runtime
state and run one explicit Ollama pull/test at a time when the server-side lease
allows it. Heavy media/document runtimes are returned as prepared stubs with
manual prerequisites instead of launching large parallel installers.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

_DEFAULT_TIMEOUT_SECONDS = 900.0
_MAX_TIMEOUT_SECONDS = 1800.0
_OLLAMA_BASE_URL = "http://127.0.0.1:11434"


def _timeout(params: Dict[str, Any], default: float = _DEFAULT_TIMEOUT_SECONDS) -> float:
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


def _safe_item(params: Dict[str, Any]) -> dict[str, Any]:
    item = params.get("item")
    return dict(item) if isinstance(item, dict) else {}


def _hf_cache_path(model: str) -> Path:
    cache_root = Path(os.getenv("HF_HOME") or Path.home() / ".cache" / "huggingface")
    safe = "models--" + str(model).replace("/", "--")
    return cache_root / "hub" / safe


def _manual_prerequisites(item: dict[str, Any]) -> list[str]:
    bridge = str(item.get("bridge") or "")
    if bridge == "local_image":
        return ["ComfyUI or Diffusers runtime", "CUDA PyTorch", "model weights in local model cache"]
    if bridge == "local_video":
        return ["ComfyUI or Diffusers video runtime", "CUDA PyTorch", "sufficient disk space for video weights"]
    if bridge == "local_music":
        return ["Stable Audio compatible runtime", "license review", "CUDA PyTorch"]
    if bridge == "local_3d":
        return ["Hunyuan3D runtime", "CUDA PyTorch", "mesh export dependencies"]
    if bridge == "local_document":
        return ["PaddleOCR/Tesseract runtime", "OCR language packs when needed"]
    if bridge in {"local_embedding", "local_rerank", "local_audio"}:
        return ["Python transformers runtime", "CUDA PyTorch or CPU fallback", "Hugging Face cache"]
    return ["runtime-specific dependencies"]


def _parse_ollama_list(output: str) -> set[str]:
    models: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0].upper() == "NAME":
            continue
        models.add(parts[0])
    return models


def _post_ollama_chat(model: str, timeout_seconds: float) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "AADS local model smoke test. Reply OK in Korean."}],
        "stream": False,
        "think": False,
        "options": {"num_predict": 64, "temperature": 0.1},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{_OLLAMA_BASE_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw or "{}")
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    content = str(message.get("content") or "")
    if not content:
        content = str(message.get("thinking") or message.get("reasoning") or "")
    return {"content": content, "raw_model": data.get("model") or model}


async def _ollama_status(item: dict[str, Any], params: Dict[str, Any]) -> dict[str, Any]:
    model = str(item.get("model") or "").strip()
    try:
        result = await asyncio.to_thread(_run_command, ["ollama", "list"], min(_timeout(params, 30.0), 60.0))
    except Exception as exc:
        return {"runtime": "ollama", "model": model, "installed": False, "error": str(exc)}
    installed = _parse_ollama_list(result.stdout or "")
    return {
        "runtime": "ollama",
        "model": model,
        "installed": model in installed,
        "available_models": sorted(installed),
        "exit_code": result.returncode,
        "error_output": (result.stderr or "")[-2000:],
    }


async def queue_status(params: Dict[str, Any]) -> Dict[str, Any]:
    item = _safe_item(params)
    if not item:
        return {
            "status": "success",
            "data": {
                "message": "local model manager command is installed",
                "python": sys.version.split()[0],
                "agent_capability": "local_model_manager",
            },
        }

    bridge = str(item.get("bridge") or "")
    if bridge == "pc_ollama":
        data = await _ollama_status(item, params)
    else:
        model = str(item.get("model") or "")
        cache_path = _hf_cache_path(model)
        data = {
            "runtime": bridge,
            "model": model,
            "installed": cache_path.exists(),
            "cache_path": str(cache_path),
            "manual_prerequisites": _manual_prerequisites(item),
            "automation_state": "prepared_stub",
        }
    return {"status": "success", "data": {"item": item, "local_status": data}}


async def install_test(params: Dict[str, Any]) -> Dict[str, Any]:
    item = _safe_item(params)
    if not item:
        return {"status": "error", "data": {"error": "item is required"}}

    action = str(params.get("action") or "prepare").strip().lower()
    allow_install = bool(params.get("allow_install"))
    allow_download = bool(params.get("allow_download"))
    bridge = str(item.get("bridge") or "")

    if bridge != "pc_ollama":
        return {
            "status": "success",
            "data": {
                "item": item,
                "action": action,
                "installed": False,
                "tested": False,
                "automation_state": "prepared_stub",
                "manual_prerequisites": _manual_prerequisites(item),
                "message": "heavy non-Ollama runtime install is prepared only; no large download was started",
            },
        }

    model = str(item.get("model") or "").strip()
    if not model:
        return {"status": "error", "data": {"error": "item.model is required"}}

    status_before = await _ollama_status(item, params)
    installed = bool(status_before.get("installed"))
    install_result: dict[str, Any] | None = None

    if action in {"install", "install_test"} and not installed:
        if not allow_install or not allow_download:
            return {
                "status": "success",
                "data": {
                    "item": item,
                    "installed": False,
                    "tested": False,
                    "status_before": status_before,
                    "automation_state": "queued_requires_explicit_install_download",
                    "message": "Ollama pull not started because allow_install and allow_download are required",
                },
            }
        try:
            result = await asyncio.to_thread(_run_command, ["ollama", "pull", model], _timeout(params, 900.0))
            install_result = {
                "exit_code": result.returncode,
                "output": (result.stdout or "")[-8000:],
                "error_output": (result.stderr or "")[-4000:],
            }
            installed = result.returncode == 0
        except subprocess.TimeoutExpired:
            return {"status": "error", "data": {"error": "ollama pull timeout", "item": item}}
        except Exception as exc:
            return {"status": "error", "data": {"error": str(exc), "item": item}}

    test_result: dict[str, Any] | None = None
    tested = False
    if action in {"test", "install_test"}:
        if not installed:
            return {
                "status": "success",
                "data": {
                    "item": item,
                    "installed": False,
                    "tested": False,
                    "status_before": status_before,
                    "install_result": install_result,
                    "automation_state": "not_installed",
                },
            }
        try:
            test_result = await asyncio.to_thread(_post_ollama_chat, model, min(_timeout(params, 120.0), 300.0))
            tested = True
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            test_result = {"error": f"ollama HTTP {exc.code}: {body[:500]}"}
        except Exception as exc:
            test_result = {"error": str(exc)}

    return {
        "status": "success",
        "data": {
            "item": item,
            "installed": installed,
            "tested": tested,
            "status_before": status_before,
            "install_result": install_result,
            "test_result": test_result,
            "automation_state": "installed_tested" if installed and tested else "prepared_or_installed",
        },
    }


async def media_job(params: Dict[str, Any]) -> Dict[str, Any]:
    item = _safe_item(params)
    job = params.get("job") if isinstance(params.get("job"), dict) else {}
    if not item or not job:
        return {"status": "error", "data": {"error": "item and job are required"}}
    return {
        "status": "success",
        "data": {
            "job": job,
            "item": item,
            "status": "prepared",
            "installed": False,
            "result_ready": False,
            "automation_state": "async_stub_prepared",
            "manual_prerequisites": _manual_prerequisites(item),
            "message": "local media job was prepared for PC runtime; generation is not marked complete",
        },
    }
