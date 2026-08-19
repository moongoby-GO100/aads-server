"""Permission policy for OHVIS managed browser actions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

Decision = Literal["allow", "ask", "deny"]
RiskLevel = Literal["low", "medium", "high", "critical"]

DENY_PATTERNS = (
    "show password",
    "reveal password",
    "copy password",
    "export password",
    "show secret",
    "reveal secret",
    "copy secret",
    "export secret",
    "autofill otp",
    "enter otp",
    "2fa code",
    "mfa code",
)

ASK_PATTERNS = (
    "payment",
    "pay ",
    "transfer",
    "refund",
    "delete",
    "remove",
    "cancel account",
    "close account",
    "change password",
    "post",
    "publish",
    "send message",
    "upload",
    "submit form",
    "purchase",
    "order",
)

SECRET_KEYS = re.compile(r"(password|passwd|secret|token|api[_-]?key|otp|mfa|2fa|authorization)", re.I)


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    risk_level: RiskLevel
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "decision": self.decision,
            "risk_level": self.risk_level,
            "reason": self.reason,
        }


def classify_browser_action(action_type: str, summary: str = "", payload: dict[str, Any] | None = None) -> PolicyDecision:
    """Classify an agent browser action before execution."""
    text = f"{action_type} {summary}".strip().lower()
    payload = payload or {}

    if any(pattern in text for pattern in DENY_PATTERNS):
        return PolicyDecision("deny", "critical", "secret_or_otp_disclosure_blocked")

    if _payload_requests_secret_disclosure(payload):
        return PolicyDecision("deny", "critical", "secret_payload_disclosure_blocked")

    if any(pattern in text for pattern in ASK_PATTERNS):
        return PolicyDecision("ask", "high", "human_approval_required_for_risky_action")

    if action_type.lower() in {"navigate", "read", "snapshot", "screenshot", "fill", "click"}:
        return PolicyDecision("allow", "low", "routine_browser_action")

    return PolicyDecision("ask", "medium", "unknown_action_requires_review")


def mask_sensitive_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***MASKED***" if SECRET_KEYS.search(str(key)) else mask_sensitive_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_sensitive_value(item) for item in value]
    return value


def _payload_requests_secret_disclosure(payload: dict[str, Any]) -> bool:
    intent = str(payload.get("intent") or payload.get("action") or "").lower()
    if any(word in intent for word in ("reveal", "show", "copy", "export")):
        return any(SECRET_KEYS.search(str(key)) for key in payload.keys())
    return False
