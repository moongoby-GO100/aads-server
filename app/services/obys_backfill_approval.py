"""오비서 백필 승인 게이트 — 승인된 회사·종류·기간 안에서만 백필을 실행한다.

백필은 정기 증분 수집(마지막 성공 covered_to 3일 전부터)과 다른, 과거 구간의 일괄 재수집이다:
클로브 run_backfill(CLI)·클로브 수동 실행의 기본 창 밖 구간·배달 mode=full_backfill.
승인 기록은 OBYS DB 의 obys_backfill_approval 이고 조회가 안 되면 닫는다(fail-closed).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Awaitable, TypeVar

SOURCE_CLOBE = "clobe"
SOURCE_DELIVERY = "delivery"
SOURCES = (SOURCE_CLOBE, SOURCE_DELIVERY)

T = TypeVar("T")


class BackfillNotApproved(Exception):
    status = 409

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BackfillApprovalUnavailable(BackfillNotApproved):
    status = 503


def _fmt(start: date, end: date) -> str:
    return f"{start.isoformat()}~{end.isoformat()}"


def merge_ranges(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """겹치거나 하루 차이로 이어지는 구간을 합친다."""
    merged: list[tuple[date, date]] = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def is_covered(ranges: list[tuple[date, date]], start: date, end: date) -> bool:
    return any(s <= start and end <= e for s, e in merge_ranges(ranges))


def effective_range(source: str, kind: str, start: date, end: date) -> tuple[date, date]:
    """승인 대조에 쓰는 실제 조회 범위. channel_revenue 는 도구 스키마가 월 단위(yearMonth 등)이면 걸친 달 전체를 읽으므로
    스키마를 보기 전에도 안전하도록 항상 달 단위(1일~말일)로 넓혀 본다. 다른 종류는 요청 날짜 그대로다."""
    from app.services import obys_collection_core as core

    if source != SOURCE_CLOBE or kind != core.KIND_CHANNEL:
        return start, end
    next_month = date(end.year + (end.month == 12), end.month % 12 + 1, 1)
    return date(start.year, start.month, 1), next_month - timedelta(days=1)


def _row_matches(row: dict[str, Any], source: str, scope_id: str, branch: str, kind: str) -> bool:
    if row.get("revoked_at") is not None:
        return False
    if str(row.get("source")) != source or str(row.get("scope_id")) != scope_id or str(row.get("data_kind")) != kind:
        return False
    row_branch = str(row.get("branch") or "")
    return row_branch == "" or row_branch == branch


def check_rows(
    rows: list[dict[str, Any]], *, source: str, scope_id: str, kind: str, start: date, end: date, branch: str = "",
) -> None:
    """요청 구간 전체가 승인 구간의 합집합 안에 있어야 한다. 다른 회사·종류의 승인은 세지 않는다."""
    ranges = [(row["period_start"], row["period_end"]) for row in rows if _row_matches(row, source, scope_id, branch, kind)]
    requested = (start, end)
    start, end = effective_range(source, kind, start, end)
    if is_covered(ranges, start, end):
        return
    approved = ", ".join(_fmt(s, e) for s, e in merge_ranges(ranges)) or "없음"
    target = f"{scope_id}/{branch}" if branch else scope_id
    raise BackfillNotApproved(
        f"승인되지 않은 회사·기간입니다(승인 범위: {approved}; 요청: {target} {kind} {_fmt(*requested)}"
        + (f"; 실제 조회 범위: {_fmt(start, end)}" if (start, end) != requested else "") + ")")


def run_sync(coro: Awaitable[T]) -> T:
    """이벤트 루프가 이미 돌고 있어도 안전하게 코루틴을 끝까지 돌린다(별도 스레드)."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _connect():
    import asyncpg

    from app.core.obys_db import obys_db_url

    return await asyncpg.connect(obys_db_url(), timeout=5)


async def load_rows(source: str, scope_id: str, kind: str) -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        rows = await conn.fetch(
            "SELECT approval_id, source, scope_id, branch, data_kind, period_start, period_end, revoked_at "
            "FROM obys_backfill_approval WHERE source=$1 AND scope_id=$2 AND data_kind=$3 AND revoked_at IS NULL",
            source, scope_id, kind)
    finally:
        await conn.close()
    return [dict(r) for r in rows]


async def require_approved(
    source: str, scope_id: str, kind: str, start: date, end: date, *, branch: str = "",
) -> None:
    try:
        rows = await load_rows(source, scope_id, kind)
    except Exception as exc:  # noqa: BLE001 - 승인 기록을 못 읽으면 실행하지 않는다. 예외 본문(DSN 등)은 싣지 않는다
        raise BackfillApprovalUnavailable(
            f"백필 승인 기록을 조회하지 못해 실행하지 않았습니다({type(exc).__name__})") from None
    check_rows(rows, source=source, scope_id=scope_id, kind=kind, start=start, end=end, branch=branch)


def require_approved_sync(
    source: str, scope_id: str, kind: str, start: date, end: date, *, branch: str = "",
) -> None:
    run_sync(require_approved(source, scope_id, kind, start, end, branch=branch))


def _view(row: Any) -> dict[str, Any]:
    d = dict(row)
    return {k: (v.isoformat() if hasattr(v, "isoformat") else (str(v) if k == "approval_id" else v)) for k, v in d.items()}


def _validate_new(source: str, scope_id: str, branch: str, kind: str, start: date, end: date, reason: str) -> None:
    from app.services import obys_collection_core as core

    if source not in SOURCES:
        raise ValueError("invalid_source")
    if not scope_id.strip() or not kind.strip():
        raise ValueError("scope_and_kind_required")
    if start > end:
        raise ValueError("invalid_period")
    if len(reason.strip()) < 5:
        raise ValueError("reason_required")
    if source == SOURCE_CLOBE:
        if kind not in core.COLLECTION_KINDS:
            raise ValueError("invalid_kind")
        if branch:
            raise ValueError("branch_not_allowed_for_clobe")
    else:
        from app.services.yeoljeong_delivery_collectors import PORTAL_CONFIG

        if kind not in PORTAL_CONFIG:
            raise ValueError("invalid_kind")


async def create_approval(
    *, source: str, scope_id: str, kind: str, start: date, end: date, reason: str, actor: str, branch: str = "",
) -> dict[str, Any]:
    scope_id, branch, kind, reason = scope_id.strip(), branch.strip(), kind.strip(), reason.strip()
    _validate_new(source, scope_id, branch, kind, start, end, reason)
    approval_id = uuid.uuid4()
    conn = await _connect()
    try:
        async with conn.transaction():
            if source == SOURCE_CLOBE:
                known = await conn.fetchval(
                    "SELECT 1 FROM obys_clobe_company_link WHERE clobe_company_id=$1 AND link_status='linked'", scope_id)
            else:
                known = await conn.fetchval("SELECT 1 FROM yeoljeong_businesses WHERE id=$1", scope_id)
            if not known:
                raise ValueError("unknown_scope")
            row = await conn.fetchrow(
                "INSERT INTO obys_backfill_approval (approval_id, source, scope_id, branch, data_kind, period_start, "
                "period_end, approved_by, reason) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                "RETURNING approval_id, source, scope_id, branch, data_kind, period_start, period_end, "
                "approved_by, approved_at, reason, revoked_at, revoked_by",
                approval_id, source, scope_id, branch, kind, start, end, actor, reason)
            await conn.execute(
                "INSERT INTO obys_backfill_approval_audit (approval_id, action, actor, detail) VALUES ($1,'approve',$2,$3::jsonb)",
                approval_id, actor, json.dumps({"source": source, "scope_id": scope_id, "branch": branch, "data_kind": kind,
                                                "period": [start.isoformat(), end.isoformat()], "reason": reason},
                                               ensure_ascii=False))
    finally:
        await conn.close()
    return _view(row)


async def revoke_approval(approval_id: str, *, actor: str) -> dict[str, Any]:
    try:
        key = uuid.UUID(approval_id)
    except ValueError:
        raise ValueError("invalid_approval_id") from None
    conn = await _connect()
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE obys_backfill_approval SET revoked_at=now(), revoked_by=$2 "
                "WHERE approval_id=$1 AND revoked_at IS NULL "
                "RETURNING approval_id, source, scope_id, branch, data_kind, period_start, period_end, "
                "approved_by, approved_at, reason, revoked_at, revoked_by", key, actor)
            if row is None:
                raise ValueError("approval_not_found_or_revoked")
            await conn.execute(
                "INSERT INTO obys_backfill_approval_audit (approval_id, action, actor) VALUES ($1,'revoke',$2)", key, actor)
    finally:
        await conn.close()
    return _view(row)


async def list_approvals(*, source: str | None = None, include_revoked: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        rows = await conn.fetch(
            "SELECT approval_id, source, scope_id, branch, data_kind, period_start, period_end, approved_by, approved_at, "
            "reason, revoked_at, revoked_by FROM obys_backfill_approval "
            "WHERE ($1::text IS NULL OR source=$1) AND ($2 OR revoked_at IS NULL) ORDER BY approved_at DESC LIMIT $3",
            source, include_revoked, limit)
    finally:
        await conn.close()
    return [_view(r) for r in rows]
