"""검토 대기 정본 문서 조회와 CEO 일일 요약 발송.

검토 대기 = 최신 리비전의 마지막 상태 이벤트(review/approved/archived)가 'review' 이고
승인 포인터가 최신 리비전이 아닌 문서. 같은 조회를 읽기 API 와 일일 잡이 공유한다.
"""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger()

KST = ZoneInfo("Asia/Seoul")
PER_PROJECT_LIMIT = 10
MAX_MESSAGE_CHARS = 3900
_TITLE_CHARS = 60
_LOCK_KEY = "canonical_review_digest"

_QUEUE_SQL = (
    "SELECT h.tenant_id::text AS tenant_id, h.project_key, h.document_key, h.kind, r.title, "
    "ev.created_at AS review_at "
    "FROM project_document_heads h "
    "JOIN project_document_revisions r ON r.id=h.latest_revision_id AND r.head_id=h.id "
    "JOIN LATERAL (SELECT e.action, e.created_at FROM project_document_events e "
    "WHERE e.head_id=h.id AND e.revision_id=h.latest_revision_id "
    "AND e.action IN ('review','approved','archived') ORDER BY e.id DESC LIMIT 1) ev ON true "
    "WHERE ($1::uuid IS NULL OR h.tenant_id=$1::uuid) AND ($2::text IS NULL OR h.project_key=$2) "
    "AND ev.action='review' AND h.approved_revision_id IS DISTINCT FROM h.latest_revision_id "
    "ORDER BY ev.created_at, h.project_key, h.document_key"
)


def _kst_now() -> datetime:
    return datetime.now(KST)


async def fetch_review_queue(conn: Any, tenant_id: str | None, project_key: str | None = None,
                             *, now: datetime | None = None) -> list[dict]:
    """tenant_id=None 은 일일 잡 전용(전 테넌트). API 는 항상 테넌트를 넘긴다."""
    today = (now or _kst_now()).astimezone(KST).date()
    rows = await conn.fetch(_QUEUE_SQL, tenant_id, project_key)
    items = []
    for row in rows:
        review_kst = row["review_at"].astimezone(KST)
        items.append({
            "tenant_id": row["tenant_id"],
            "project_key": row["project_key"],
            "document_key": row["document_key"],
            "title": row["title"],
            "kind": row["kind"],
            "review_at": review_kst.isoformat(),
            "waiting_days": max((today - review_kst.date()).days, 0),
        })
    return items


def format_digest(items: list[dict]) -> str:
    by_project: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_project[item["project_key"]].append(item)
    lines = [f"📋 <b>검토 대기 정본 문서 {len(items)}건</b>"]
    for project in sorted(by_project):
        docs = sorted(by_project[project], key=lambda d: (-d["waiting_days"], d["document_key"]))
        lines.append(f"\n<b>{html.escape(project)}</b> ({len(docs)}건)")
        for doc in docs[:PER_PROJECT_LIMIT]:
            title = doc["title"] if len(doc["title"]) <= _TITLE_CHARS else doc["title"][:_TITLE_CHARS] + "…"
            lines.append(f"  • {html.escape(title)} ({html.escape(doc['document_key'])}) — {doc['waiting_days']}일 대기")
        if len(docs) > PER_PROJECT_LIMIT:
            lines.append(f"  … 외 {len(docs) - PER_PROJECT_LIMIT}건")
    lines.append("\n대시보드 '문서 &gt; 정본 문서'에서 검토 후 승인해 주세요.")
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS].rsplit("\n", 1)[0] + "\n  … (이하 생략)"
    return text


async def _default_sender(text: str) -> bool:
    from app.services.ceo_notify import send_ceo_text

    return await send_ceo_text(text)


async def send_daily_digest(pool: Any, *, now: datetime | None = None,
                            sender: Callable[[str], Awaitable[bool]] | None = None) -> dict:
    """KST 날짜·테넌트당 1회 발송. 두 블루그린 슬롯이 동시에 불러도 advisory lock 과 기록 PK 로 중복이 없다."""
    send = sender or _default_sender
    digest_date: date = (now or _kst_now()).astimezone(KST).date()
    result = {"date": digest_date.isoformat(), "sent": 0, "skipped": 0, "failed": 0}
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _LOCK_KEY)
        items = await fetch_review_queue(conn, None, now=now)
        by_tenant: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            by_tenant[item["tenant_id"]].append(item)
        for tenant_id, tenant_items in by_tenant.items():
            if await conn.fetchval(
                "SELECT 1 FROM canonical_review_digest_log WHERE digest_date=$1 AND tenant_id=$2::uuid",
                digest_date, tenant_id,
            ):
                result["skipped"] += 1
                continue
            if not await send(format_digest(tenant_items)):
                result["failed"] += 1
                logger.warning("canonical_review_digest_send_failed", tenant_id=tenant_id)
                continue
            await conn.execute(
                "INSERT INTO canonical_review_digest_log(digest_date,tenant_id,pending_count) "
                "VALUES($1,$2::uuid,$3) ON CONFLICT DO NOTHING",
                digest_date, tenant_id, len(tenant_items),
            )
            result["sent"] += 1
    logger.info("canonical_review_digest_done", **result)
    return result
