"""DB-backed managed browser task lifecycle service."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.core.db_pool import get_pool
from app.services.browser_permission_policy import classify_browser_action, mask_sensitive_value
from app.services.managed_browser import normalize_work_key


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
PUSH_STATUSES = {"approval_required", "auth_required", "completed", "failed"}
logger = logging.getLogger(__name__)


def _tenant_uuid(tenant_id: str) -> uuid.UUID:
    return uuid.UUID(str(tenant_id))


def _nullable_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    return uuid.UUID(str(value))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _task_to_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("id", "tenant_id", "session_id", "approval_request_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    for key in ("created_at", "updated_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    item["result"] = _json_dict(item.get("result"))
    return item


async def create_browser_task(
    *,
    tenant_id: str,
    user_id: str,
    work_key: str,
    target_url: str,
    session_id: str | None = None,
    current_step: str = "",
) -> dict[str, Any]:
    normalized_work_key = normalize_work_key(work_key)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO browser_tasks (tenant_id, user_id, session_id, work_key, target_url, status, current_step)
            VALUES ($1, $2, $3, $4, $5, 'queued', $6)
            RETURNING *
            """,
            _tenant_uuid(tenant_id),
            user_id,
            _nullable_uuid(session_id),
            normalized_work_key,
            target_url,
            current_step,
        )
        await append_browser_task_event(
            conn=conn,
            tenant_id=tenant_id,
            task_id=str(row["id"]),
            event_type="created",
            payload={
                "work_key": normalized_work_key,
                "target_url": target_url,
                "session_id": session_id or "",
                "current_step": current_step,
            },
        )
    return _task_to_dict(row)


def _should_cleanup_browser_session(task: dict[str, Any]) -> bool:
    if str(task.get("status") or "") not in TERMINAL_STATUSES:
        return False
    result = _json_dict(task.get("result"))
    if result.get("keep_browser_open") is True:
        return False
    if result.get("browser_cleanup") is False:
        return False
    return True


async def cleanup_browser_task_session(task: dict[str, Any]) -> dict[str, Any]:
    if not _should_cleanup_browser_session(task):
        return {"status": "skipped", "reason": "cleanup_not_required"}
    work_key = str(task.get("work_key") or "").strip()
    if not work_key:
        return {"status": "skipped", "reason": "work_key_missing"}
    try:
        from app.services.pc_agent_manager import pc_agent_manager

        result = await pc_agent_manager.execute_routed_command(
            command_type="browser_close_session",
            params={
                "work_key": work_key,
                "close_browser": True,
                "close_tabs": True,
                "reason": f"browser_task_{task.get('status')}",
                "command_timeout_seconds": 10,
            },
            job_type="managed_browser_cleanup",
            required_capabilities=["interactive_browser"],
            queue_if_busy=True,
            wait_for_turn=True,
            queue_wait_timeout_seconds=10,
            lease_ttl_seconds=60,
            command_timeout_seconds=10,
        )
        if result.get("status") == "error":
            fallback = await pc_agent_manager.execute_routed_command(
                command_type="browser_health",
                params={
                    "work_key": work_key,
                    "cleanup": True,
                    "reason": f"browser_task_{task.get('status')}_fallback",
                    "command_timeout_seconds": 5,
                },
                job_type="managed_browser_cleanup",
                required_capabilities=["interactive_browser"],
                queue_if_busy=True,
                wait_for_turn=True,
                queue_wait_timeout_seconds=5,
                lease_ttl_seconds=30,
                command_timeout_seconds=5,
            )
            result = {"status": "fallback", "primary": result, "fallback": fallback}
        logger.info(
            "browser_task_cleanup_result task_id=%s work_key=%s status=%s",
            task.get("id"),
            work_key,
            result.get("status"),
        )
        return result
    except Exception as exc:
        logger.warning(
            "browser_task_cleanup_failed task_id=%s work_key=%s err=%s",
            task.get("id"),
            work_key,
            exc,
        )
        return {"status": "error", "message": str(exc)}


async def list_browser_tasks(*, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    args: list[Any] = [_tenant_uuid(tenant_id)]
    where = "tenant_id = $1"
    if status:
        where += " AND status = $2"
        args.append(status)
    args.append(max(1, min(limit, 200)))
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT *
              FROM browser_tasks
             WHERE {where}
             ORDER BY updated_at DESC
             LIMIT ${len(args)}
            """,
            *args,
        )
    return [_task_to_dict(row) for row in rows]


async def get_browser_task(*, tenant_id: str, task_id: str) -> dict[str, Any] | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM browser_tasks WHERE tenant_id = $1 AND id = $2",
            _tenant_uuid(tenant_id),
            uuid.UUID(task_id),
        )
    return _task_to_dict(row) if row else None


async def update_browser_task_status(
    *,
    tenant_id: str,
    task_id: str,
    status: str,
    current_step: str = "",
    result: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any] | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE browser_tasks
               SET status = $3,
                   current_step = COALESCE(NULLIF($4, ''), current_step),
                   result = CASE WHEN $5::jsonb = '{}'::jsonb THEN result ELSE $5::jsonb END,
                   error = $6,
                   requires_approval = CASE WHEN $3 = 'approval_required' THEN TRUE ELSE requires_approval END,
                   updated_at = NOW()
             WHERE tenant_id = $1 AND id = $2
             RETURNING *
            """,
            _tenant_uuid(tenant_id),
            uuid.UUID(task_id),
            status,
            current_step,
            json.dumps(mask_sensitive_value(result or {}), ensure_ascii=False),
            error[:1000],
        )
        if not row:
            return None
        await append_browser_task_event(
            conn=conn,
            tenant_id=tenant_id,
            task_id=task_id,
            event_type=f"status:{status}",
            payload={"current_step": current_step, "result": result or {}, "error": error},
        )
    task = _task_to_dict(row)
    if status in PUSH_STATUSES:
        await notify_browser_task_status(task)
    if status in TERMINAL_STATUSES:
        await cleanup_browser_task_session(task)
    return task


async def request_task_permission(
    *,
    tenant_id: str,
    task_id: str,
    work_key: str,
    origin: str,
    action_type: str,
    action_summary: str,
    requested_by: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = classify_browser_action(action_type, action_summary, payload)
    if policy.decision == "allow":
        return {"decision": "allow", "policy": policy.to_dict(), "request": None}
    if policy.decision == "deny":
        return {"decision": "deny", "policy": policy.to_dict(), "request": None}

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_permission_requests (
                tenant_id, task_id, work_key, origin, action_type, action_summary, risk_level, requested_by
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            _tenant_uuid(tenant_id),
            uuid.UUID(task_id),
            work_key,
            origin,
            action_type,
            action_summary,
            policy.risk_level,
            requested_by,
        )
        task_row = await conn.fetchrow(
            """
            UPDATE browser_tasks
               SET status = 'approval_required',
                   requires_approval = TRUE,
                   approval_request_id = $3,
                   current_step = $4,
                   updated_at = NOW()
             WHERE tenant_id = $1 AND id = $2
             RETURNING *
            """,
            _tenant_uuid(tenant_id),
            uuid.UUID(task_id),
            row["id"],
            action_summary[:500],
        )
        await append_browser_task_event(
            conn=conn,
            tenant_id=tenant_id,
            task_id=task_id,
            event_type="approval_required",
            payload={"request_id": str(row["id"]), "action_type": action_type, "policy": policy.to_dict()},
        )
    if task_row:
        await notify_browser_task_status(_task_to_dict(task_row))
    return {"decision": "ask", "policy": policy.to_dict(), "request": _permission_to_dict(row)}


async def decide_permission(
    *,
    tenant_id: str,
    request_id: str,
    decision: str,
    decided_by: str,
    reason: str = "",
) -> dict[str, Any] | None:
    normalized = decision.lower()
    if normalized not in {"approved", "rejected"}:
        raise ValueError("invalid_permission_decision")
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE agent_permission_requests
               SET decision = $3,
                   reason = $4,
                   decided_by = $5,
                   decided_at = NOW(),
                   updated_at = NOW()
             WHERE tenant_id = $1
               AND id = $2
               AND decision = 'pending'
               AND expires_at > NOW()
             RETURNING *
            """,
            _tenant_uuid(tenant_id),
            uuid.UUID(request_id),
            normalized,
            reason,
            decided_by,
        )
        if not row:
            return None
        status = "running" if normalized == "approved" else "failed"
        error = "" if normalized == "approved" else f"permission_rejected:{reason}"[:1000]
        task_row = await conn.fetchrow(
            """
            UPDATE browser_tasks
               SET status = $3,
                   requires_approval = FALSE,
                   error = $4,
                   updated_at = NOW()
             WHERE tenant_id = $1 AND approval_request_id = $2
             RETURNING *
            """,
            _tenant_uuid(tenant_id),
            row["id"],
            status,
            error,
        )
        if task_row:
            await append_browser_task_event(
                conn=conn,
                tenant_id=tenant_id,
                task_id=str(task_row["id"]),
                event_type=f"permission:{normalized}",
                payload={"request_id": request_id, "reason": reason},
            )
    return _permission_to_dict(row)


async def list_permission_requests(*, tenant_id: str, decision: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
              FROM agent_permission_requests
             WHERE tenant_id = $1
               AND ($2 = '' OR decision = $2)
             ORDER BY created_at DESC
             LIMIT $3
            """,
            _tenant_uuid(tenant_id),
            decision,
            max(1, min(limit, 200)),
        )
    return [_permission_to_dict(row) for row in rows]


async def append_browser_task_event(
    *,
    conn: Any,
    tenant_id: str,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO browser_task_events (tenant_id, task_id, event_type, payload)
        VALUES ($1, $2, $3, $4::jsonb)
        """,
        _tenant_uuid(tenant_id),
        uuid.UUID(task_id),
        event_type,
        json.dumps(mask_sensitive_value(payload), ensure_ascii=False),
    )


async def notify_browser_task_status(task: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.services.push_notifications import notify_managed_browser_task

        return await notify_managed_browser_task(
            tenant_id=task.get("tenant_id"),
            user_id=task.get("user_id", ""),
            task_id=task.get("id", ""),
            session_id=task.get("session_id"),
            status=task.get("status", ""),
            current_step=task.get("current_step", ""),
        )
    except Exception:
        return {"sent": 0, "failed": 1, "skipped": "notify_failed"}


def _permission_to_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("id", "tenant_id", "task_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    for key in ("expires_at", "decided_at", "created_at", "updated_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    return item
