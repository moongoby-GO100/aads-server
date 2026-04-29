"""SMS 전송/읽기 — termux-sms-send, termux-sms-list."""
from __future__ import annotations
import json, subprocess
from typing import Any


async def sms_send(params: dict[str, Any]) -> dict[str, Any]:
    number = params.get("number", "")
    body = params.get("body", "")
    if not number or not body:
        return {"status": "error", "data": {"error": "number, body 필수"}}
    result = subprocess.run(
        ["termux-sms-send", "-n", number, body],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"stderr": result.stderr}}


async def sms_list(params: dict[str, Any]) -> dict[str, Any]:
    limit = str(params.get("limit", 10))
    result = subprocess.run(
        ["termux-sms-list", "-l", limit],
        capture_output=True, text=True, timeout=10,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = result.stdout
    return {"status": "success", "data": {"messages": data}}
