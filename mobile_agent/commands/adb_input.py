"""ADB 입력 제어 — input tap/swipe/text."""
from __future__ import annotations
import subprocess
from typing import Any


async def tap(params: dict[str, Any]) -> dict[str, Any]:
    x, y = params.get("x", 0), params.get("y", 0)
    result = subprocess.run(
        ["input", "tap", str(x), str(y)],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"x": x, "y": y}}


async def swipe(params: dict[str, Any]) -> dict[str, Any]:
    x1, y1 = params.get("x1", 0), params.get("y1", 0)
    x2, y2 = params.get("x2", 0), params.get("y2", 0)
    duration = params.get("duration_ms", 300)
    result = subprocess.run(
        ["input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"from": [x1, y1], "to": [x2, y2]}}


async def input_text(params: dict[str, Any]) -> dict[str, Any]:
    text = params.get("text", "")
    if not text:
        return {"status": "error", "data": {"error": "text 필수"}}
    result = subprocess.run(
        ["input", "text", text],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"text": text}}
