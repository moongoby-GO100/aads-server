"""카메라 촬영 — termux-camera-photo."""
from __future__ import annotations
import base64, os, subprocess
from typing import Any


async def take_photo(params: dict[str, Any]) -> dict[str, Any]:
    camera_id = str(params.get("camera_id", 0))
    path = params.get("path", "/data/data/com.termux/files/home/photo.jpg")
    result = subprocess.run(
        ["termux-camera-photo", "-c", camera_id, path],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return {"status": "error", "data": {"error": result.stderr}}
    if os.path.exists(path):
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return {"status": "success", "data": {"path": path, "base64": b64[:500] + "...(truncated)"}}
    return {"status": "success", "data": {"path": path}}
