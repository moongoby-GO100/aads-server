#!/usr/bin/env python3
"""클로브 은행거래 검토함 항목의 counterparty 를 payload 에서 다시 계산한다 (기본은 dry-run).

2026-10-14 운영 실측: obys_clobe_item data_kind='bank_transaction' stage='review_box' 4,968건 중
3,392건이 counterparty='' 였다. payload 의 transactionName 은 항상 "" 이고 실제 값은
businessEntityName(있을 때만) / transactionDescription(항상 있음)에 있다. normalize 는
businessEntityName → transactionDescription → transactionName 순으로 고쳤지만, 같은 거래를 다시 수집해도
item_hash 가 같아 'unchanged' 로 건너뛰므로 기존 행은 이 스크립트로만 채워진다.

item_hash 는 건드리지 않는다 — payload(원본)의 canonical JSON sha256 이라 정규화 열을 고쳐도 해시는 그대로다
(obys_collection_core.item_hash). UNIQUE(tenant, business, data_kind, source_key, item_hash) 도 영향받지 않는다.
amount/direction 등 다른 열은 쓰지 않는다 — counterparty 만 갱신한다.

규칙:
  - 대상: stage='review_box' 이고 data_kind='bank_transaction' 인 행만. reviewed/confirmed/rejected/superseded 와
    확정 원장 obys_clobe_ledger_entry 는 건드리지 않는다.
  - 계산은 서비스와 같은 함수(core.normalize_item)를 쓴다.
  - 새 값이 비어 있으면 기존 값을 지우지 않는다. 이미 같은 값이면 쓰지 않는다(IS DISTINCT FROM — 멱등).
  - dry-run 은 READ ONLY 트랜잭션이고 행을 잠그지 않는다.
  - --apply 일 때만 FOR UPDATE + lock_timeout 5초 + UPDATE, 단일 트랜잭션.
  - 차단 범위(core.blocked_tenants / blocked_businesses)는 범위 인자와 무관하게 항상 제외한다.
  - --tenant-id / --business-id 로 범위를 좁힐 수 있고, 대상 건수는 (tenant, business) 별로 출력한다.

사용 (운영 컨테이너 안, OBYS_DATABASE_URL 필요):
    python3 scripts/backfill_obys_clobe_bank_counterparty.py                          # dry-run, 전체 범위
    python3 scripts/backfill_obys_clobe_bank_counterparty.py --tenant-id <uuid> --business-id <id>
    python3 scripts/backfill_obys_clobe_bank_counterparty.py --tenant-id <uuid> --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import obys_collection_core as core  # noqa: E402

# $3/$4 는 수집 코어의 차단 범위(blocked_tenants/blocked_businesses). 범위 인자를 안 줘도 항상 적용된다.
_SCOPE_WHERE = (
    "data_kind = 'bank_transaction' AND stage = 'review_box' "
    "AND ($1::uuid IS NULL OR tenant_id = $1::uuid) AND ($2::text IS NULL OR business_id = $2::text) "
    "AND tenant_id::text <> ALL($3::text[]) AND COALESCE(business_id, '') <> ALL($4::text[])"
)
SELECT_SQL = (
    "SELECT item_id, tenant_id, business_id, payload, counterparty FROM obys_clobe_item "
    f"WHERE {_SCOPE_WHERE} ORDER BY item_id"
)
SELECT_FOR_UPDATE_SQL = SELECT_SQL + " FOR UPDATE"
UPDATE_SQL = (
    "UPDATE obys_clobe_item SET counterparty = $2 "
    "WHERE item_id = $1 AND data_kind = 'bank_transaction' AND stage = 'review_box' "
    "AND counterparty IS DISTINCT FROM $2"
)
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"


def recompute(payload: Any) -> str:
    """payload → counterparty. normalize_item 과 같은 규칙."""
    raw = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    if not isinstance(raw, dict):
        return ""
    return core.normalize_item(core.KIND_BANK, raw)["counterparty"]


def plan_changes(rows: list[Any]) -> tuple[list[tuple[Any, str]], int]:
    """(변경 목록[(item_id, counterparty)], 변경 없음 건수)."""
    changes: list[tuple[Any, str]] = []
    unchanged = 0
    for row in rows:
        current = row["counterparty"] or ""
        new_value = recompute(row["payload"]) or current
        if new_value == current:
            unchanged += 1
            continue
        changes.append((row["item_id"], new_value))
    return changes, unchanged


def count_by_scope(rows: list[Any], changed_ids: set[Any]) -> list[dict[str, Any]]:
    scopes: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["tenant_id"]), str(row["business_id"]))
        entry = scopes.setdefault(key, {"tenant_id": key[0], "business_id": key[1], "targets": 0, "would_change": 0})
        entry["targets"] += 1
        entry["would_change"] += row["item_id"] in changed_ids
    return [scopes[k] for k in sorted(scopes)]


def _affected(status: str) -> int:
    """asyncpg execute 의 'UPDATE n' 상태 문자열에서 n."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def backfill(
    conn: Any, *, apply: bool, tenant_id: str | None = None, business_id: str | None = None,
) -> dict[str, Any]:
    async with conn.transaction(readonly=not apply):
        if apply:
            await conn.execute(LOCK_TIMEOUT_SQL)
        rows = list(await conn.fetch(
            SELECT_FOR_UPDATE_SQL if apply else SELECT_SQL, tenant_id, business_id,
            sorted(core.blocked_tenants()), sorted(core.blocked_businesses()),
        ))
        changes, unchanged = plan_changes(rows)
        written = 0
        if apply:
            for item_id, counterparty in changes:
                written += _affected(await conn.execute(UPDATE_SQL, item_id, counterparty))
    return {
        "mode": "apply" if apply else "dry-run",
        "scope": {"tenant_id": tenant_id, "business_id": business_id},
        "excluded_by_policy": {"tenants": sorted(core.blocked_tenants()), "businesses": sorted(core.blocked_businesses())},
        "targets": len(rows),
        "changed": written,
        "would_change": len(changes),
        "unchanged": unchanged,
        "by_scope": count_by_scope(rows, {c[0] for c in changes}),
    }


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    from app.core.obys_db import obys_db_url

    conn = await asyncpg.connect(args.obys_dsn or obys_db_url(), timeout=10)
    try:
        result = await backfill(conn, apply=args.apply, tenant_id=args.tenant_id, business_id=args.business_id)
    finally:
        await conn.close()
    for key, value in result.items():
        if key == "by_scope":
            continue
        print(f"{key}: {value}")
    for entry in result["by_scope"]:
        print(f"  tenant={entry['tenant_id']} business={entry['business_id']} "
              f"targets={entry['targets']} would_change={entry['would_change']}")
    if not args.apply:
        print("dry-run: 쓰기 없음. 반영하려면 --apply")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="실제로 반영한다 (기본은 dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="명시적 dry-run (기본값과 같다)")
    parser.add_argument("--tenant-id", default=None, help="이 테넌트(UUID)만 대상 (기본 전체)")
    parser.add_argument("--business-id", default=None, help="이 사업장만 대상 (기본 전체)")
    parser.add_argument("--obys-dsn", default="", help="오비서 DB DSN (기본 $OBYS_DATABASE_URL)")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 와 --dry-run 은 함께 쓸 수 없다")
    if args.tenant_id:
        try:
            uuid.UUID(args.tenant_id)
        except ValueError:
            parser.error("--tenant-id 는 UUID 여야 한다")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
