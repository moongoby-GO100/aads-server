"""클로브 정상 거래 자동 확정. 기본 비활성, 전표 작성은 기존 확정 경로에 위임한다."""
from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import ceil
from typing import Any

from app.services import obys_collection_core as core
from app.services import obys_collection_service as svc

logger = logging.getLogger(__name__)
ACTOR = "system:auto-confirm"
KST = timezone(timedelta(hours=9))
# 90일 내 양수 표본 30건 이상일 때만 P95의 5배 초과를 보류한다.
# 소표본/통상 고액 거래의 오탐을 줄이는 보수적인 초기 운영 기준(통계적 확정 판정이 아님).
HISTORY_DAYS = 90
OUTLIER_MIN_SAMPLES = 30
OUTLIER_PERCENTILE = Decimal("0.95")
OUTLIER_MULTIPLIER = Decimal("5")
ALLOWED_DIRECTIONS = {
    core.KIND_BANK: {"IN", "OUT"}, core.KIND_TAX: {"SALES", "PURCHASE"},
    core.KIND_CASH: {"SALES", "PURCHASE"}, core.KIND_CARD: {"OUT"},
}


def enabled() -> bool:
    return os.getenv("OBYS_AUTO_CONFIRM_ENABLED", "0") == "1"


def _identity(row: Any) -> tuple:
    return (str(row.get("tenant_id")), row.get("business_id"), row.get("data_kind"))


def _duplicate_key(row: Any) -> tuple:
    return (*_identity(row), row.get("occurred_on"), row.get("amount"), row.get("counterparty", ""))


def build_history(items: list[Any], ledger: list[Any], *, today: date) -> dict[str, Any]:
    """호출당 고정된 기준일과 사업장별 색인. 개인정보는 반환 요약/로그에 포함하지 않는다."""
    keys: dict[tuple, set] = defaultdict(set)
    amounts: dict[tuple, list] = defaultdict(list)
    for r in items:
        keys[_duplicate_key(r)].add(r.get("source_key"))
        day, amount = r.get("occurred_on"), r.get("amount")
        if (day and today - timedelta(days=HISTORY_DAYS) <= day <= today
                and amount is not None and amount > 0):
            amounts[_identity(r)].append((r.get("item_id"), Decimal(amount)))
    return {"today": today, "duplicates": keys, "amounts": amounts,
            "ledger": {(*_identity(r), r["source_key"]): r["item_hash"] for r in ledger}}


def classify_item(row: Any, history: dict[str, Any]) -> tuple[bool, list[str]]:
    """DB/환경변수/시계 접근 없는 순수 판정. today는 history가 제공한다."""
    reasons = []
    if row.get("direction") not in ALLOWED_DIRECTIONS.get(row.get("data_kind"), set()):
        reasons.append("direction_invalid")
    amount = Decimal(str(row.get("amount") or 0))
    if amount == 0:
        reasons.append("amount_zero")
    if amount < 0:
        reasons.append("amount_negative")
    if row.get("occurred_on") and row["occurred_on"] > history["today"]:
        reasons.append("future_date")
    if not str(row.get("source_key") or "").strip():
        reasons.append("source_key_missing")
    prior = history["ledger"].get((*_identity(row), row.get("source_key")))
    if prior is not None and prior != row.get("item_hash"):
        reasons.append("hash_conflict")
    if history["duplicates"].get(_duplicate_key(row), set()) - {row.get("source_key")}:
        reasons.append("duplicate_suspect")
    samples = sorted(a for item_id, a in history["amounts"].get(_identity(row), [])
                     if item_id != row.get("item_id"))
    if len(samples) >= OUTLIER_MIN_SAMPLES:
        p95 = samples[ceil(len(samples) * OUTLIER_PERCENTILE) - 1]
        if amount > p95 * OUTLIER_MULTIPLIER:
            reasons.append("amount_outlier")
    return not reasons, reasons


async def auto_confirm_business(
    tenant_id: Any, business_id: str, *, actor: str = ACTOR, limit: int = 500,
    dry_run: bool = False, since: datetime | None = None, preview_offset: int = 0,
) -> dict[str, Any]:
    """정상 항목만 잠금·검토·확정을 한 트랜잭션으로 수행한다.

    since는 수집 호출 직전 시각이다. backlog 허용 없이 since가 없으면 쓰지 않는다.
    dry_run은 플래그와 무관하게 읽기만 하며 backlog 전체를 미리 볼 수 있다.
    actor 인자를 받아도 자동 확정 감사 주체는 항상 ACTOR로 고정한다.
    """
    result: dict[str, Any] = {"dry_run": dry_run, "confirmed": 0, "normal": 0,
                              "exceptions": 0, "reasons": {}, "remaining": 0, "by_kind": {}}
    if not dry_run and not enabled():
        return {**result, "skipped": "disabled"}
    backlog = os.getenv("OBYS_AUTO_CONFIRM_BACKLOG", "0") == "1"
    if not dry_run and not backlog and since is None:
        return {**result, "skipped": "collection_start_required"}
    tenant = svc._scope(tenant_id)
    limit = max(1, min(int(limit), svc.BULK_MAX_LIMIT))
    if preview_offset and not dry_run:
        raise ValueError("preview_offset_requires_dry_run")
    cutoff = None if backlog or (dry_run and since is None) else since
    args = (tenant, business_id, list(core.DATA_KINDS), cutoff)
    clause = ("i.tenant_id=$1 AND i.business_id=$2 AND i.data_kind=ANY($3::text[]) "
              "AND i.stage='review_box' AND ($4::timestamptz IS NULL OR i.first_seen_at >= $4)")
    conn = await svc._connect()
    try:
        async with conn.transaction(readonly=dry_run):
            await svc._require_business(conn, tenant, business_id)
            table = await conn.fetchval("SELECT to_regclass('obys_auto_confirm_exception')")
            # 가장 오래 검사하지 않은 항목 우선: 보류 항목이 backlog의 다음 배치를 막지 않는다.
            join = ("LEFT JOIN obys_auto_confirm_exception e ON e.item_id=i.item_id " if table else "")
            order = "e.detected_at NULLS FIRST, " if table else ""
            rows = await conn.fetch(
                f"SELECT i.* FROM obys_clobe_item i {join}WHERE {clause} "
                f"ORDER BY {order}i.data_kind, i.source_key, i.item_id LIMIT $5 OFFSET $6 "
                + ("" if dry_run else "FOR UPDATE OF i SKIP LOCKED"),
                *args, limit, max(0, int(preview_offset)))
            if not dry_run:
                # 수동 확정과 같은 원장 키 잠금. 이력 조회 이후 확정 원장이 변경되는 경쟁 방지.
                for r in sorted(rows, key=lambda r: (r["data_kind"], r["source_key"], str(r["item_id"]))):
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                                       f"{tenant}|{business_id}|{r['data_kind']}|{r['source_key']}")
            today = datetime.now(KST).date()
            items = await conn.fetch(
                "SELECT * FROM obys_clobe_item WHERE tenant_id=$1 AND business_id=$2 "
                "AND data_kind=ANY($3::text[]) AND stage IN ('confirmed','reviewed','review_box') "
                "AND (occurred_on >= $4 OR occurred_on=ANY($5::date[]))",
                tenant, business_id, list(core.DATA_KINDS), today - timedelta(days=HISTORY_DAYS),
                list({r["occurred_on"] for r in rows if r["occurred_on"] is not None}))
            ledger = await conn.fetch(
                "SELECT * FROM obys_clobe_ledger_entry WHERE tenant_id=$1 AND business_id=$2",
                tenant, business_id)
            history = build_history(items, ledger, today=today)
            good, bad = [], []
            counts: Counter = Counter()
            for r in rows:
                ok, reasons = classify_item(r, history)
                summary = result["by_kind"].setdefault(r["data_kind"], {"normal": 0, "exceptions": 0, "reasons": {}})
                summary["normal" if ok else "exceptions"] += 1
                for reason in reasons:
                    summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
                counts.update(reasons)
                if ok:
                    good.append(r["item_id"])
                else:
                    bad.append((r["item_id"], reasons))
            if not dry_run:
                if table:
                    for item_id, reasons in bad:
                        await conn.execute(
                            "INSERT INTO obys_auto_confirm_exception (item_id, tenant_id, business_id, reasons) "
                            "VALUES ($1,$2,$3,$4::text[]) ON CONFLICT (item_id) DO UPDATE SET "
                            "reasons=EXCLUDED.reasons, detected_at=now(), resolved_at=NULL",
                            item_id, tenant, business_id, reasons)
                    await conn.execute(
                        "UPDATE obys_auto_confirm_exception SET resolved_at=now() "
                        "WHERE tenant_id=$1 AND business_id=$2 AND item_id=ANY($3::uuid[]) AND resolved_at IS NULL",
                        tenant, business_id, good)
                elif rows:
                    logger.warning("obys_auto_confirm_exception table missing; reason persistence skipped")
                reviewed = await conn.fetch(
                    "UPDATE obys_clobe_item SET stage='reviewed', reviewed_by=$3, reviewed_at=now() "
                    "WHERE tenant_id=$1 AND item_id=ANY($2::uuid[]) AND stage='review_box' RETURNING *",
                    tenant, good, ACTOR)
                reviewed = sorted(reviewed, key=lambda r: (r["data_kind"], r["source_key"], str(r["item_id"])))
                result["confirmed"] = await svc._post_reviewed_rows(conn, tenant, business_id, reviewed, ACTOR)
            result.update(normal=len(good), exceptions=len(bad), reasons=dict(counts))
            result["remaining"] = int(await conn.fetchval(
                f"SELECT count(*) FROM obys_clobe_item i WHERE {clause}", *args) or 0)
        return result
    finally:
        await conn.close()

