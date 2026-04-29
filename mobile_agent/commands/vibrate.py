"""진동 — termux-vibrate."""
from __future__ import annotations
import subprocess
from typing import Any


async def do_vibrate(params: dict[str, Any]) -> dict[str, Any]:
    duration = str(params.get("duration_ms", 500))
    result = subprocess.run(
        ["termux-vibrate", "-d", duration],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"duration_ms": duration}}
