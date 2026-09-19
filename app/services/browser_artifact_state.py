"""User-facing Smart Browser artifact state derived from persisted task evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_LEARNING_STATES = {"learned", "reused", "rediscover", "approval_required", "idle"}
_FRESHNESS_STATES = {"CURRENT", "STALE", "CONFLICT", "UNAVAILABLE", "NOT_APPLICABLE"}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def build_browser_artifact_status(
    *, task: Mapping[str, Any], frame: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = _dict(task.get("result"))
    metadata = _dict((frame or {}).get("metadata"))
    knowledge = _dict(result.get("site_knowledge")) or _dict(metadata.get("site_knowledge"))
    freshness = _dict(result.get("freshness_gate")) or _dict(metadata.get("freshness_gate"))
    learning_state = str(knowledge.get("learning_state") or "idle")
    if learning_state not in _LEARNING_STATES:
        learning_state = "idle"
    freshness_status = str(freshness.get("status") or "NOT_APPLICABLE").upper()
    if freshness_status not in _FRESHNESS_STATES:
        freshness_status = "UNAVAILABLE"
    latest_error = str(task.get("error") or "")
    for event in events:
        payload = _dict(event.get("payload"))
        if not latest_error and event.get("event_type") in {"failed", "error"}:
            latest_error = str(payload.get("user_message") or payload.get("reason") or "작업이 중단되었습니다.")
    status = str(task.get("status") or "")
    approval_required = bool(task.get("requires_approval")) or status in {"approval_required", "auth_required"}
    retry_action = freshness.get("retry_action") or result.get("retry_action")
    if not retry_action and (latest_error or freshness_status != "CURRENT"):
        retry_action = {"action": "retry_browser_task", "task_id": str(task.get("id") or "")}
    source = str(metadata.get("source") or result.get("execution_source") or "")
    actor = {
        "self_hosted_playwright": "Browser",
        "pc_agent_browser_screenshot": "Windows PC",
        "human_gateway": "Human",
    }.get(source, "Browser" if source else "대기")
    return {
        "current_url": str((frame or {}).get("current_url") or task.get("target_url") or ""),
        "execution_actor": actor,
        "current_step": str((frame or {}).get("current_step") or task.get("current_step") or "대기 중"),
        "learning_state": learning_state,
        "freshness_status": freshness_status,
        "evidence_count": len(knowledge.get("evidence_refs") or freshness.get("facts") or []),
        "approval_required": approval_required,
        "last_error": latest_error,
        "retry_action": retry_action,
        "can_retry": bool(retry_action),
    }
