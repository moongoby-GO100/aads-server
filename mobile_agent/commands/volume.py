"""볼륨 조절 — termux-volume."""
from __future__ import annotations
import subprocess
from typing import Any


async def set_volume(params: dict[str, Any]) -> dict[str, Any]:
    stream = params.get("stream", "music")
    volume = str(params.get("volume", 5))
    result = subprocess.run(
        ["termux-volume", stream, volume],
        capture_output=True, text=True, timeout=10,
    )
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"stream": stream, "volume": volume}}
