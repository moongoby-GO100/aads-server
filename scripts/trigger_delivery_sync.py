"""배달 채널 자동수집 트리거 — 내부 monitor-key 인증으로 /sync 호출.

사용: python3 scripts/trigger_delivery_sync.py ddangyo,coupangeats [biz-mia]
키는 컨테이너 환경변수에서만 읽고 출력하지 않는다 (R-KEY).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

API = "http://localhost:8080/api/v1/yeoljeong-finance/sync"


def main() -> int:
    key = os.getenv("AADS_MONITOR_KEY", "").strip()
    if not key:
        print("NO_MONITOR_KEY")
        return 2

    services = [s for s in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if s]
    business_id = sys.argv[2] if len(sys.argv) > 2 else "biz-mia"
    branch = sys.argv[3] if len(sys.argv) > 3 else "열정국밥_미아점"

    body = {
        "services": services,
        "business_id": business_id,
        "branch": branch,
        "background": True,
        "force_recreate_portal_sessions": True,
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-monitor-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {exc}")
        return 1

    print(f"HTTP_OK queued={payload.get('queued')} job_id={payload.get('job_id', '')}")
    for item in payload.get("summary") or []:
        print(f"  {item.get('service')}: {item.get('status')} {item.get('error_code') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
