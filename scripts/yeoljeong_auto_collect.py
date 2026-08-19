#!/usr/bin/env python3
"""Run Yeoljeong delivery collection without the static UI."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.yeoljeong_finance_service import queue_delivery_sync, sync_delivery  # noqa: E402


KST = timezone(timedelta(hours=9))
DEFAULT_SERVICES = ("baemin", "coupangeats", "yogiyo", "ddangyo")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    today = datetime.now(KST).date()
    date_from = args.date_from or today.replace(day=1).isoformat()
    date_to = args.date_to or today.isoformat()
    return {
        "services": _split_csv(args.services) if args.services else list(DEFAULT_SERVICES),
        "business_id": args.business_id,
        "branch": args.branch,
        "date_from": date_from,
        "date_to": date_to,
        "all_businesses": args.business_id in {"all", "*", "__all__", "전체"},
        "sync_job_id": args.job_id or "",
        "browser_session_id": args.browser_session_id or "",
        "storage_state_path": args.storage_state_path or "",
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), list) else []
    return {
        "queued": bool(result.get("queued")),
        "job_id": result.get("job_id") or result.get("sync_job_id") or "",
        "synced_at": result.get("synced_at") or result.get("queued_at") or "",
        "business_id": result.get("business_id") or "",
        "branch": result.get("branch") or "",
        "date_from": result.get("date_from") or "",
        "date_to": result.get("date_to") or "",
        "totals": result.get("totals") or {"sales": 0, "settlements": 0, "reviews": 0},
        "summary": [
            {
                "service": item.get("service") or "",
                "business_id": item.get("business_id") or "",
                "branch": item.get("branch") or "",
                "status": item.get("status") or "",
                "error_code": item.get("error_code") or "",
                "counts": item.get("counts") or {"sales": 0, "settlements": 0, "reviews": 0},
                "run_id": item.get("run_id") or "",
                "account_id": item.get("account_id") or "",
                "message": item.get("message") or item.get("portal_message") or "",
            }
            for item in summary
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Yeoljeong delivery sales-channel collection.")
    parser.add_argument("--services", default=",".join(DEFAULT_SERVICES), help="Comma-separated services. Default: all delivery channels.")
    parser.add_argument("--business-id", default="all", help="Business id, or all for every registered branch.")
    parser.add_argument("--branch", default="전체", help="Branch name, or 전체 with --business-id all.")
    parser.add_argument("--date-from", default="", help="YYYY-MM-DD. Default: first day of current month.")
    parser.add_argument("--date-to", default="", help="YYYY-MM-DD. Default: today.")
    parser.add_argument("--browser-session-id", default="", help="Optional PC Agent browser session id.")
    parser.add_argument("--storage-state-path", default="", help="Optional Playwright storage state path.")
    parser.add_argument("--job-id", default="", help="Optional sync job id.")
    parser.add_argument("--queue-only", action="store_true", help="Create queued rows and exit without running collectors.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    user = {"email": "system@aads.local", "is_admin": True}
    payload = _payload(args)
    result = queue_delivery_sync(payload, user) if args.queue_only else sync_delivery(payload, user)
    print(json.dumps(_summary(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
