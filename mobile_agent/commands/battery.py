"""배터리 상태 — termux-battery-status."""
from __future__ import annotations
import json, subprocess
from typing import Any


async def get_battery_status(params: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["termux-battery-status"],
        capture_output=True, text=True, timeout=10,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = {"raw": result.stdout}
    return {"status": "success" if result.returncode == 0 else "error", "data": data}
