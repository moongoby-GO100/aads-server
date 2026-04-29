"""클립보드 — termux-clipboard-get, termux-clipboard-set."""
from __future__ import annotations
import subprocess
from typing import Any


async def clipboard_get(params: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["termux-clipboard-get"],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success", "data": {"text": result.stdout}}


async def clipboard_set(params: dict[str, Any]) -> dict[str, Any]:
    text = params.get("text", "")
    result = subprocess.run(
        ["termux-clipboard-set", text],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"stderr": result.stderr}}
