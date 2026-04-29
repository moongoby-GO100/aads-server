"""알림 — termux-notification, termux-notification-list."""
from __future__ import annotations
import json, subprocess
from typing import Any


async def send_notification(params: dict[str, Any]) -> dict[str, Any]:
    title = params.get("title", "AADS")
    content = params.get("content", "")
    cmd = ["termux-notification", "-t", title, "-c", content]
    noti_id = params.get("id")
    if noti_id:
        cmd.extend(["--id", str(noti_id)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return {"status": "success" if result.returncode == 0 else "error",
            "data": {"stderr": result.stderr}}


async def list_notifications(params: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["termux-notification-list"],
        capture_output=True, text=True, timeout=10,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        data = result.stdout
    return {"status": "success", "data": {"notifications": data}}
