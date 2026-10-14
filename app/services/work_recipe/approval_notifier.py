"""자동화 승인 대기 알림 — 생성 알림 1건, 48시간 재알림 1회, 14일 만료 표시 (FR-18).

registration 테이블과 decide_registration/request_registration 은 건드리지 않는다.
알림 이력은 work_recipe_approval_notices 에만 남기고, "한 번만"은 컬럼이 NULL 일 때만
채워지는 원자적 UPDATE(claim)로 보장한다 — 스윕이 겹쳐 돌아도 같은 알림이 두 번 나가지 않는다.
만료돼도 registration 행은 pending 그대로 두고(지우지 않음), 이 메타의 expired_at 으로만 표시한다.
알림 본문에는 비밀값이 들어가지 않는다(레시피 이름·도메인만, 비밀 패턴은 지운다).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

APPROVERS_ENV = "AUTOMATION_APPROVERS"
DEFAULT_APPROVER = "대표님"
REMIND_AFTER = timedelta(hours=48)
EXPIRE_AFTER = timedelta(days=14)
SWEEP_LIMIT = 500

_CLAIM_COLUMNS = frozenset({"notified_at", "reminded_at", "expired_at"})
_SECRET_PATTERN = re.compile(
    r"eyJ[\w-]{10,}|sk-[A-Za-z0-9_-]{10,}|(?i:password|passwd|token|secret)\s*[=:]\s*\S+"
)


def approvers() -> list[str]:
    """승인자 표시 이름. 기본은 대표님 1명, 두 번째 관리자는 환경변수로만 추가한다."""
    raw = os.getenv(APPROVERS_ENV, "")
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names or [DEFAULT_APPROVER]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _clean(text: Any, limit: int = 80) -> str:
    return _SECRET_PATTERN.sub("***", str(text or "")).strip()[:limit]


def _spec_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _label(row: Mapping[str, Any]) -> str:
    spec = _spec_object(row.get("spec"))
    return _clean(spec.get("description") or row.get("name") or "이름 없는 자동화")


def _created_text(label: str, domain: str) -> tuple[str, str]:
    return (
        f"자동화 승인 요청: {label}",
        f"'{label}' 자동화가 승인을 기다리고 있어요. (사이트: {domain}) "
        "내용을 확인하고 승인하거나 거절해 주세요.",
    )


def _reminder_text(label: str, domain: str) -> tuple[str, str]:
    return (
        f"자동화 승인 재알림: {label}",
        f"'{label}' 자동화가 이틀 넘게 승인을 기다리고 있어요. (사이트: {domain}) "
        "14일이 지나면 만료되니 그 전에 확인해 주세요.",
    )


async def _send(title: str, message: str, *, severity: str) -> bool:
    """승인자마다 알림함에 1건. 하나라도 들어가면 성공으로 본다."""
    from app.services.ohvis_alert import notify

    sent = False
    for approver in approvers():
        ok = await notify(
            title,
            f"{message} (승인자: {approver})",
            severity=severity,
            category="automation_approval",
        )
        sent = sent or bool(ok)
    return sent


async def _claim(conn: Any, row: Mapping[str, Any], column: str, now: datetime) -> bool:
    """column 이 NULL 일 때만 now 로 채우고 True. 이미 채워졌으면 False."""
    if column not in _CLAIM_COLUMNS:
        raise ValueError(f"unsupported_claim_column:{column}")
    clock = row.get("clock_started_at") or row["requested_at"]
    claimed = await conn.fetchrow(
        f"""
        INSERT INTO work_recipe_approval_notices
            (registration_id, tenant_id, clock_started_at, {column})
        VALUES ($1::uuid, $2::uuid, $3, $4)
        ON CONFLICT (registration_id) DO UPDATE
           SET {column} = EXCLUDED.{column}, updated_at = NOW()
         WHERE work_recipe_approval_notices.{column} IS NULL
        RETURNING registration_id
        """,
        str(row["id"]),
        str(row["tenant_id"]),
        clock,
        now,
    )
    return claimed is not None


async def _release(conn: Any, registration_id: Any, column: str) -> None:
    if column not in _CLAIM_COLUMNS:
        raise ValueError(f"unsupported_claim_column:{column}")
    await conn.execute(
        f"UPDATE work_recipe_approval_notices SET {column}=NULL, updated_at=NOW() "
        "WHERE registration_id=$1::uuid",
        str(registration_id),
    )


async def _claim_and_send(
    conn: Any, row: Mapping[str, Any], column: str, now: datetime,
    text: tuple[str, str], severity: str,
) -> bool:
    if not await _claim(conn, row, column, now):
        return False
    if await _send(text[0], text[1], severity=severity):
        return True
    await _release(conn, row["id"], column)  # 다음 스윕에서 다시 시도
    return False


async def notify_pending_created(
    registration: Mapping[str, Any], *, now: datetime | None = None
) -> bool:
    """pending registration 이 만들어졌을 때 승인자에게 1건. 어떤 오류도 호출부로 새지 않는다."""
    try:
        if str(registration.get("status") or "") != "pending":
            return False
        now = _as_aware(now) if now else _utcnow()
        row = {**registration, "clock_started_at": None}
        row["requested_at"] = registration.get("requested_at") or now
        async with get_pool().acquire() as conn:
            revived = await conn.fetchrow(
                """
                UPDATE work_recipe_approval_notices
                   SET expired_at=NULL, notified_at=NULL, reminded_at=NULL,
                       clock_started_at=$2, updated_at=NOW()
                 WHERE registration_id=$1::uuid AND expired_at IS NOT NULL
                RETURNING registration_id
                """,
                str(registration["id"]),
                now,
            )
            if revived is not None:
                row["clock_started_at"] = now  # 만료 뒤 다시 요청 — 14일 시계를 새로 시작
            domain = _clean(registration.get("domain"))
            return await _claim_and_send(
                conn, row, "notified_at", now,
                _created_text(_label(registration), domain), "info",
            )
    except Exception as exc:
        logger.warning("approval_notify_created_failed: %s", str(exc)[:200])
        return False


async def run_sweep(*, now: datetime | None = None) -> dict[str, int]:
    """매시 스윕: 14일 경과 → 만료 표시, 48시간 경과 & 재알림 0회 → 재알림 1회, 미알림 → 생성 알림."""
    now = _as_aware(now) if now else _utcnow()
    stats = {"scanned": 0, "notified": 0, "reminded": 0, "expired": 0}
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.id, r.tenant_id, r.name, r.domain, r.spec, r.requested_at,
                   n.clock_started_at, n.notified_at, n.reminded_at
              FROM work_recipe_registration_requests r
              LEFT JOIN work_recipe_approval_notices n ON n.registration_id = r.id
             WHERE r.status = 'pending' AND n.expired_at IS NULL
             ORDER BY r.requested_at
             LIMIT $1
            """,
            SWEEP_LIMIT,
        )
        for raw in rows:
            row = dict(raw)
            stats["scanned"] += 1
            started = _as_aware(row.get("clock_started_at") or row["requested_at"])
            age = now - started
            label, domain = _label(row), _clean(row.get("domain"))
            if age >= EXPIRE_AFTER:
                if await _claim(conn, row, "expired_at", now):
                    stats["expired"] += 1
            elif age >= REMIND_AFTER:
                if row.get("reminded_at") is None and await _claim_and_send(
                    conn, row, "reminded_at", now, _reminder_text(label, domain), "warning"
                ):
                    stats["reminded"] += 1
            elif row.get("notified_at") is None and await _claim_and_send(
                conn, row, "notified_at", now, _created_text(label, domain), "info"
            ):
                stats["notified"] += 1
    return stats


async def pending_count(*, tenant_id: Any, now: datetime | None = None) -> dict[str, Any]:
    """홈 배지용. 만료된 요청은 세지 않는다 — 스윕 전이라도 14일이 넘었으면 제외."""
    now = _as_aware(now) if now else _utcnow()
    tenant = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS pending, MIN(COALESCE(n.clock_started_at, r.requested_at)) AS oldest
              FROM work_recipe_registration_requests r
              LEFT JOIN work_recipe_approval_notices n ON n.registration_id = r.id
             WHERE r.tenant_id = $1 AND r.status = 'pending' AND n.expired_at IS NULL
               AND COALESCE(n.clock_started_at, r.requested_at) > $2
            """,
            tenant,
            now - EXPIRE_AFTER,
        )
    pending = int(row["pending"] or 0) if row else 0
    oldest = row["oldest"] if row else None
    hours = round((now - _as_aware(oldest)).total_seconds() / 3600, 1) if oldest else 0
    return {"pending": pending, "oldest_hours": max(hours, 0)}
