"""Shinhan EasyView Windows Collector harness.

This module is the server-side control harness for the PC-installed collector
flow. The actual browser work still runs on a Windows PC Agent; this harness
normalizes the bank-only request, enforces the financial-exclusive lane, runs
the security preflight, invokes the existing Yeoljeong bank collector, and
returns a completion contract suitable for LLMOps traces.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))

SHINHAN_EASYVIEW_LOGIN_URL = "https://bank.shinhan.com/rib/easy/index.jsp#210000000000"
SHINHAN_EASYVIEW_ORIGIN = "https://bank.shinhan.com"
SHINHAN_FORBIDDEN_LOGIN_ORIGINS = ("https://bizbank.shinhan.com",)
WINDOWS_COLLECTOR_CONTRACT_VERSION = "shinhan_windows_collector_v1"
FINANCIAL_EXCLUSIVE_JOB_TYPE = "financial_exclusive"
DEFAULT_BUSINESS_ID = "biz-mia"
DEFAULT_BRANCH = "열정국밥_미아점"
DEFAULT_BROWSER_TIMEOUT_SECONDS = 600
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 900


def _now_text() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _date_bounds(payload: dict[str, Any]) -> tuple[str, str]:
    today = datetime.now(KST).date()
    date_to = _clean_text(payload.get("date_to")) or today.isoformat()
    date_from = _clean_text(payload.get("date_from")) or today.replace(day=1).isoformat()
    return date_from, date_to


def build_shinhan_runtime_contract(agent_id: str = "") -> dict[str, Any]:
    return {
        "contract_version": WINDOWS_COLLECTOR_CONTRACT_VERSION,
        "bank": "shinhan_business",
        "entry_url": SHINHAN_EASYVIEW_LOGIN_URL,
        "allowed_origins": [SHINHAN_EASYVIEW_ORIGIN],
        "forbidden_login_origins": list(SHINHAN_FORBIDDEN_LOGIN_ORIGINS),
        "runtime": "windows_pc_agent",
        "job_type": FINANCIAL_EXCLUSIVE_JOB_TYPE,
        "required_browser_agent_id": _clean_text(agent_id),
        "lease_policy": {
            "exclusive": True,
            "resource_key": FINANCIAL_EXCLUSIVE_JOB_TYPE,
            "queue_if_busy": True,
            "wait_for_turn": True,
            "max_concurrency_per_agent": 1,
        },
        "state_machine": [
            "normalize_request",
            "security_program_preflight",
            "open_shinhan_easyview_login_url",
            "idpw_login",
            "account_query",
            "parse_transaction_table_or_download",
            "persist_bank_transactions",
            "verify_completion",
        ],
        "llmops": {
            "engine": "langgraph",
            "trace_provider": "ohvis_harness_trace",
            "langsmith_enabled": _env_bool("LANGCHAIN_TRACING_V2") or _env_bool("LANGSMITH_TRACING"),
            "langsmith_project": _clean_text(os.getenv("LANGSMITH_PROJECT")),
        },
        "success_contract": {
            "minimum_imported_rows": 1,
            "allow_verified_no_records": True,
            "required_evidence": [
                "security_preflight_ready",
                "login_url_is_shinhan_easyview",
                "financial_exclusive_lane",
                "bank_collection_result",
                "ledger_import_result",
            ],
        },
    }


def normalize_shinhan_windows_collector_request(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    date_from, date_to = _date_bounds(source)
    agent_id = _clean_text(
        source.get("browser_agent_id")
        or source.get("pc_agent_id")
        or os.getenv("YEOLJEONG_BANK_AUTO_COLLECT_AGENT_ID")
        or os.getenv("YEOLJEONG_BANK_BROWSER_AGENT_ID")
    )
    normalized = {
        "services": ["shinhan_business"],
        "service": "shinhan_business",
        "business_id": _clean_text(source.get("business_id"), DEFAULT_BUSINESS_ID),
        "branch": _clean_text(source.get("branch") or source.get("branch_id"), DEFAULT_BRANCH),
        "date_from": date_from,
        "date_to": date_to,
        "all_businesses": False,
        "bank_only": True,
        "skip_financial_accounts": False,
        "prefer_pc_agent": True,
        "require_pc_agent": True,
        "browser_agent_id": agent_id,
        "pc_agent_id": agent_id,
        "required_browser_agent_id": agent_id,
        "bank_browser_work_key": _clean_text(source.get("bank_browser_work_key") or source.get("browser_work_key")),
        "browser_session_id": _clean_text(source.get("browser_session_id")),
        "browser_preferred_port": source.get("browser_preferred_port"),
        "portal_url": SHINHAN_EASYVIEW_LOGIN_URL,
        "login_url": SHINHAN_EASYVIEW_LOGIN_URL,
        "auto_open_bank_browser": bool(source.get("auto_open_bank_browser", True)),
        "force_recreate_bank_browser": bool(source.get("force_recreate_bank_browser", True)),
        "close_portal_browser_on_complete": bool(source.get("close_portal_browser_on_complete", False)),
        "bank_browser_timeout_seconds": _as_int(
            source.get("bank_browser_timeout_seconds"),
            DEFAULT_BROWSER_TIMEOUT_SECONDS,
            DEFAULT_BROWSER_TIMEOUT_SECONDS,
            3600,
        ),
        "attempt_timeout_seconds": _as_int(
            source.get("attempt_timeout_seconds"),
            DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
            DEFAULT_BROWSER_TIMEOUT_SECONDS,
            7200,
        ),
        "operator_approved": bool(source.get("operator_approved", False)),
        "approved_input": _clean_text(source.get("approved_input")),
        "sync_job_id": _clean_text(source.get("sync_job_id"))
        or f"shinhan-windows-{uuid.uuid4().hex[:12]}",
    }
    if source.get("bank_account_id"):
        normalized["bank_account_id"] = _clean_text(source.get("bank_account_id"))
    if source.get("browser_preferred_port") in ("", None):
        normalized["browser_preferred_port"] = None
    return normalized


def _auto_collect_module() -> Any:
    import scripts.yeoljeong_auto_collect as auto_collect

    return auto_collect


def _run_security_preflight(payload: dict[str, Any], agent_id: str) -> dict[str, Any]:
    return _auto_collect_module()._financial_security_program_preflight(payload, agent_id=agent_id)


def _run_collection(payload: dict[str, Any], user: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    return _auto_collect_module()._run_sync_with_timeout(payload, user, timeout_seconds)


def _enqueue_collection(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    return _auto_collect_module()._enqueue_global_collection_queue(payload, user)


def _bank_collections(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("bank_collections")
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


def _verify_result(result: dict[str, Any]) -> dict[str, Any]:
    collections = _bank_collections(result)
    imported_rows = sum(int(item.get("imported_rows") or 0) for item in collections)
    collected_rows = sum(int(item.get("collected_rows") or 0) for item in collections)
    duplicate_rows = sum(int(item.get("duplicate_rows") or 0) for item in collections)
    statuses = sorted({str(item.get("status") or "") for item in collections if item.get("status")})
    error_codes = sorted({str(item.get("error_code") or "") for item in collections if item.get("error_code")})
    verified_no_records = bool(collections) and all(str(item.get("status") or "") == "no_records" for item in collections)
    completed = imported_rows > 0 or verified_no_records
    return {
        "completed": completed,
        "verified_no_records": verified_no_records,
        "imported_rows": imported_rows,
        "collected_rows": collected_rows,
        "duplicate_rows": duplicate_rows,
        "collection_count": len(collections),
        "statuses": statuses,
        "error_codes": error_codes,
        "completion_condition": "imported_rows_gt_0" if imported_rows > 0 else (
            "verified_no_records" if verified_no_records else "not_completed"
        ),
    }


def _node_validate(state: dict[str, Any]) -> dict[str, Any]:
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    payload = normalize_shinhan_windows_collector_request(request)
    errors: list[str] = []
    if not payload["browser_agent_id"]:
        errors.append("WINDOWS_COLLECTOR_AGENT_REQUIRED")
    if any(origin in _clean_text(request.get("portal_url") or request.get("login_url")) for origin in SHINHAN_FORBIDDEN_LOGIN_ORIGINS):
        state.setdefault("warnings", []).append("legacy_bizbank_origin_replaced")
    state.update(
        {
            "payload": payload,
            "runtime_contract": build_shinhan_runtime_contract(payload["browser_agent_id"]),
            "errors": errors,
            "status": "configuration_required" if errors else "validated",
        }
    )
    return state


def _node_security_preflight(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("errors"):
        return state
    payload = state["payload"]
    if state.get("skip_security_preflight"):
        state["security_preflight"] = {"checked": "skipped", "ready": True, "reason": "dry_run_or_test"}
        return state
    preflight = _run_security_preflight(payload, str(payload.get("browser_agent_id") or ""))
    state["security_preflight"] = preflight
    if not preflight.get("ready"):
        state["errors"] = [str(preflight.get("error_code") or "SHINHAN_SECURITY_PROGRAM_NOT_READY")]
        state["status"] = "preflight_failed"
    else:
        state["status"] = "preflight_passed"
    return state


def _node_collect(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("errors"):
        return state
    payload = state["payload"]
    user = state.get("user") if isinstance(state.get("user"), dict) else {"email": "system@aads.local", "is_admin": True}
    if state.get("dry_run"):
        state["collection_result"] = {"bank_collections": [], "dry_run": True}
        state["status"] = "dry_run"
        return state
    if state.get("queue_only"):
        state["collection_result"] = _enqueue_collection(payload, user)
        state["status"] = "queued"
        return state
    result = _run_collection(payload, user, int(payload.get("attempt_timeout_seconds") or DEFAULT_ATTEMPT_TIMEOUT_SECONDS))
    state["collection_result"] = result
    state["status"] = "collected"
    return state


def _node_verify(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("errors"):
        state["completion"] = {
            "completed": False,
            "completion_condition": "blocked_before_collection",
            "error_codes": state.get("errors") or [],
        }
        return state
    if state.get("queue_only"):
        state["completion"] = {
            "completed": False,
            "completion_condition": "queued_for_windows_collector",
            "error_codes": [],
        }
        return state
    result = state.get("collection_result") if isinstance(state.get("collection_result"), dict) else {}
    completion = _verify_result(result)
    state["completion"] = completion
    state["status"] = "completed" if completion["completed"] else "incomplete"
    return state


def _graph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except Exception:
        return False
    return True


def _run_langgraph(state: dict[str, Any]) -> dict[str, Any]:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(dict)
    graph.add_node("validate", _node_validate)
    graph.add_node("security_preflight", _node_security_preflight)
    graph.add_node("collect", _node_collect)
    graph.add_node("verify", _node_verify)
    graph.add_edge(START, "validate")
    graph.add_edge("validate", "security_preflight")
    graph.add_edge("security_preflight", "collect")
    graph.add_edge("collect", "verify")
    graph.add_edge("verify", END)
    return graph.compile().invoke(state)


def _run_sequential(state: dict[str, Any]) -> dict[str, Any]:
    for node in (_node_validate, _node_security_preflight, _node_collect, _node_verify):
        state = node(state)
    return state


def _record_harness_trace(state: dict[str, Any], *, started_at: float, error: str = "") -> None:
    if not _env_bool("YEOLJEONG_BANK_COLLECTOR_TRACE_ENABLED", True):
        return
    try:
        from app.services.ohvis_harness_trace import record_trace

        coro = record_trace(
            graph_run_id=str((state.get("payload") or {}).get("sync_job_id") or "shinhan-windows-collector"),
            project="FOOD",
            run_type="shinhan_windows_collector",
            input_summary={
                "agent_id": (state.get("payload") or {}).get("browser_agent_id", ""),
                "business_id": (state.get("payload") or {}).get("business_id", ""),
                "branch": (state.get("payload") or {}).get("branch", ""),
                "date_from": (state.get("payload") or {}).get("date_from", ""),
                "date_to": (state.get("payload") or {}).get("date_to", ""),
            },
            output_summary=state.get("completion") or state.get("status"),
            metadata={
                "component": "yeoljeong_bank_collector_harness",
                "runtime_contract": state.get("runtime_contract") or {},
                "security_preflight": state.get("security_preflight") or {},
            },
            latency_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
            provider="langgraph" if state.get("harness_engine") == "langgraph" else "internal",
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            loop.create_task(coro)
    except Exception:
        return


def run_shinhan_windows_collector_harness(
    payload: dict[str, Any] | None,
    user: dict[str, Any] | None = None,
    *,
    queue_only: bool = False,
    dry_run: bool = False,
    skip_security_preflight: bool = False,
) -> dict[str, Any]:
    """Run or queue the Shinhan Windows Collector with a fixed contract."""
    started_at = time.monotonic()
    state: dict[str, Any] = {
        "request": dict(payload or {}),
        "user": dict(user or {"email": "system@aads.local", "is_admin": True}),
        "queue_only": bool(queue_only),
        "dry_run": bool(dry_run),
        "skip_security_preflight": bool(skip_security_preflight),
        "started_at": _now_text(),
    }
    engine = "langgraph" if _graph_available() else "sequential"
    state["harness_engine"] = engine
    trace_error = ""
    try:
        state = _run_langgraph(state) if engine == "langgraph" else _run_sequential(state)
    except Exception as exc:
        trace_error = exc.__class__.__name__
        state["status"] = "failed"
        state["errors"] = [trace_error]
        state["completion"] = {
            "completed": False,
            "completion_condition": "harness_exception",
            "error_codes": [trace_error],
        }
        state["message"] = str(exc)[:300]
    finally:
        _record_harness_trace(state, started_at=started_at, error=trace_error)

    return {
        "status": state.get("status") or "unknown",
        "harness_engine": state.get("harness_engine") or engine,
        "runtime_contract": state.get("runtime_contract") or {},
        "payload": state.get("payload") or {},
        "security_preflight": state.get("security_preflight") or {},
        "collection_result": state.get("collection_result") or {},
        "completion": state.get("completion") or {},
        "errors": state.get("errors") or [],
        "warnings": state.get("warnings") or [],
        "started_at": state.get("started_at") or "",
        "finished_at": _now_text(),
    }
