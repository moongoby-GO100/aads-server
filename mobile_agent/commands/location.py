"""GPS 위치 — termux-location."""
from __future__ import annotations
import json, subprocess
from typing import Any


async def get_location(params: dict[str, Any]) -> dict[str, Any]:
    provider = params.get("provider", "gps")
    result = subprocess.run(
        ["termux-location", "-p", provider],
        capture_output=True, text=True, timeout=30,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = {"raw": result.stdout}
    return {"status": "success" if result.returncode == 0 else "error", "data": data}
