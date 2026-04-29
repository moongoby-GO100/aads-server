"""텍스트 음성 변환 — termux-tts-speak."""
from __future__ import annotations
import subprocess
from typing import Any


async def speak(params: dict[str, Any]) -> dict[str, Any]:
    text = params.get("text", "")
    if not text:
        return {"status": "error", "data": {"error": "text 필수"}}
    cmd = ["termux-tts-speak", text]
    lang = params.get("language")
    if lang:
        cmd.extend(["-l", lang])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"stderr": result.stderr}}
