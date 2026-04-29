"""WiFi 정보 — termux-wifi-connectioninfo, termux-wifi-scaninfo."""
from __future__ import annotations
import json, subprocess
from typing import Any


async def wifi_info(params: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["termux-wifi-connectioninfo"],
        capture_output=True, text=True, timeout=10,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = {"raw": result.stdout}
    return {"status": "success", "data": data}


async def wifi_scan(params: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["termux-wifi-scaninfo"],
        capture_output=True, text=True, timeout=15,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = {"raw": result.stdout}
    return {"status": "success", "data": {"networks": data}}
