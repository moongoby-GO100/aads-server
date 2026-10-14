#!/usr/bin/env python3
"""이미 확정된 클로브 원장(obys_clobe_ledger_entry)에 전표 초안을 만든다 (기본은 dry-run).

확정 시점 훅(obys_collection_service._draft_voucher)이 생기기 전에 확정된 건을 채우는 용도다.
전표 생성 규칙은 서비스와 같은 모듈(app.services.obys_clobe_voucher)을 쓴다 — 매핑표를 두 벌로 두지 않는다.

규칙:
  - --business-id 필수. 사업장은 파일럿 목록(OBYS_CLOBE_VOUCHER_BUSINESSES, 기본 biz-chicblack)에 있어야 하고
    차단 범위(core.blocked_tenants / blocked_businesses)는 환경변수와 무관하게 항상 거절한다.
  - 대상: 해당 사업장의 확정 원장 중 source_type='clobe', source_id=entry_id 전표가 아직 없는 건.
  - 멱등: 전표 존재 검사 + 어드바이저리 락 + UNIQUE(tenant,business,source_type,source_id). 두 번 돌려도 전표는 1건.
  - 전표 status 는 draft/needs_review 뿐이다. 승인·확정(approved/posted)은 만들지 않는다.
  - dry-run 은 READ ONLY 트랜잭션이다. --apply 만 쓰며 lock_timeout 5초, 단일 트랜잭션(하나라도 실패하면 전부 롤백).

사용 (운영 컨테이너 안, OBYS_DATABASE_URL 필요):
    python3 scripts/backfill_obys_clobe_vouchers.py --business-id biz-chicblack            # dry-run
    python3 scripts/backfill_obys_clobe_vouchers.py --business-id biz-chicblack --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import obys_clobe_voucher as voucher  # noqa: E402

TENANT_SQL = "SELECT tenant_id FROM yeoljeong_businesses WHERE id = $1 AND deleted_at IS NULL"
PENDING_SQL = (
    "SELECT e.entry_id, e.data_kind, e.direction, e.occurred_on, e.amount, i.counterparty, i.payload "
    "FROM obys_clobe_ledger_entry e JOIN obys_clobe_item i ON i.item_id = e.item_id "
    "WHERE e.tenant_id = $1 AND e.business_id = $2 "
    "AND NOT EXISTS (SELECT 1 FROM yeoljeong_journal_vouchers v WHERE v.tenant_id = e.tenant_id "
    " AND v.business_id = e.business_id AND v.source_type = 'clobe' AND v.source_id = e.entry_id "
    " AND v.reversal_of_id IS NULL) "
    "ORDER BY e.confirmed_at, e.entry_id"
)
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
ACTOR = "backfill:obys_clobe_vouchers"


class BackfillRefused(Exception):
    pass


def check_scope(tenant_id: Any, business_id: str) -> None:
    if not voucher.voucher_enabled(tenant_id, business_id):
        raise BackfillRefused(
            f"{business_id}: 파일럿 대상이 아니거나 차단 범위다 ({voucher.PILOT_ENV}={sorted(voucher.pilot_businesses())})")


def plan(rows: list[Any]) -> dict[str, Any]:
    """쓰기 없이 결과를 예측한다. create_draft_for_entry 와 같은 build_draft 를 쓴다."""
    out: dict[str, Any] = {"would_create": 0, "unclassified": 0, "skipped": {}}
    for row in rows:
        try:
            draft = voucher.build_draft(
                row["data_kind"], direction=row["direction"], occurred_on=row["occurred_on"], amount=row["amount"],
                counterparty=row["counterparty"] or "", payload=voucher._payload(row["payload"]))
        except voucher.VoucherSkip as skip:
            out["skipped"][skip.reason] = out["skipped"].get(skip.reason, 0) + 1
            continue
        out["would_create"] += 1
        out["unclassified"] += draft.unclassified
    return out


async def backfill(conn: Any, *, apply: bool, business_id: str) -> dict[str, Any]:
    tenant_id = await conn.fetchval(TENANT_SQL, business_id)
    if tenant_id is None:
        raise BackfillRefused(f"{business_id}: 사업장을 찾을 수 없다")
    check_scope(tenant_id, business_id)
    async with conn.transaction(readonly=not apply):
        if apply:
            await conn.execute(LOCK_TIMEOUT_SQL)
        rows = list(await conn.fetch(PENDING_SQL, tenant_id, business_id))
        result: dict[str, Any] = {
            "mode": "apply" if apply else "dry-run", "tenant_id": str(tenant_id), "business_id": business_id,
            "pending_entries": len(rows)}
        if not apply:
            result.update(plan(rows))
            return result
        outcomes: dict[str, int] = {}
        for row in rows:
            outcome = await voucher.create_draft_for_entry(
                conn, tenant_id=tenant_id, business_id=business_id, entry_id=row["entry_id"], item=row, actor=ACTOR)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        result["created"] = outcomes.get("created", 0)
        result["outcomes"] = outcomes
        return result


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    from app.core.obys_db import obys_db_url

    conn = await asyncpg.connect(args.obys_dsn or obys_db_url(), timeout=10)
    try:
        result = await backfill(conn, apply=args.apply, business_id=args.business_id)
    except BackfillRefused as exc:
        print(f"거절: {exc}", file=sys.stderr)
        return 2
    finally:
        await conn.close()
    for key, value in result.items():
        print(f"{key}: {value}")
    if not args.apply:
        print("dry-run: 쓰기 없음. 반영하려면 --apply")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--business-id", required=True, help="대상 사업장 (필수, 파일럿 목록에 있어야 한다)")
    parser.add_argument("--apply", action="store_true", help="실제로 반영한다 (기본은 dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="명시적 dry-run (기본값과 같다)")
    parser.add_argument("--obys-dsn", default="", help="오비서 DB DSN (기본 $OBYS_DATABASE_URL)")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 와 --dry-run 은 함께 쓸 수 없다")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
