"""ADB 스크린샷 — screencap."""
from __future__ import annotations
import base64, os, subprocess
from typing import Any


async def take_screenshot(params: dict[str, Any]) -> dict[str, Any]:
    path = params.get("path", "/data/data/com.termux/files/home/screenshot.png")
    result = subprocess.run(
        ["screencap", "-p", path],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return {"status": "error", "data": {"error": result.stderr}}
    if os.path.exists(path):
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return {"status": "success", "data": {"path": path, "size": size, "base64_preview": b64[:500] + "..."}}
    return {"status": "success", "data": {"path": path}}
