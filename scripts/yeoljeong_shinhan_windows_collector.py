#!/usr/bin/env python3
"""Run the Shinhan EasyView Windows Collector harness."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_HARNESS_PATH = ROOT / "app" / "services" / "yeoljeong_bank_collector_harness.py"
_HARNESS_SPEC = importlib.util.spec_from_file_location("yeoljeong_bank_collector_harness_cli", _HARNESS_PATH)
_HARNESS = importlib.util.module_from_spec(_HARNESS_SPEC)
assert _HARNESS_SPEC and _HARNESS_SPEC.loader
_HARNESS_SPEC.loader.exec_module(_HARNESS)

DEFAULT_ATTEMPT_TIMEOUT_SECONDS = _HARNESS.DEFAULT_ATTEMPT_TIMEOUT_SECONDS
DEFAULT_BRANCH = _HARNESS.DEFAULT_BRANCH
DEFAULT_BROWSER_TIMEOUT_SECONDS = _HARNESS.DEFAULT_BROWSER_TIMEOUT_SECONDS
DEFAULT_BUSINESS_ID = _HARNESS.DEFAULT_BUSINESS_ID
run_shinhan_windows_collector_harness = _HARNESS.run_shinhan_windows_collector_harness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Shinhan EasyView bank collection on a Windows PC Agent.")
    parser.add_argument("--business-id", default=DEFAULT_BUSINESS_ID)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--date-from", default="")
    parser.add_argument("--date-to", default="")
    parser.add_argument("--bank-account-id", default="")
    parser.add_argument("--browser-agent-id", default=os.getenv("YEOLJEONG_BANK_AUTO_COLLECT_AGENT_ID", ""))
    parser.add_argument("--browser-preferred-port", type=int, default=None)
    parser.add_argument("--bank-browser-work-key", default="")
    parser.add_argument("--bank-browser-timeout-seconds", type=int, default=DEFAULT_BROWSER_TIMEOUT_SECONDS)
    parser.add_argument("--attempt-timeout-seconds", type=int, default=DEFAULT_ATTEMPT_TIMEOUT_SECONDS)
    parser.add_argument("--keep-browser-open", action="store_true", default=True)
    parser.add_argument("--no-force-recreate-bank-browser", action="store_true")
    parser.add_argument("--queue-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-security-preflight", action="store_true")
    return parser


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "business_id": args.business_id,
        "branch": args.branch,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "bank_account_id": args.bank_account_id,
        "browser_agent_id": args.browser_agent_id,
        "browser_preferred_port": args.browser_preferred_port,
        "bank_browser_work_key": args.bank_browser_work_key,
        "bank_browser_timeout_seconds": args.bank_browser_timeout_seconds,
        "attempt_timeout_seconds": args.attempt_timeout_seconds,
        "force_recreate_bank_browser": not bool(args.no_force_recreate_bank_browser),
        "close_portal_browser_on_complete": not bool(args.keep_browser_open),
    }
    return {key: value for key, value in payload.items() if value not in ("", None)}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_shinhan_windows_collector_harness(
        _payload(args),
        {"email": "system@aads.local", "is_admin": True},
        queue_only=bool(args.queue_only),
        dry_run=bool(args.dry_run),
        skip_security_preflight=bool(args.skip_security_preflight),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    completion = result.get("completion") if isinstance(result.get("completion"), dict) else {}
    if args.queue_only or args.dry_run:
        return 0
    return 0 if completion.get("completed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
