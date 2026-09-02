"""Secret-safe browser collection stage audit helpers.

The helpers in this module are intentionally small and dependency-light so
bank, delivery, supplier, tax, and future portal collectors can share the same
stage log schema without pulling in Browser Bridge internals.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

SITE_STAGE_LOG_SCHEMA = (
    "stage,status,recorded_at,elapsed_ms,error_code,reason,attempt_index,"
    "success_condition,failure_condition,timeout_ms"
)

_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "credential",
    "account_no",
    "registration_no",
    "business_no",
    "raw_html",
)


def _safe_stage_text(value: Any, *, limit: int = 160) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text.replace("\n", " ").replace("\r", " ").strip()[:limit]


def _is_secret_field(key: str) -> bool:
    normalized = str(key or "").lower()
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def append_site_stage_log(
    stage_logs: list[dict[str, str]],
    *,
    stage: str,
    status: str,
    started_at: float,
    logger: logging.Logger | None = None,
    event_name: str = "browser_collection_stage",
    error_code: str = "",
    reason: str = "",
    **fields: Any,
) -> dict[str, str]:
    """Append one secret-free browser collection audit stage.

    `started_at` must be a monotonic timestamp captured immediately before the
    stage action. All arbitrary fields are converted to short strings and
    sensitive keys are redacted before they can reach diagnostics or logs.
    """
    elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
    entry: dict[str, str] = {
        "stage": _safe_stage_text(stage, limit=80) or "unknown",
        "status": _safe_stage_text(status, limit=40) or "unknown",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "elapsed_ms": str(elapsed_ms),
    }
    if error_code:
        entry["error_code"] = _safe_stage_text(error_code, limit=120)
    if reason:
        entry["reason"] = _safe_stage_text(reason, limit=160)
    for key, value in fields.items():
        if value is None:
            continue
        safe_key = _safe_stage_text(key, limit=60)
        if not safe_key:
            continue
        if _is_secret_field(safe_key):
            safe_value = "[REDACTED]"
        else:
            safe_value = _safe_stage_text(value, limit=160)
        if safe_value:
            entry[safe_key] = safe_value
    stage_logs.append(entry)
    if logger is not None:
        logger.info(
            "%s stage=%s status=%s elapsed_ms=%s error_code=%s reason=%s",
            event_name,
            entry["stage"],
            entry["status"],
            entry["elapsed_ms"],
            entry.get("error_code", ""),
            entry.get("reason", ""),
        )
    return entry
