"""배달 채널 자동수집 트리거 — 내부 monitor-key 인증으로 /sync 호출.

사용: python3 scripts/trigger_delivery_sync.py ddangyo,coupangeats [biz-mia]
      python3 scripts/trigger_delivery_sync.py --service baemin --mode full_backfill --from 2026-01-01 --to 2026-08-25 --max-orders 300 --branch all
키는 컨테이너 환경변수에서만 읽고 출력하지 않는다 (R-KEY).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

API = "http://localhost:8080/api/v1/yeoljeong-finance/sync"


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trigger FOOD delivery sync")
    parser.add_argument("legacy_services", nargs="?", help="comma separated services")
    parser.add_argument("legacy_business_id", nargs="?", default="biz-mia")
    parser.add_argument("legacy_branch", nargs="?", default="열정국밥_미아점")
    parser.add_argument("--service", action="append", dest="services", help="service name; repeatable or comma separated")
    parser.add_argument("--mode", default="", choices=("", "full_backfill"))
    parser.add_argument("--from", dest="date_from", default="")
    parser.add_argument("--to", dest="date_to", default="")
    parser.add_argument("--max-orders", type=int, default=300)
    parser.add_argument("--max-reviews", type=int, default=300)
    parser.add_argument("--window-days", type=int, default=1)
    parser.add_argument("--max-backfill-runs", type=int, default=1)
    parser.add_argument("--business-id", default="")
    parser.add_argument("--branch", default="")
    return parser.parse_args(argv)


def _services(parsed: argparse.Namespace) -> list[str]:
    raw = parsed.services or ([parsed.legacy_services] if parsed.legacy_services else [])
    services: list[str] = []
    for item in raw:
        services.extend([part.strip() for part in str(item or "").split(",") if part.strip()])
    return services


def main() -> int:
    key = os.getenv("AADS_MONITOR_KEY", "").strip()
    if not key:
        print("NO_MONITOR_KEY")
        return 2

    parsed = _args(sys.argv[1:])
    services = _services(parsed)
    business_id = parsed.business_id or parsed.legacy_business_id
    branch = parsed.branch or parsed.legacy_branch

    body = {
        "services": services,
        "business_id": business_id,
        "branch": branch,
        "background": True,
        "force_recreate_portal_sessions": True,
    }
    if parsed.mode:
        body["mode"] = parsed.mode
    if parsed.date_from:
        body["date_from"] = parsed.date_from
    if parsed.date_to:
        body["date_to"] = parsed.date_to
    if parsed.max_orders:
        body["max_orders"] = parsed.max_orders
    if parsed.max_reviews:
        body["max_reviews"] = parsed.max_reviews
    if parsed.window_days:
        body["window_days"] = parsed.window_days
    if parsed.max_backfill_runs:
        body["max_backfill_runs"] = parsed.max_backfill_runs
    if str(branch).strip().lower() in {"all", "*", "__all__", "전체"}:
        body["all_businesses"] = True
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
