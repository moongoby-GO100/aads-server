"""전화 — termux-telephony-call, termux-call-log."""
from __future__ import annotations
import json, subprocess
from typing import Any


async def make_call(params: dict[str, Any]) -> dict[str, Any]:
    number = params.get("number", "")
    if not number:
        return {"status": "error", "data": {"error": "number 필수"}}
    result = subprocess.run(
        ["termux-telephony-call", number],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"stderr": result.stderr}}


async def get_call_log(params: dict[str, Any]) -> dict[str, Any]:
    limit = str(params.get("limit", 10))
    result = subprocess.run(
        ["termux-call-log", "-l", limit],
        capture_output=True, text=True, timeout=10,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = result.stdout
    return {"status": "success", "data": {"calls": data}}
