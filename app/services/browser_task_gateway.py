"""DB-backed managed browser task lifecycle service."""
from __future__ import annotations

import json
import logging
import hashlib
import secrets
import uuid
from typing import Any
from urllib.parse import urlparse

from app.core.db_pool import get_pool
from app.services.browser_permission_policy import classify_browser_action, mask_sensitive_value
from app.services.managed_browser import normalize_work_key


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
PUSH_STATUSES = {"approval_required", "auth_required", "completed", "failed"}
logger = logging.getLogger(__name__)
CHALLENGE_KINDS = {"otp", "captcha"}
CHALLENGE_MODEL_SOURCES = {"llm", "model", "vision", "solver", "ocr", "auto"}


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


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _origin_host(value: str) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return str(value or "").strip().lower()


def _payload_uses_challenge_model_analysis(payload: dict[str, Any]) -> bool:
    source = str(
        payload.get("value_source")
        or payload.get("captcha_value_source")
        or payload.get("otp_value_source")
        or ""
    ).lower()
    return source in CHALLENGE_MODEL_SOURCES or any(
        bool(payload.get(key)) for key in ("llm_generated_value", "model_generated_value", "captcha_solved_by_model")
    )


def _contains_challenge_bypass(payload: dict[str, Any]) -> bool:
    intent = str(payload.get("intent") or payload.get("action") or "").lower()
    if "bypass" in intent or payload.get("bypass_challenge"):
        return True
    challenge_kind = str(payload.get("challenge_kind") or payload.get("challenge") or "").lower()
    if challenge_kind == "otp" and _payload_uses_challenge_model_analysis(payload):
        return True
    if payload.get("otp_generated_by_model"):
        return True
    return any(bool(payload.get(key)) for key in ("unauthorized_challenge_bypass", "challenge_bypass_attempt"))


def _scope_allows_action(
    scope: dict[str, Any],
    *,
    action_type: str,
    origin: str,
    selector: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    payload = payload or {}
    policy = classify_browser_action(action_type, str(payload.get("summary") or ""), payload)
    if policy.decision == "deny":
        return False, policy.reason

    allowed_action_types = {str(item).lower() for item in _json_list(scope.get("action_types")) if str(item).strip()}
    if allowed_action_types and str(action_type or "").lower() not in allowed_action_types:
        return False, "action_type_out_of_scope"

    approved_origin = _origin_host(str(scope.get("origin") or ""))
    allowed_origins = {_origin_host(str(item)) for item in _json_list(scope.get("origins")) if str(item).strip()}
    actual_origin = _origin_host(origin)
    if approved_origin and actual_origin != approved_origin:
        return False, "origin_out_of_scope"
    if allowed_origins and actual_origin not in allowed_origins:
        return False, "origin_out_of_scope"

    selectors = {str(item) for item in _json_list(scope.get("selectors")) if str(item)}
    if selectors and selector and selector not in selectors:
        return False, "selector_out_of_scope"

    challenge_kind = str(payload.get("challenge_kind") or payload.get("challenge") or "").lower()
    if challenge_kind in CHALLENGE_KINDS:
        if _contains_challenge_bypass(payload):
            return False, "challenge_bypass_blocked"
        allowed_challenge_kinds = {
            str(item).lower()
            for item in _json_list(scope.get("challenge_kinds"))
            if str(item).strip()
        }
        if allowed_challenge_kinds and challenge_kind not in allowed_challenge_kinds:
            return False, "challenge_kind_out_of_scope"
        if challenge_kind == "captcha" and _payload_uses_challenge_model_analysis(payload):
            if scope.get("allow_model_challenge_analysis") is not True:
                return False, "model_challenge_analysis_not_approved"
        elif payload.get("operator_confirmed") is not True and payload.get("operator_approved") is not True:
            return False, "operator_confirmed_input_required"

    return True, "approved_scope_match"


def _iso(value: Any) -> str:
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _permission_decision_audit_payload(
    row: Any,
    *,
    request_id: str,
    decision: str,
    decided_by: str,
    reason: str,
    approval_scope: dict[str, Any] | None,
    max_executions: int | None,
    approval_token_issued: bool,
) -> dict[str, Any]:
    item = dict(row)
    scope = approval_scope or _json_dict(item.get("approval_scope"))
    return {
        "request_id": request_id,
        "decision": decision,
        "reason": reason,
        "decided_by": decided_by,
        "decided_at": _iso(item.get("decided_at")),
        "work_key": item.get("work_key") or "",
        "origin": item.get("origin") or "",
        "action_type": item.get("action_type") or "",
        "action_summary": item.get("action_summary") or "",
        "approval_scope": mask_sensitive_value(scope),
        "max_executions": max_executions if max_executions is not None else item.get("max_executions"),
        "approval_token_issued": approval_token_issued,
    }


def _approval_token_audit_payload(
    row: Any,
    *,
    action_type: str,
    origin: str,
    selector: str = "",
    reason: str,
    status: str,
    used_executions: int | None = None,
) -> dict[str, Any]:
    item = dict(row)
    payload = {
        "status": status,
        "request_id": str(item.get("request_id") or ""),
        "approved_by": item.get("created_by") or "",
        "approved_at": _iso(item.get("created_at")),
        "work_key": item.get("work_key") or "",
        "approved_origin": item.get("origin") or "",
        "actual_origin": origin,
        "action_type": action_type,
        "action_summary": item.get("action_summary") or "",
        "selector": selector,
        "approval_scope": mask_sensitive_value(_json_dict(item.get("approval_scope"))),
        "max_executions": item.get("max_executions"),
        "used_executions": used_executions if used_executions is not None else item.get("used_executions"),
        "expires_at": _iso(item.get("expires_at")),
        "reason": reason,
    }
    return mask_sensitive_value(payload)


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
    automation_scope: dict[str, Any] | None = None,
    max_executions: int = 1,
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
                tenant_id, task_id, work_key, origin, action_type, action_summary, risk_level, requested_by,
                approval_scope, max_executions
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
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
            json.dumps(mask_sensitive_value(automation_scope or {}), ensure_ascii=False),
            max(1, min(int(max_executions or 1), 500)),
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
    approval_scope: dict[str, Any] | None = None,
    max_executions: int | None = None,
) -> dict[str, Any] | None:
    normalized = decision.lower()
    if normalized not in {"approved", "rejected"}:
        raise ValueError("invalid_permission_decision")
    async with get_pool().acquire() as conn:
        approval_token = secrets.token_urlsafe(32) if normalized == "approved" else ""
        approval_token_hash = _token_hash(approval_token) if approval_token else None
        row = await conn.fetchrow(
            """
            UPDATE agent_permission_requests
               SET decision = $3,
                   reason = $4,
                   decided_by = $5,
                   approval_scope = CASE WHEN $6::jsonb = '{}'::jsonb THEN approval_scope ELSE $6::jsonb END,
                   max_executions = COALESCE($7, max_executions),
                   token_hash = $8,
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
            json.dumps(mask_sensitive_value(approval_scope or {}), ensure_ascii=False),
            max(1, min(int(max_executions), 500)) if max_executions is not None else None,
            approval_token_hash,
        )
        if not row:
            return None
        if normalized == "approved" and approval_token_hash:
            scope = _json_dict(row.get("approval_scope"))
            if approval_scope:
                scope = mask_sensitive_value(approval_scope)
            await conn.execute(
                """
                INSERT INTO browser_approval_tokens (
                    token_hash, tenant_id, task_id, request_id, work_key, origin, action_type,
                    action_summary, approval_scope, policy, max_executions, expires_at, created_by
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12, $13)
                ON CONFLICT (token_hash) DO NOTHING
                """,
                approval_token_hash,
                _tenant_uuid(tenant_id),
                row.get("task_id"),
                row["id"],
                row["work_key"],
                row["origin"],
                row["action_type"],
                row["action_summary"],
                json.dumps(scope, ensure_ascii=False),
                json.dumps({"risk_level": row["risk_level"], "decision": "approved"}, ensure_ascii=False),
                max(1, min(int(row.get("max_executions") or 1), 500)),
                row["expires_at"],
                decided_by,
            )
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
                payload=_permission_decision_audit_payload(
                    row,
                    request_id=request_id,
                    decision=normalized,
                    decided_by=decided_by,
                    reason=reason,
                    approval_scope=approval_scope,
                    max_executions=max_executions,
                    approval_token_issued=bool(approval_token_hash),
                ),
            )
    result = _permission_to_dict(row)
    if approval_token:
        result["approval_token"] = approval_token
    return result


async def consume_approval_token(
    *,
    tenant_id: str,
    task_id: str,
    approval_token: str,
    action_type: str,
    origin: str,
    selector: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    token_hash = _token_hash(approval_token)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
              FROM browser_approval_tokens
             WHERE token_hash = $1
               AND tenant_id = $2
               AND task_id = $3
               AND revoked_at IS NULL
               AND expires_at > NOW()
             FOR UPDATE
            """,
            token_hash,
            _tenant_uuid(tenant_id),
            uuid.UUID(task_id),
        )
        if not row:
            await append_browser_task_event(
                conn=conn,
                tenant_id=tenant_id,
                task_id=task_id,
                event_type="approval_token:denied",
                payload={"action_type": action_type, "origin": origin, "reason": "token_not_found_or_expired"},
            )
            return {"status": "denied", "reason": "token_not_found_or_expired"}
        if int(row["used_executions"]) >= int(row["max_executions"]):
            await append_browser_task_event(
                conn=conn,
                tenant_id=tenant_id,
                task_id=task_id,
                event_type="approval_token:denied",
                payload=_approval_token_audit_payload(
                    row,
                    action_type=action_type,
                    origin=origin,
                    selector=selector,
                    reason="execution_limit_exceeded",
                    status="denied",
                ),
            )
            return {"status": "denied", "reason": "execution_limit_exceeded"}
        allowed, reason = _scope_allows_action(
            _json_dict(row["approval_scope"]),
            action_type=action_type,
            origin=origin,
            selector=selector,
            payload=payload,
        )
        if not allowed:
            await append_browser_task_event(
                conn=conn,
                tenant_id=tenant_id,
                task_id=task_id,
                event_type="approval_token:denied",
                payload=_approval_token_audit_payload(
                    row,
                    action_type=action_type,
                    origin=origin,
                    selector=selector,
                    reason=reason,
                    status="denied",
                ),
            )
            return {"status": "denied", "reason": reason}
        updated = await conn.fetchrow(
            """
            UPDATE browser_approval_tokens
               SET used_executions = used_executions + 1,
                   updated_at = NOW()
             WHERE token_hash = $1
             RETURNING used_executions, max_executions, expires_at
            """,
            token_hash,
        )
        await append_browser_task_event(
            conn=conn,
            tenant_id=tenant_id,
            task_id=task_id,
            event_type="approval_token:consumed",
            payload=_approval_token_audit_payload(
                row,
                action_type=action_type,
                origin=origin,
                selector=selector,
                reason=reason,
                status="approved",
                used_executions=int(updated["used_executions"]),
            ),
        )
    return {
        "status": "approved",
        "reason": reason,
        "used_executions": int(updated["used_executions"]),
        "max_executions": int(updated["max_executions"]),
        "expires_at": updated["expires_at"].isoformat(),
    }


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
