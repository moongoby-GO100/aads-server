#!/usr/bin/env python3
"""Run Yeoljeong delivery collection without the static UI."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.yeoljeong_finance_service import (  # noqa: E402
    CANONICAL_BRANCHES,
    FINANCIAL_TRANSACTION_SERVICES,
    _read,
    _write_delivery_collection_statuses,
    collect_bank_account_transactions,
    list_bank_accounts,
    list_accounts as list_platform_accounts,
    list_collection_status,
    queue_delivery_sync,
    sync_delivery,
    sync_financial_transactions,
)


KST = timezone(timedelta(hours=9))
DEFAULT_SERVICES = ("baemin", "coupangeats", "yogiyo", "ddangyo")
DELIVERY_RECORD_TYPES = ("sales", "settlements", "reviews", "ads")


def _empty_delivery_counts() -> dict[str, int]:
    return {kind: 0 for kind in DELIVERY_RECORD_TYPES}
BLOCKING_ERROR_CODES = {
    "BANK_ACTION_REQUIRED",
    "BANK_BROWSER_AUTH_CHALLENGE_DETECTED",
    "BANK_BROWSER_OPERATOR_ACTION_REQUIRED",
    "BANK_BROWSER_SESSION_REQUIRED",
    "BANK_CERTIFICATE_PASSWORD_REQUIRED",
    "BANK_CONNECTOR_NOT_CONFIGURED",
    "CSV_UPLOAD_REQUIRED",
    "DDANGYO_NUMERIC_CAPTCHA_REQUIRED",
    "BANK_ACCOUNT_PASSWORD_REQUIRED",
    "MISSING_CREDENTIALS",
    "PC_AGENT_SESSION_REQUIRED",
    "PORTAL_AUTH_CHALLENGE",
    "PORTAL_BLOCKED",
}
RETRYABLE_ERROR_CODES = {
    "ATTEMPT_TIMEOUT",
    "AUTHENTICATED_NO_ROWS",
    "BANK_BROWSER_PAGE_ERROR",
    "BANK_BROWSER_PC_AGENT_TIMEOUT",
    "BANK_BROWSER_SESSION_NOT_FOUND",
    "BACKGROUND_SYNC_STALE",
    "COLLECTION_ALREADY_RUNNING",
    "CDP_NOT_READY",
    "COMMAND_TIMEOUT",
    "EMPTY_SOURCE",
    "LOGIN_FORM_NOT_FOUND",
    "NO_PARSEABLE_ROWS",
    "PARSE_FAILED",
    "PC_AGENT_COLLECTOR_TIMEOUTERROR",
    "PC_AGENT_SESSION_NOT_FOUND",
    "PC_AGENT_WRONG_PORTAL_SESSION",
    "PORTAL_TABLE_NOT_FOUND",
    "RUNTIME_EVALUATE_TIMEOUT",
    "STALE_TARGET",
}
SESSION_RECREATE_ERROR_CODES = {
    "PC_AGENT_COLLECTOR_TIMEOUTERROR",
    "PC_AGENT_SESSION_REQUIRED",
    "PC_AGENT_SESSION_NOT_FOUND",
    "PC_AGENT_WRONG_PORTAL_SESSION",
}
BANK_SESSION_RECREATE_ERROR_CODES = {
    "BANK_BROWSER_PAGE_ERROR",
    "BANK_BROWSER_PC_AGENT_TIMEOUT",
    "BANK_BROWSER_SESSION_NOT_FOUND",
    "CDP_NOT_READY",
    "COMMAND_TIMEOUT",
    "PC_AGENT_SESSION_REQUIRED",
    "PC_AGENT_SESSION_NOT_FOUND",
    "RUNTIME_EVALUATE_TIMEOUT",
    "STALE_TARGET",
}


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
        "mode": str(getattr(args, "mode", "") or ""),
        "max_orders": int(getattr(args, "max_orders", 300) or 300),
        "max_reviews": int(getattr(args, "max_reviews", 300) or 300),
        "sync_job_id": args.job_id or "",
        "browser_session_id": args.browser_session_id or "",
        "storage_state_path": args.storage_state_path or "",
        "force_recreate_portal_sessions": bool(args.force_recreate_sessions),
        "close_portal_browser_on_complete": not bool(args.keep_browser_open),
        "auto_open_bank_browser": not bool(getattr(args, "no_auto_open_bank_browser", False)),
        "browser_agent_id": str(getattr(args, "browser_agent_id", "") or ""),
        "browser_preferred_port": getattr(args, "browser_preferred_port", None),
        "bank_browser_timeout_seconds": int(getattr(args, "bank_browser_timeout_seconds", 90) or 90),
        "force_recreate_bank_browser": bool(getattr(args, "force_recreate_bank_browser", False)),
        "operator_approved": bool(getattr(args, "operator_approved", False)),
        "approved_input": str(getattr(args, "approved_input", "") or ""),
        "skip_financial_accounts": bool(getattr(args, "skip_financial_accounts", False)),
        "bank_only": bool(getattr(args, "bank_only", False)),
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), list) else []
    normalized = {
        "queued": bool(result.get("queued")),
        "job_id": result.get("job_id") or result.get("sync_job_id") or "",
        "synced_at": result.get("synced_at") or result.get("queued_at") or "",
        "business_id": result.get("business_id") or "",
        "branch": result.get("branch") or "",
        "date_from": result.get("date_from") or "",
        "date_to": result.get("date_to") or "",
        "totals": result.get("totals") or _empty_delivery_counts(),
        "summary": [
            {
                "service": item.get("service") or "",
                "business_id": item.get("business_id") or "",
                "branch": item.get("branch") or "",
                "status": item.get("status") or "",
                "error_code": item.get("error_code") or "",
                "counts": item.get("counts") or _empty_delivery_counts(),
                "run_id": item.get("run_id") or "",
                "account_id": item.get("account_id") or "",
                "message": item.get("message") or item.get("portal_message") or "",
            }
            for item in summary
        ],
    }
    if isinstance(result.get("bank_collections"), list):
        normalized["bank_collections"] = result["bank_collections"]
    if isinstance(result.get("bank_totals"), dict):
        normalized["bank_totals"] = result["bank_totals"]
    return normalized


def _branch_id_for_bank_scope(branch: str, business_id: str = "") -> str:
    branch_text = str(branch or "").strip()
    if not branch_text or branch_text in {"all", "*", "__all__", "전체"}:
        return ""
    for item in CANONICAL_BRANCHES:
        if branch_text in {str(item.get("id") or ""), str(item.get("name") or "")}:
            if business_id and str(item.get("businessId") or "") != business_id:
                return ""
            return str(item.get("id") or "")
    return branch_text


def _bank_accounts_for_payload(payload: dict[str, Any], user: dict[str, Any]) -> list[dict[str, Any]]:
    business_id = str(payload.get("business_id") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    all_businesses = bool(payload.get("all_businesses")) or business_id in {"all", "*", "__all__", "전체"}
    wanted_business = None if all_businesses else business_id or None
    wanted_branch = "" if all_businesses else _branch_id_for_bank_scope(branch, business_id)
    accounts = list_bank_accounts(
        user,
        wanted_business,
        branch_id=wanted_branch or None,
        status="active",
    )
    return [account for account in accounts if account.get("auto_sync") is not False]


def _bank_collection_error_code(collection: dict[str, Any]) -> str:
    error_code = str(collection.get("error_code") or "").strip().upper()
    if error_code:
        return error_code
    connector_status = str(collection.get("connector_status") or "").strip().upper()
    if connector_status == "NOT_CONFIGURED":
        return "BANK_CONNECTOR_NOT_CONFIGURED"
    if connector_status == "ACTION_REQUIRED":
        return "BANK_ACTION_REQUIRED"
    return ""


def _financial_collection_error_code(item: dict[str, Any]) -> str:
    error_code = str(item.get("error_code") or "").strip().upper()
    if error_code:
        return error_code
    status = str(item.get("status") or "").strip().lower()
    if status == "connector_not_configured":
        return "BANK_CONNECTOR_NOT_CONFIGURED"
    if status == "credential_required":
        return "MISSING_CREDENTIALS"
    if status == "upload_required":
        return "CSV_UPLOAD_REQUIRED"
    if status in {"action_required", "blocked"}:
        return "BANK_ACTION_REQUIRED"
    return ""


def _collect_bank_accounts(payload: dict[str, Any], user: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        accounts = _bank_accounts_for_payload(payload, user)
    except Exception as exc:
        return [
            {
                "service": "bank",
                "status": "failed",
                "error_code": "BANK_ACCOUNT_LIST_FAILED",
                "message": f"은행계좌 목록 조회 실패: {str(exc)[:200]}",
                "counts": {"transactions": 0},
            }
        ]

    for account in accounts:
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            continue
        collect_payload = {
            "business_id": str(account.get("business_id") or payload.get("business_id") or ""),
            "branch_id": str(account.get("branch_id") or ""),
            "date_from": payload.get("date_from") or "",
            "date_to": payload.get("date_to") or "",
            "source": str(account.get("connection_type") or "manual"),
            "transactions": [],
            "browser_session_id": str(payload.get("bank_browser_session_id") or payload.get("browser_session_id") or ""),
            "auto_open_browser": bool(payload.get("auto_open_bank_browser", True)),
            "browser_agent_id": str(payload.get("browser_agent_id") or ""),
            "browser_preferred_port": payload.get("browser_preferred_port") or None,
            "browser_timeout_seconds": int(payload.get("bank_browser_timeout_seconds") or 90),
            "force_recreate_browser": bool(payload.get("force_recreate_bank_browser")),
        }
        try:
            result = collect_bank_account_transactions(account_id, collect_payload, user)
            collection = dict(result.get("collection") or {})
            imported_rows = int(collection.get("imported_rows") or 0)
            duplicate_rows = int(collection.get("duplicate_rows") or 0)
            collected_rows = int(collection.get("collected_rows") or imported_rows or 0)
            results.append(
                {
                    "service": "bank",
                    "bank_account_id": account_id,
                    "business_id": collection.get("business_id") or collect_payload["business_id"],
                    "branch_id": collection.get("branch_id") or collect_payload["branch_id"],
                    "status": collection.get("status") or "",
                    "connector_status": collection.get("connector_status") or "",
                    "connection_type": collection.get("connection_type") or account.get("connection_type") or "",
                    "error_code": _bank_collection_error_code(collection),
                    "counts": {"transactions": imported_rows},
                    "collected_rows": collected_rows,
                    "imported_rows": imported_rows,
                    "duplicate_rows": duplicate_rows,
                    "message": collection.get("message") or "",
                    "diagnostics": collection.get("diagnostics") or {},
                    "last_collected_at": collection.get("last_collected_at") or "",
                }
            )
        except Exception as exc:
            results.append(
                {
                    "service": "bank",
                    "bank_account_id": account_id,
                    "business_id": collect_payload["business_id"],
                    "branch_id": collect_payload["branch_id"],
                    "status": "failed",
                    "connector_status": "FAILED",
                    "connection_type": str(account.get("connection_type") or ""),
                    "error_code": "BANK_COLLECT_FAILED",
                    "counts": {"transactions": 0},
                    "collected_rows": 0,
                    "imported_rows": 0,
                    "duplicate_rows": 0,
                    "message": f"은행계좌 자동수집 실패: {str(exc)[:200]}",
                    "last_collected_at": str(account.get("last_synced_at") or ""),
                }
            )
    return results


def _platform_financial_accounts_for_payload(payload: dict[str, Any], user: dict[str, Any]) -> list[dict[str, Any]]:
    business_id = str(payload.get("business_id") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    all_businesses = bool(payload.get("all_businesses")) or business_id in {"all", "*", "__all__", "전체"}
    rows = list_platform_accounts(user, None if all_businesses else business_id or None)
    accounts: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        service = str(row.get("service") or "").strip()
        if service not in FINANCIAL_TRANSACTION_SERVICES:
            continue
        if row.get("auto_sync") is False:
            continue
        if not all_businesses:
            row_branch = str(row.get("branch") or "").strip()
            if branch and row_branch and row_branch != branch:
                continue
        accounts.append(row)
    return accounts


def _collect_platform_financial_accounts(payload: dict[str, Any], user: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        accounts = _platform_financial_accounts_for_payload(payload, user)
    except Exception as exc:
        return [
            {
                "service": "bank",
                "source_ledger": "platform_accounts",
                "status": "failed",
                "error_code": "BANK_ACCOUNT_LIST_FAILED",
                "message": f"기존 은행/카드 연동 목록 조회 실패: {str(exc)[:200]}",
                "counts": {"transactions": 0},
            }
        ]

    results: list[dict[str, Any]] = []
    for account in accounts:
        service = str(account.get("service") or "").strip()
        account_id = str(account.get("id") or "").strip()
        if not service or not account_id:
            continue
        sync_payload = {
            "services": [service],
            "account_id": account_id,
            "business_id": account.get("business_id") or payload.get("business_id") or "",
            "branch": account.get("branch") or payload.get("branch") or "",
            "date_from": payload.get("date_from") or "",
            "date_to": payload.get("date_to") or "",
        }
        try:
            sync_result = sync_financial_transactions(sync_payload, user)
            for item in sync_result.get("summary") if isinstance(sync_result.get("summary"), list) else []:
                imported_rows = int(item.get("imported_rows") or 0)
                status = str(item.get("status") or "")
                results.append(
                    {
                        "service": service,
                        "source_ledger": "platform_accounts",
                        "bank_account_id": account_id,
                        "business_id": sync_result.get("business_id") or sync_payload["business_id"],
                        "branch": sync_result.get("branch") or sync_payload["branch"],
                        "status": status,
                        "connection_type": account.get("collection_mode") or "",
                        "error_code": _financial_collection_error_code(item),
                        "counts": {"transactions": imported_rows},
                        "collected_rows": imported_rows,
                        "imported_rows": imported_rows,
                        "duplicate_rows": 0,
                        "message": item.get("message") or "",
                        "last_collected_at": sync_result.get("synced_at") or "",
                    }
                )
        except Exception as exc:
            results.append(
                {
                    "service": service,
                    "source_ledger": "platform_accounts",
                    "bank_account_id": account_id,
                    "business_id": sync_payload["business_id"],
                    "branch": sync_payload["branch"],
                    "status": "failed",
                    "connection_type": str(account.get("collection_mode") or ""),
                    "error_code": "BANK_COLLECT_FAILED",
                    "counts": {"transactions": 0},
                    "collected_rows": 0,
                    "imported_rows": 0,
                    "duplicate_rows": 0,
                    "message": f"기존 은행/카드 연동 자동수집 실패: {str(exc)[:200]}",
                    "last_collected_at": str(account.get("last_synced_at") or ""),
                }
            )
    return results


def _attach_financial_collections(
    summary: dict[str, Any],
    payload: dict[str, Any],
    user: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("skip_financial_accounts"):
        return summary
    bank_collections = _collect_bank_accounts(payload, user)
    bank_collections.extend(_collect_platform_financial_accounts(payload, user))
    if bank_collections:
        summary["bank_collections"] = bank_collections
        summary["bank_totals"] = {
            "accounts": len(bank_collections),
            "imported_rows": sum(int(item.get("imported_rows") or 0) for item in bank_collections),
            "duplicate_rows": sum(int(item.get("duplicate_rows") or 0) for item in bank_collections),
            "collected_rows": sum(int(item.get("collected_rows") or 0) for item in bank_collections),
        }
    return summary


def _run_collectors(payload: dict[str, Any], user: dict[str, Any], *, queue_only: bool = False) -> dict[str, Any]:
    if payload.get("bank_only"):
        base = {
            "queued": False,
            "job_id": str(payload.get("sync_job_id") or ""),
            "synced_at": datetime.now(KST).isoformat(timespec="seconds"),
            "business_id": payload.get("business_id") or "",
            "branch": payload.get("branch") or "",
            "date_from": payload.get("date_from") or "",
            "date_to": payload.get("date_to") or "",
            "totals": _empty_delivery_counts(),
            "summary": [],
        }
        return _attach_financial_collections(base, payload, user)
    summary = _summary(_run_sync(payload, user, queue_only=queue_only))
    if queue_only:
        return summary
    return _attach_financial_collections(summary, payload, user)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _count_total(item: dict[str, Any]) -> int:
    counts = item.get("counts") if isinstance(item.get("counts"), dict) else {}
    return sum(int(counts.get(kind) or 0) for kind in DELIVERY_RECORD_TYPES)


def _bank_count_total(item: dict[str, Any]) -> int:
    counts = item.get("counts") if isinstance(item.get("counts"), dict) else {}
    return int(item.get("imported_rows") or counts.get("transactions") or 0)


def _completion_state(summary: dict[str, Any]) -> dict[str, Any]:
    items = summary.get("summary") if isinstance(summary.get("summary"), list) else []
    bank_items = summary.get("bank_collections") if isinstance(summary.get("bank_collections"), list) else []
    if not items and not bank_items:
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

    for item in bank_items:
        status = str(item.get("status") or "").strip().lower()
        error_code = str(item.get("error_code") or "").strip().upper()
        count_total = _bank_count_total(item)
        if status in {"completed", "no_records"} or count_total > 0:
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
            retryable_codes.add(status.upper() or "BANK_PENDING")

    return {
        "complete": completed == len(items) + len(bank_items),
        "blocked": bool(blocking_codes) and pending > 0,
        "retryable": bool(retryable_codes) or pending > 0,
        "pending": pending,
        "completed": completed,
        "total": len(items) + len(bank_items),
        "blocking_codes": sorted(blocking_codes),
        "retryable_codes": sorted(retryable_codes),
    }


def _should_force_recreate_portal_sessions(state: dict[str, Any]) -> bool:
    codes = {
        str(code or "").strip().upper()
        for key in ("retryable_codes", "blocking_codes")
        for code in (state.get(key) if isinstance(state.get(key), list) else [])
    }
    return bool(codes & SESSION_RECREATE_ERROR_CODES)


def _should_force_recreate_bank_browser(state: dict[str, Any]) -> bool:
    codes = {
        str(code or "").strip().upper()
        for key in ("retryable_codes", "blocking_codes")
        for code in (state.get(key) if isinstance(state.get(key), list) else [])
    }
    return bool(codes & BANK_SESSION_RECREATE_ERROR_CODES)


def _initial_force_recreate_portal_sessions(payload: dict[str, Any], user: dict[str, Any]) -> bool:
    if payload.get("force_recreate_portal_sessions"):
        return True
    try:
        rows = list_collection_status(user, None)
    except Exception:
        return False

    requested_services = {
        str(service or "").strip()
        for service in (payload.get("services") if isinstance(payload.get("services"), list) else DEFAULT_SERVICES)
        if str(service or "").strip()
    }
    business_id = str(payload.get("business_id") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    all_businesses = bool(payload.get("all_businesses")) or business_id in {"all", "*", "__all__", "전체"} or branch == "전체"
    latest_by_scope: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        service = str(row.get("service") or "").strip()
        row_business_id = str(row.get("business_id") or "").strip()
        row_branch = str(row.get("branch") or "").strip()
        if requested_services and service not in requested_services:
            continue
        if not all_businesses:
            if business_id and row_business_id != business_id:
                continue
            if branch and row_branch != branch:
                continue
        key = (service, row_business_id, row_branch)
        latest_by_scope.setdefault(key, row)

    state = _completion_state({"summary": list(latest_by_scope.values())})
    return _should_force_recreate_portal_sessions(state)


def _sleep(seconds: int) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _run_sync(payload: dict[str, Any], user: dict[str, Any], *, queue_only: bool = False) -> dict[str, Any]:
    return queue_delivery_sync(payload, user) if queue_only else sync_delivery(payload, user)


def _payload_services(payload: dict[str, Any]) -> list[str]:
    services = payload.get("services")
    if isinstance(services, list):
        return [str(service).strip() for service in services if str(service).strip()]
    if isinstance(services, str):
        return _split_csv(services)
    return list(DEFAULT_SERVICES)


def _child_collect_argv(payload: dict[str, Any]) -> list[str]:
    services = _payload_services(payload)
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--services",
        ",".join(services) or ",".join(DEFAULT_SERVICES),
        "--business-id",
        str(payload.get("business_id") or "all"),
        "--branch",
        str(payload.get("branch") or "전체"),
    ]
    for key, flag in (
        ("date_from", "--date-from"),
        ("date_to", "--date-to"),
        ("mode", "--mode"),
        ("browser_session_id", "--browser-session-id"),
        ("storage_state_path", "--storage-state-path"),
        ("sync_job_id", "--job-id"),
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            argv.extend([flag, value])
    for key, flag, default in (
        ("max_orders", "--max-orders", 300),
        ("max_reviews", "--max-reviews", 300),
        ("bank_browser_timeout_seconds", "--bank-browser-timeout-seconds", 90),
    ):
        value = int(payload.get(key) or 0)
        if value and value != default:
            argv.extend([flag, str(value)])
    if payload.get("force_recreate_portal_sessions"):
        argv.append("--force-recreate-sessions")
    if payload.get("close_portal_browser_on_complete") is False:
        argv.append("--keep-browser-open")
    if payload.get("auto_open_bank_browser") is False:
        argv.append("--no-auto-open-bank-browser")
    if payload.get("force_recreate_bank_browser"):
        argv.append("--force-recreate-bank-browser")
    if payload.get("browser_agent_id"):
        argv.extend(["--browser-agent-id", str(payload.get("browser_agent_id") or "")])
    if payload.get("browser_preferred_port"):
        argv.extend(["--browser-preferred-port", str(payload.get("browser_preferred_port") or "")])
    if payload.get("skip_financial_accounts"):
        argv.append("--skip-financial-accounts")
    if payload.get("bank_only"):
        argv.append("--bank-only")
    argv.append("--child-no-timeout")
    return argv


def _parse_child_collect_stdout(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        raise ValueError("child collector returned empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _mark_timeout_statuses(payload: dict[str, Any], timeout_seconds: int, attempt_started_at: str) -> None:
    services = set(_payload_services(payload))
    business_id = str(payload.get("business_id") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    all_businesses = bool(payload.get("all_businesses")) or business_id in {"all", "*", "__all__", "전체"} or branch == "전체"
    now_text = datetime.now(KST).isoformat(timespec="seconds")
    try:
        statuses = _read("delivery_collection_status")
    except Exception:
        return

    changed_rows: list[dict[str, Any]] = []
    for row in statuses if isinstance(statuses, list) else []:
        if str(row.get("status") or "").strip() not in {"queued", "running"}:
            continue
        service = str(row.get("service") or "").strip()
        if services and service not in services:
            continue
        if not all_businesses:
            if business_id and str(row.get("business_id") or "").strip() != business_id:
                continue
            if branch and str(row.get("branch") or "").strip() != branch:
                continue
        started_at = str(row.get("started_at") or row.get("created_at") or row.get("updated_at") or "")
        if started_at and started_at < attempt_started_at:
            continue
        row["status"] = "failed"
        row["raw_status"] = "timeout"
        row["error_code"] = "ATTEMPT_TIMEOUT"
        row["message"] = f"자동수집 단일 시도가 {timeout_seconds}초를 초과해 중단됐습니다. 다음 시도에서 재개합니다."
        row["finished_at"] = now_text
        row["updated_at"] = now_text
        row.setdefault("counts", _empty_delivery_counts())
        changed_rows.append(row)

    for row in changed_rows:
        _write_delivery_collection_statuses(statuses, row)


def _latest_status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    services = set(_payload_services(payload))
    business_id = str(payload.get("business_id") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    all_businesses = bool(payload.get("all_businesses")) or business_id in {"all", "*", "__all__", "전체"} or branch == "전체"
    try:
        statuses = _read("delivery_collection_status")
    except Exception:
        statuses = []

    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in statuses if isinstance(statuses, list) else []:
        service = str(row.get("service") or "").strip()
        if services and service not in services:
            continue
        row_business_id = str(row.get("business_id") or "").strip()
        row_branch = str(row.get("branch") or "").strip()
        if not all_businesses:
            if business_id and row_business_id != business_id:
                continue
            if branch and row_branch != branch:
                continue
        key = (service, row_business_id, row_branch)
        if key not in latest or str(row.get("updated_at") or "") > str(latest[key].get("updated_at") or ""):
            latest[key] = row

    items = []
    totals = _empty_delivery_counts()
    for row in latest.values():
        counts = row.get("counts") if isinstance(row.get("counts"), dict) else _empty_delivery_counts()
        for kind in DELIVERY_RECORD_TYPES:
            totals[kind] += int(counts.get(kind) or 0)
        items.append(
            {
                "service": row.get("service") or "",
                "business_id": row.get("business_id") or "",
                "branch": row.get("branch") or "",
                "status": row.get("status") or "",
                "error_code": row.get("error_code") or "",
                "counts": counts,
                "run_id": row.get("id") or "",
                "account_id": row.get("account_id") or "",
                "message": row.get("message") or "",
            }
        )

    return {
        "queued": False,
        "job_id": str(payload.get("sync_job_id") or ""),
        "synced_at": datetime.now(KST).isoformat(timespec="seconds"),
        "business_id": payload.get("business_id") or "",
        "branch": payload.get("branch") or "",
        "date_from": payload.get("date_from") or "",
        "date_to": payload.get("date_to") or "",
        "totals": totals,
        "summary": items,
    }


def _timeout_result(payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    services = _payload_services(payload)
    return {
        "synced_at": datetime.now(KST).isoformat(timespec="seconds"),
        "business_id": payload.get("business_id") or "",
        "branch": payload.get("branch") or "",
        "date_from": payload.get("date_from") or "",
        "date_to": payload.get("date_to") or "",
        "totals": _empty_delivery_counts(),
        "summary": [
            {
                "service": service,
                "business_id": payload.get("business_id") or "",
                "branch": payload.get("branch") or "",
                "status": "failed",
                "error_code": "ATTEMPT_TIMEOUT",
                "counts": _empty_delivery_counts(),
                "run_id": "",
                "account_id": "",
                "message": f"자동수집 단일 시도가 {timeout_seconds}초를 초과해 중단됐습니다. 루프가 다음 시도로 재개합니다.",
            }
            for service in services
        ],
    }


def _run_child_collect_with_timeout(payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    attempt_started_at = datetime.now(KST).isoformat(timespec="seconds")
    try:
        completed = subprocess.run(
            _child_collect_argv(payload),
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _mark_timeout_statuses(payload, timeout_seconds, attempt_started_at)
        status_summary = _latest_status_summary(payload)
        if status_summary["summary"]:
            return status_summary
        return _summary(_timeout_result(payload, timeout_seconds))

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()[-1000:]
        raise RuntimeError(f"자동수집 자식 프로세스 실패: exit={completed.returncode} stderr={stderr_tail}")
    return _summary(_parse_child_collect_stdout(completed.stdout))


def _merge_attempt_summaries(payload: dict[str, Any], summaries: list[dict[str, Any]]) -> dict[str, Any]:
    totals = _empty_delivery_counts()
    merged_summary: list[dict[str, Any]] = []
    bank_collections: list[dict[str, Any]] = []
    for item in summaries:
        item_totals = item.get("totals") if isinstance(item.get("totals"), dict) else {}
        for kind in DELIVERY_RECORD_TYPES:
            totals[kind] += int(item_totals.get(kind) or 0)
        if isinstance(item.get("summary"), list):
            merged_summary.extend(item["summary"])
        if isinstance(item.get("bank_collections"), list):
            bank_collections.extend(item["bank_collections"])

    result: dict[str, Any] = {
        "queued": False,
        "job_id": str(payload.get("sync_job_id") or ""),
        "synced_at": datetime.now(KST).isoformat(timespec="seconds"),
        "business_id": payload.get("business_id") or "",
        "branch": payload.get("branch") or "",
        "date_from": payload.get("date_from") or "",
        "date_to": payload.get("date_to") or "",
        "totals": totals,
        "summary": merged_summary,
    }
    if bank_collections:
        result["bank_collections"] = bank_collections
        result["bank_totals"] = {
            "accounts": len(bank_collections),
            "imported_rows": sum(int(item.get("imported_rows") or 0) for item in bank_collections),
            "duplicate_rows": sum(int(item.get("duplicate_rows") or 0) for item in bank_collections),
            "collected_rows": sum(int(item.get("collected_rows") or 0) for item in bank_collections),
        }
    return result


def _run_sync_with_timeout(payload: dict[str, Any], user: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    if timeout_seconds <= 0:
        return _run_collectors(payload, user, queue_only=False)

    services = _payload_services(payload)
    if len(services) <= 1:
        return _run_child_collect_with_timeout(payload, timeout_seconds)

    summaries: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout_seconds
    for service in services:
        remaining_seconds = int(deadline - time.monotonic())
        if remaining_seconds <= 0:
            service_payload = dict(payload)
            service_payload["services"] = [service]
            service_payload["skip_financial_accounts"] = True
            _mark_timeout_statuses(
                service_payload,
                timeout_seconds,
                datetime.now(KST).isoformat(timespec="seconds"),
            )
            summaries.append(_summary(_timeout_result(service_payload, timeout_seconds)))
            continue
        service_payload = dict(payload)
        service_payload["services"] = [service]
        service_payload["skip_financial_accounts"] = True
        summaries.append(_run_child_collect_with_timeout(service_payload, remaining_seconds))
    merged = _merge_attempt_summaries(payload, summaries)
    return _attach_financial_collections(merged, payload, user)


def _run_until_complete(args: argparse.Namespace, user: dict[str, Any]) -> int:
    base_payload = _payload(args)
    max_attempts = max(0, int(args.max_attempts or 0))
    retry_seconds = max(1, int(args.retry_seconds or 1))
    blocked_retry_seconds = max(retry_seconds, int(args.blocked_retry_seconds or retry_seconds))
    success_sleep_seconds = max(1, int(args.success_sleep_seconds or retry_seconds))
    attempt_timeout_seconds = max(0, int(args.attempt_timeout_seconds or 0))
    retry_blocked = bool(getattr(args, "retry_blocked", False))
    attempt = 0
    force_recreate_portal_next = _initial_force_recreate_portal_sessions(base_payload, user)
    force_recreate_bank_next = bool(base_payload.get("force_recreate_bank_browser"))

    while True:
        attempt += 1
        attempt_payload = dict(base_payload)
        if force_recreate_portal_next:
            attempt_payload["force_recreate_portal_sessions"] = True
        if force_recreate_bank_next:
            attempt_payload["force_recreate_bank_browser"] = True
        summary = _run_sync_with_timeout(attempt_payload, user, attempt_timeout_seconds)
        state = _completion_state(summary)
        can_retry_blocked_with_recreate = (
            state["blocked"]
            and (
                (
                    _should_force_recreate_portal_sessions(state)
                    and not bool(attempt_payload.get("force_recreate_portal_sessions"))
                )
                or (
                    _should_force_recreate_bank_browser(state)
                    and not bool(attempt_payload.get("force_recreate_bank_browser"))
                )
            )
        )
        will_stop_on_blocked = state["blocked"] and not retry_blocked and not can_retry_blocked_with_recreate
        next_retry_seconds = 0 if state["complete"] or will_stop_on_blocked else (
            blocked_retry_seconds if state["blocked"] else retry_seconds
        )
        print(
            json.dumps(
                {
                    "loop": {
                        "attempt": attempt,
                        "state": state,
                        "force_recreate_portal_sessions": bool(
                            attempt_payload.get("force_recreate_portal_sessions")
                        ),
                        "force_recreate_bank_browser": bool(
                            attempt_payload.get("force_recreate_bank_browser")
                        ),
                        "next_retry_seconds": next_retry_seconds,
                        "stop_on_blocked": bool(will_stop_on_blocked),
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
        if will_stop_on_blocked:
            return 0 if bool(getattr(args, "exit_zero_on_blocked", False)) else 2
        if max_attempts and attempt >= max_attempts:
            return 2 if state["blocked"] else 1
        force_recreate_portal_next = (
            bool(base_payload.get("force_recreate_portal_sessions"))
            or _should_force_recreate_portal_sessions(state)
        )
        force_recreate_bank_next = (
            bool(base_payload.get("force_recreate_bank_browser"))
            or _should_force_recreate_bank_browser(state)
        )
        _sleep(blocked_retry_seconds if state["blocked"] else retry_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Yeoljeong delivery sales-channel collection.")
    parser.add_argument("--services", default=",".join(DEFAULT_SERVICES), help="Comma-separated services. Default: all delivery channels.")
    parser.add_argument("--business-id", default="all", help="Business id, or all for every registered branch.")
    parser.add_argument("--branch", default="전체", help="Branch name, or 전체 with --business-id all.")
    parser.add_argument("--date-from", default="", help="YYYY-MM-DD. Default: first day of current month.")
    parser.add_argument("--date-to", default="", help="YYYY-MM-DD. Default: today.")
    parser.add_argument("--mode", default="", choices=("", "full_backfill"), help="Collection mode. full_backfill enables Baemin order-history/review/ad backfill.")
    parser.add_argument("--max-orders", type=int, default=300, help="Baemin full_backfill order cap per branch, 1-300.")
    parser.add_argument("--max-reviews", type=int, default=300, help="Baemin full_backfill review cap per branch, 1-300.")
    parser.add_argument("--browser-session-id", default="", help="Optional PC Agent browser session id.")
    parser.add_argument("--browser-agent-id", default="", help="Optional PC Agent id for bank browser auto-open.")
    parser.add_argument("--browser-preferred-port", type=int, default=None, help="Optional preferred CDP port for bank browser auto-open.")
    parser.add_argument("--bank-browser-timeout-seconds", type=int, default=_env_int("YEOLJEONG_BANK_BROWSER_TIMEOUT_SECONDS", 90), help="Bank browser automation timeout per account.")
    parser.add_argument("--storage-state-path", default="", help="Optional Playwright storage state path.")
    parser.add_argument(
        "--force-recreate-sessions",
        action="store_true",
        help="Recreate each delivery portal and bank work-key browser session before collecting.",
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Keep PC Agent portal browser sessions open after each collection attempt.",
    )
    parser.add_argument(
        "--no-auto-open-bank-browser",
        action="store_true",
        help="Disable automatic PC Agent bank corporate-page browser opening.",
    )
    parser.add_argument(
        "--force-recreate-bank-browser",
        action="store_true",
        help="Recreate only the bank Browser Bridge work-key session. Default is to reuse the existing bank browser.",
    )
    parser.add_argument("--job-id", default="", help="Optional sync job id.")
    parser.add_argument("--operator-approved", action="store_true", help="Allow one operator-approved challenge input for the current run.")
    parser.add_argument("--approved-input", default="", help="Write-only operator-approved challenge input for the current run.")
    parser.add_argument("--queue-only", action="store_true", help="Create queued rows and exit without running collectors.")
    parser.add_argument("--skip-financial-accounts", action="store_true", help="Skip bank and financial account collection.")
    parser.add_argument("--bank-only", action="store_true", help="Run only bank account collection without delivery portal collection.")
    parser.add_argument("--until-complete", action="store_true", help="Retry collection until every requested scope has data or succeeds.")
    parser.add_argument("--repeat-after-complete", action="store_true", help="After a complete cycle, sleep and start the next collection cycle.")
    parser.add_argument("--retry-blocked", action="store_true", help="Keep retrying manual action-required states such as captcha or portal blocking.")
    parser.add_argument("--exit-zero-on-blocked", action="store_true", help="Exit 0 when a terminal manual action-required state is reached.")
    parser.add_argument("--max-attempts", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_MAX_ATTEMPTS", 0), help="0 means unlimited attempts.")
    parser.add_argument("--retry-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_RETRY_SECONDS", 60))
    parser.add_argument("--blocked-retry-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_BLOCKED_RETRY_SECONDS", 180))
    parser.add_argument("--success-sleep-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_INTERVAL_SECONDS", 1800))
    parser.add_argument("--attempt-timeout-seconds", type=int, default=_env_int("YEOLJEONG_AUTO_COLLECT_TIMEOUT_SECONDS", 1200))
    parser.add_argument("--child-no-timeout", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.queue_only and args.until_complete:
        raise SystemExit("--queue-only and --until-complete cannot be used together")
    user = {"email": "system@aads.local", "is_admin": True}
    if args.until_complete:
        return _run_until_complete(args, user)
    payload = _payload(args)
    if args.queue_only or args.child_no_timeout or args.attempt_timeout_seconds <= 0:
        result = _run_collectors(payload, user, queue_only=args.queue_only)
    else:
        result = _run_sync_with_timeout(payload, user, int(args.attempt_timeout_seconds))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
