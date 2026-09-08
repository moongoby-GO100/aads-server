"""External LangSmith export gate — disabled by default.

Policy (PRD FR-010, task item 5): nothing leaves AADS unless *all* of
`LANGSMITH_TRACING`, `LANGSMITH_ENDPOINT`, `LANGSMITH_API_KEY` are set **and**
the masking policy passes. The gate is fail-closed: any missing input, any
unmasked secret, and the export is refused.

Secrets are never returned, logged, or serialized here — only booleans and the
endpoint host.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

MASKING_POLICY_VERSION = "mask_v1"
MASK = "«redacted»"

TRUTHY = {"1", "true", "yes", "on"}

# Patterns that must never leave the box. Ordered longest-prefix first so that a
# provider key is not partially matched by the generic bearer rule.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_oauth", re.compile(r"sk-ant-oat\d{2}-[A-Za-z0-9_\-]{8,}")),
    ("anthropic_api", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{8,}")),
    ("openai", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("google", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("langsmith", re.compile(r"lsv2_(?:pt|sk)_[A-Za-z0-9]{16,}")),
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{16,}")),
    ("pg_url", re.compile(r"postgres(?:ql)?://[^\s:@/]+:[^\s@]+@")),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("kr_phone", re.compile(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b")),
    ("kr_rrn", re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")),
)


def mask_text(value: Any) -> str:
    """Replace every known secret/PII shape with a redaction marker."""
    text = "" if value is None else str(value)
    for _name, pattern in _SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def find_secrets(value: Any) -> list[str]:
    """Names of the secret patterns still present in `value` (empty == clean)."""
    text = "" if value is None else str(value)
    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(text)]


def mask_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Mask every free-text field of a trace payload before it can be exported."""
    masked = dict(trace)
    for field in ("input_summary", "output_summary", "error", "name"):
        if masked.get(field) is not None:
            masked[field] = mask_text(masked[field])
    masked["masking_policy"] = MASKING_POLICY_VERSION
    return masked


def _env(name: str) -> Optional[str]:
    value = (os.getenv(name) or "").strip()
    return value or None


def export_gate_status() -> dict[str, Any]:
    """Whether external LangSmith export is permitted right now, and why not.

    Never includes the API key. `endpoint_host` is the host only, so a
    misconfiguration is diagnosable without leaking a full signed URL.
    """
    tracing_raw = _env("LANGSMITH_TRACING") or _env("LANGCHAIN_TRACING_V2")
    tracing_on = (tracing_raw or "").lower() in TRUTHY
    endpoint = _env("LANGSMITH_ENDPOINT")
    api_key = _env("LANGSMITH_API_KEY")

    blockers: list[str] = []
    if not tracing_on:
        blockers.append("LANGSMITH_TRACING_not_enabled")
    if not endpoint:
        blockers.append("LANGSMITH_ENDPOINT_missing")
    if not api_key:
        blockers.append("LANGSMITH_API_KEY_missing")

    endpoint_host = None
    if endpoint:
        try:
            endpoint_host = urlparse(endpoint).hostname
        except ValueError:
            blockers.append("LANGSMITH_ENDPOINT_unparseable")

    return {
        "enabled": not blockers,
        "default": "disabled",
        "blockers": blockers,
        "tracing_enabled": tracing_on,
        "endpoint_configured": bool(endpoint),
        "endpoint_host": endpoint_host,
        "api_key_present": bool(api_key),
        "masking_policy": MASKING_POLICY_VERSION,
    }


def prepare_export(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Mask and gate a batch. Returns what *would* be sent; never sends.

    Sending is deliberately not implemented until the CEO approves the data
    policy — this keeps the gate auditable while guaranteeing zero egress.
    """
    gate = export_gate_status()
    if not gate["enabled"]:
        return {
            "exported": 0,
            "blocked": True,
            "reason": "export_gate_closed",
            "gate": gate,
            "payload": [],
        }

    masked = [mask_trace(trace) for trace in traces]
    leaked: list[str] = []
    for trace in masked:
        for field in ("input_summary", "output_summary", "error", "name"):
            leaked.extend(find_secrets(trace.get(field)))
    if leaked:
        return {
            "exported": 0,
            "blocked": True,
            "reason": "masking_policy_failed",
            "leaked_patterns": sorted(set(leaked)),
            "gate": gate,
            "payload": [],
        }

    return {
        "exported": 0,
        "blocked": True,
        "reason": "egress_not_implemented_pending_data_policy_approval",
        "gate": gate,
        "payload": masked,
    }
