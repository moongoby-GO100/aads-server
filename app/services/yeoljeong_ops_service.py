"""Service layer for the Yeoljeong store-assistant approvals / notifications / audit-log center.

Tables (see scripts/migrations/yeoljeong_ops_tables.sql):
    yeoljeong_approvals, yeoljeong_notifications, yeoljeong_audit_logs

Core rules:
    - Approve/reject writes always emit a notification to the requester AND an audit-log row.
    - Notification records get an `icon`/`color` pair attached for UI display based on notification_type.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://aads:aads@localhost:5432/aads")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 알림 type -> UI 표시용 아이콘/색상 매핑
# ---------------------------------------------------------------------------
NOTIFICATION_TYPE_META: dict[str, dict[str, str]] = {
    "approval_requested": {"icon": "clock", "color": "amber"},
    "approval_approved": {"icon": "check-circle", "color": "green"},
    "approval_rejected": {"icon": "x-circle", "color": "red"},
    "system": {"icon": "info", "color": "blue"},
    "warning": {"icon": "alert-triangle", "color": "orange"},
    "error": {"icon": "alert-circle", "color": "red"},
    "default": {"icon": "bell", "color": "gray"},
}


def _attach_notification_meta(record: dict[str, Any]) -> dict[str, Any]:
    meta = NOTIFICATION_TYPE_META.get(str(record.get("notification_type") or ""), NOTIFICATION_TYPE_META["default"])
    record["icon"] = meta["icon"]
    record["color"] = meta["color"]
    return record


def _row(record: Any) -> dict[str, Any]:
    return dict(record) if record is not None else {}


# ---------------------------------------------------------------------------
# 승인함 (yeoljeong_approvals)
# ---------------------------------------------------------------------------
async def list_approvals(
    business_id: str = "biz-mia",
    status: str | None = None,
    approval_type: str | None = None,
) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        if status:
            params.append(status)
            conditions.append(f"status = ${len(params)}")
        if approval_type:
            params.append(approval_type)
            conditions.append(f"approval_type = ${len(params)}")
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"SELECT * FROM yeoljeong_approvals WHERE {where} ORDER BY created_at DESC",
            *params,
        )
        return [_row(r) for r in rows]
    finally:
        await conn.close()


async def count_pending_approvals(business_id: str = "biz-mia") -> int:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        val = await conn.fetchval(
            "SELECT COUNT(*) FROM yeoljeong_approvals WHERE business_id = $1 AND status = 'pending'",
            business_id,
        )
        return int(val or 0)
    finally:
        await conn.close()


async def _decide_approval(approval_id: str, status: str, memo: str, decided_by: str) -> dict[str, Any] | None:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM yeoljeong_approvals WHERE id = $1 FOR UPDATE",
                approval_id,
            )
            if not existing:
                return None
            now = _now()
            updated = await conn.fetchrow(
                """
                UPDATE yeoljeong_approvals
                   SET status = $2,
                       decided_by = $3,
                       decided_at = $4,
                       memo = $5,
                       updated_at = $4
                 WHERE id = $1
             RETURNING *
                """,
                approval_id,
                status,
                decided_by,
                now,
                memo,
            )
            result = _row(updated)
            action_label = "승인" if status == "approved" else "반려"
            notification_type = "approval_approved" if status == "approved" else "approval_rejected"
            await _insert_notification_conn(
                conn,
                business_id=result.get("business_id") or "biz-mia",
                target_user=str(result.get("requested_by") or ""),
                notification_type=notification_type,
                title=f"[{action_label}] {result.get('title')}",
                body=memo or f"{result.get('title')} 요청이 {action_label}되었습니다.",
                reference_type="approval",
                reference_id=approval_id,
            )
            await _insert_audit_log_conn(
                conn,
                business_id=result.get("business_id") or "biz-mia",
                actor=decided_by,
                action=f"approval.{status}",
                resource_type="approval",
                resource_id=approval_id,
                details={
                    "memo": memo,
                    "approval_type": result.get("approval_type"),
                    "title": result.get("title"),
                },
            )
            return result
    finally:
        await conn.close()


async def approve_approval(approval_id: str, memo: str, decided_by: str) -> dict[str, Any] | None:
    return await _decide_approval(approval_id, "approved", memo, decided_by)


async def reject_approval(approval_id: str, memo: str, decided_by: str) -> dict[str, Any] | None:
    return await _decide_approval(approval_id, "rejected", memo, decided_by)


async def create_approval(
    business_id: str,
    approval_type: str,
    reference_id: str,
    title: str,
    description: str = "",
    requested_by: str = "",
    priority: str = "normal",
) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO yeoljeong_approvals
                    (business_id, approval_type, reference_id, title, description, requested_by, priority)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
             RETURNING *
                """,
                business_id,
                approval_type,
                reference_id,
                title,
                description,
                requested_by,
                priority,
            )
            result = _row(row)
            await _insert_audit_log_conn(
                conn,
                business_id=business_id,
                actor=requested_by or "system",
                action="approval.requested",
                resource_type="approval",
                resource_id=str(result.get("id") or ""),
                details={"approval_type": approval_type, "title": title, "reference_id": reference_id},
            )
            return result
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 알림센터 (yeoljeong_notifications)
# ---------------------------------------------------------------------------
async def _insert_notification_conn(
    conn: Any,
    *,
    business_id: str,
    target_user: str,
    notification_type: str,
    title: str,
    body: str = "",
    reference_type: str = "",
    reference_id: str = "",
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO yeoljeong_notifications
            (business_id, target_user, notification_type, title, body, reference_type, reference_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
     RETURNING *
        """,
        business_id,
        target_user,
        notification_type,
        title,
        body,
        reference_type,
        reference_id,
    )
    return _attach_notification_meta(_row(row))


async def create_notification(
    business_id: str,
    target_user: str,
    notification_type: str,
    title: str,
    body: str = "",
    reference_type: str = "",
    reference_id: str = "",
) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        return await _insert_notification_conn(
            conn,
            business_id=business_id,
            target_user=target_user,
            notification_type=notification_type,
            title=title,
            body=body,
            reference_type=reference_type,
            reference_id=reference_id,
        )
    finally:
        await conn.close()


async def list_notifications(
    business_id: str = "biz-mia",
    target_user: str | None = None,
    is_read: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        if target_user:
            params.append(target_user)
            conditions.append(f"target_user = ${len(params)}")
        if is_read is not None:
            params.append(is_read)
            conditions.append(f"is_read = ${len(params)}")
        where = " AND ".join(conditions)
        params.append(max(1, min(limit, 500)))
        rows = await conn.fetch(
            f"SELECT * FROM yeoljeong_notifications WHERE {where} ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
        return [_attach_notification_meta(_row(r)) for r in rows]
    finally:
        await conn.close()


async def count_unread_notifications(business_id: str = "biz-mia", target_user: str | None = None) -> int:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1", "is_read = FALSE"]
        params: list[Any] = [business_id]
        if target_user:
            params.append(target_user)
            conditions.append(f"target_user = ${len(params)}")
        where = " AND ".join(conditions)
        val = await conn.fetchval(f"SELECT COUNT(*) FROM yeoljeong_notifications WHERE {where}", *params)
        return int(val or 0)
    finally:
        await conn.close()


async def mark_notification_read(notification_id: str) -> dict[str, Any] | None:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        row = await conn.fetchrow(
            """
            UPDATE yeoljeong_notifications
               SET is_read = TRUE, read_at = $2
             WHERE id = $1
         RETURNING *
            """,
            notification_id,
            _now(),
        )
        if not row:
            return None
        return _attach_notification_meta(_row(row))
    finally:
        await conn.close()


async def mark_all_notifications_read(business_id: str = "biz-mia", target_user: str | None = None) -> int:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1", "is_read = FALSE"]
        params: list[Any] = [business_id]
        if target_user:
            params.append(target_user)
            conditions.append(f"target_user = ${len(params)}")
        params.append(_now())
        where = " AND ".join(conditions)
        result = await conn.execute(
            f"UPDATE yeoljeong_notifications SET is_read = TRUE, read_at = ${len(params)} WHERE {where}",
            *params,
        )
        # asyncpg execute() returns a status string like "UPDATE 5"
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 감사로그 (yeoljeong_audit_logs)
# ---------------------------------------------------------------------------
async def _insert_audit_log_conn(
    conn: Any,
    *,
    business_id: str,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str = "",
    details: dict[str, Any] | None = None,
    ip_address: str = "",
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO yeoljeong_audit_logs
            (business_id, actor, action, resource_type, resource_id, details, ip_address)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
     RETURNING *
        """,
        business_id,
        actor,
        action,
        resource_type,
        resource_id,
        json.dumps(details or {}, ensure_ascii=False, default=str),
        ip_address,
    )
    return _row(row)


async def log_action(
    business_id: str,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str = "",
    details: dict[str, Any] | None = None,
    ip_address: str = "",
) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        return await _insert_audit_log_conn(
            conn,
            business_id=business_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address,
        )
    finally:
        await conn.close()


async def list_audit_logs(
    business_id: str = "biz-mia",
    actor: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        if actor:
            params.append(actor)
            conditions.append(f"actor = ${len(params)}")
        if action:
            params.append(action)
            conditions.append(f"action = ${len(params)}")
        if resource_type:
            params.append(resource_type)
            conditions.append(f"resource_type = ${len(params)}")
        if date_from:
            params.append(date_from)
            conditions.append(f"created_at >= ${len(params)}::timestamptz")
        if date_to:
            params.append(date_to)
            conditions.append(f"created_at <= ${len(params)}::timestamptz")
        where = " AND ".join(conditions)
        params.append(max(1, min(limit, 1000)))
        rows = await conn.fetch(
            f"SELECT * FROM yeoljeong_audit_logs WHERE {where} ORDER BY created_at DESC LIMIT ${len(params)}",
            *params,
        )
        return [_row(r) for r in rows]
    finally:
        await conn.close()


async def audit_log_summary(
    business_id: str = "biz-mia",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        if date_from:
            params.append(date_from)
            conditions.append(f"created_at >= ${len(params)}::timestamptz")
        if date_to:
            params.append(date_to)
            conditions.append(f"created_at <= ${len(params)}::timestamptz")
        where = " AND ".join(conditions)
        total = await conn.fetchval(f"SELECT COUNT(*) FROM yeoljeong_audit_logs WHERE {where}", *params)
        by_action = await conn.fetch(
            f"""
            SELECT action, COUNT(*) AS cnt
              FROM yeoljeong_audit_logs
             WHERE {where}
          GROUP BY action
          ORDER BY cnt DESC
            """,
            *params,
        )
        by_actor = await conn.fetch(
            f"""
            SELECT actor, COUNT(*) AS cnt
              FROM yeoljeong_audit_logs
             WHERE {where}
          GROUP BY actor
          ORDER BY cnt DESC
            """,
            *params,
        )
        return {
            "business_id": business_id,
            "date_from": date_from,
            "date_to": date_to,
            "total": int(total or 0),
            "by_action": [_row(r) for r in by_action],
            "by_actor": [_row(r) for r in by_actor],
        }
    finally:
        await conn.close()
