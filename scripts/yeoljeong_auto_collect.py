#!/usr/bin/env python3
"""Run Yeoljeong delivery collection without the static UI."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.yeoljeong_finance_service import queue_delivery_sync, sync_delivery  # noqa: E402


KST = timezone(timedelta(hours=9))
DEFAULT_SERVICES = ("baemin", "coupangeats", "yogiyo", "ddangyo")
BLOCKING_ERROR_CODES = {
    "CSV_UPLOAD_REQUIRED",
    "DDANGYO_NUMERIC_CAPTCHA_REQUIRED",
    "MISSING_CREDENTIALS",
    "PC_AGENT_SESSION_REQUIRED",
    "PORTAL_AUTH_CHALLENGE",
    "PORTAL_BLOCKED",
}
RETRYABLE_ERROR_CODES = {
    "ATTEMPT_TIMEOUT",
    "AUTHENTICATED_NO_ROWS",
    "BACKGROUND_SYNC_STALE",
    "COLLECTION_ALREADY_RUNNING",
    "EMPTY_SOURCE",
    "LOGIN_FORM_NOT_FOUND",
    "NO_PARSEABLE_ROWS",
    "PORTAL_TABLE_NOT_FOUND",
}


class _AttemptTimedOut(TimeoutError):
    pass


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


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _count_total(item: dict[str, Any]) -> int:
    counts = item.get("counts") if isinstance(item.get("counts"), dict) else {}
    return sum(int(counts.get(kind) or 0) for kind in ("sales", "settlements", "reviews"))


def _completion_state(summary: dict[str, Any]) -> dict[str, Any]:
    items = summary.get("summary") if isinstance(summary.get("summary"), list) else []
    if not items:
        return {
            "complete": False,
            "blocked": False,
            "retryable": True,
            "pending": 0,
            "blocking_codes": [],
            "retryable_codes": ["NO_SUMMARY"],
        }

    blocking_codes: set[str] = set()
    retryable_codes: set[str] = set()
    pending = 0
    completed = 0
    for item in items:
        status = str(item.get("status") or "").strip().lower()
        error_code = str(item.get("error_code") or "").strip().upper()
        count_total = _count_total(item)
        if status == "succeeded" or count_total > 0:
            completed += 1
            continue
        pending += 1
        if error_code in BLOCKING_ERROR_CODES:
            blocking_codes.add(error_code)
        elif error_code in RETRYABLE_ERROR_CODES:
            retryable_codes.add(error_code)
        elif error_code:
            retryable_codes.add(error_code)
        else:
            retryable_codes.add(status.upper() or "PENDING")

    return {
        "complete": completed == len(items),
        "blocked": bool(blocking_codes) and pending > 0,
        "retryable": bool(retryable_codes) or pending > 0,
        "pending": pending,
        "completed": completed,
        "total": len(items),
        "blocking_codes": sorted(blocking_codes),
        "retryable_codes": sorted(retryable_codes),
    }


def _sleep(seconds: int) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _run_sync(payload: dict[str, Any], user: dict[str, Any], *, queue_only: bool = False) -> dict[str, Any]:
    return queue_delivery_sync(payload, user) if queue_only else sync_delivery(payload, user)


def _timeout_result(payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    services = _split_csv(str(payload.get("services") or ",".join(DEFAULT_SERVICES)))
    return {
        "synced_at": datetime.now(KST).isoformat(timespec="seconds"),
        "business_id": payload.get("business_id") or "",
        "branch": payload.get("branch") or "",
        "date_from": payload.get("date_from") or "",
        "date_to": payload.get("date_to") or "",
        "totals": {"sales": 0, "settlements": 0, "reviews": 0},
        "summary": [
            {
                "service": service,
                "business_id": payload.get("business_id") or "",
                "branch": payload.get("branch") or "",
                "status": "failed",
                "error_code": "ATTEMPT_TIMEOUT",
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                "run_id": "",
                "account_id": "",
                "message": f"자동수집 단일 시도가 {timeout_seconds}초를 초과해 중단됐습니다. 루프가 다음 시도로 재개합니다.",
            }
            for service in services
        ],
    }


def _run_sync_with_timeout(payload: dict[str, Any], user: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    if timeout_seconds <= 0:
        return _run_sync(payload, user, queue_only=False)

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _handle_timeout(signum: int, frame: Any) -> None:
        raise _AttemptTimedOut()

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(timeout_seconds)
    try:
        return _run_sync(payload, user, queue_only=False)
    except _AttemptTimedOut:
        return _timeout_result(payload, timeout_seconds)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _run_until_complete(args: argparse.Namespace, user: dict[str, Any]) -> int:
    base_payload = _payload(args)
    max_attempts = max(0, int(args.max_attempts or 0))
    retry_seconds = max(1, int(args.retry_seconds or 1))
    blocked_retry_seconds = max(retry_seconds, int(args.blocked_retry_seconds or retry_seconds))
    success_sleep_seconds = max(1, int(args.success_sleep_seconds or retry_seconds))
    attempt_timeout_seconds = max(0, int(args.attempt_timeout_seconds or 0))
    attempt = 0

    while True:
        attempt += 1
        result = _run_sync_with_timeout(dict(base_payload), user, attempt_timeout_seconds)
        summary = _summary(result)
        state = _completion_state(summary)
        print(
            json.dumps(
                {
                    "loop": {
                        "attempt": attempt,
                        "state": state,
                        "next_retry_seconds": 0 if state["complete"] else (blocked_retry_seconds if state["blocked"] else retry_seconds),
                    },
                    **summary,
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        if state["complete"]:
            if not args.repeat_after_complete:
                return 0
            _sleep(success_sleep_seconds)
            attempt = 0
            continue
        if max_attempts and attempt >= max_attempts:
            return 2 if state["blocked"] else 1
        _sleep(blocked_retry_seconds if state["blocked"] else retry_seconds)


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
    parser.add_argument("--until-complete", action="store_true", help="Retry collection until every requested scope has data or succeeds.")
    parser.add_argument("--repeat-after-complete", action="store_true", help="After a complete cycle, sleep and start the next collection cycle.")
    parser.add_argument("--max-attempts", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_MAX_ATTEMPTS", 0), help="0 means unlimited attempts.")
    parser.add_argument("--retry-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_RETRY_SECONDS", 60))
    parser.add_argument("--blocked-retry-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_BLOCKED_RETRY_SECONDS", 180))
    parser.add_argument("--success-sleep-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_INTERVAL_SECONDS", 1800))
    parser.add_argument("--attempt-timeout-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_TIMEOUT_SECONDS", 1200))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.queue_only and args.until_complete:
        raise SystemExit("--queue-only and --until-complete cannot be used together")
    user = {"email": "system@aads.local", "is_admin": True}
    if args.until_complete:
        return _run_until_complete(args, user)
    payload = _payload(args)
    result = _run_sync(payload, user, queue_only=args.queue_only)
    print(json.dumps(_summary(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
