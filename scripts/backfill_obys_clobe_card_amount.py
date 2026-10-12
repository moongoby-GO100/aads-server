#!/usr/bin/env python3
"""클로브 카드승인 검토함 항목의 amount/counterparty 를 payload 에서 다시 계산한다 (기본은 dry-run).

2026-10-12 운영 실측: obys_clobe_item data_kind='card_approval' 154건이 전부 amount IS NULL,
counterparty='' 였다. payload 의 실제 키는 usedAmountKrw / memberStoreName 인데 normalize 가
usedAmount / merchantName 만 봐서 비었다. normalize 는 고쳤지만, 같은 거래를 다시 수집해도
item_hash 가 같아 'unchanged' 로 건너뛰므로 기존 행은 이 스크립트로만 채워진다.

item_hash 는 건드리지 않는다 — 근거: item_hash 는 payload(원본)에서 _VOLATILE 키를 뺀 canonical JSON 의
sha256 이다(obys_collection_core.item_hash). 정규화된 amount/counterparty 열은 입력에 들어가지 않으므로
열을 고쳐도 해시는 그대로이고, 재수집이 같은 해시를 만나 중복 버전을 쌓지 않는다.
UNIQUE(tenant, business, data_kind, source_key, item_hash) 제약도 영향받지 않는다.

규칙:
  - 대상: stage='review_box' 이고 data_kind='card_approval' 인 행만. reviewed/confirmed/rejected/superseded 는 건드리지 않는다
    (확정 원장 obys_clobe_ledger_entry 도 건드리지 않는다).
  - 계산은 서비스와 같은 함수(core.card_amount / 서비스 normalize 의 counterparty 규칙)를 쓴다.
  - 새 값이 비어 있으면(amount None, counterparty '') 기존 값을 지우지 않는다. 이미 같은 값이면 쓰지 않는다.
  - dry-run 은 READ ONLY 트랜잭션이고 행을 잠그지 않는다(FOR UPDATE 없음) — 운영 수집·검토와 경합하지 않는다.
  - --apply 일 때만 행 잠금(FOR UPDATE, lock_timeout 5초) + UPDATE. 단일 트랜잭션이고 잠금 아래에서 다시 계산한다.
    UPDATE 는 `IS DISTINCT FROM` 조건이라 두 번 돌려도 두 번째는 0건이다(멱등).
  - 차단 범위(core.blocked_tenants / blocked_businesses — 라일론 등)는 범위 인자와 무관하게 항상 SELECT 에서 제외한다.
    차단 대상을 --tenant-id/--business-id 로 지정해도 0건이다.
  - --tenant-id / --business-id 로 범위를 좁힐 수 있고, 대상 건수는 (tenant, business) 별로 출력한다.

사용 (운영 컨테이너 안, OBYS_DATABASE_URL 필요):
    python3 scripts/backfill_obys_clobe_card_amount.py                          # dry-run, 전체 범위
    python3 scripts/backfill_obys_clobe_card_amount.py --tenant-id <uuid> --business-id <id>
    python3 scripts/backfill_obys_clobe_card_amount.py --tenant-id <uuid> --apply
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
    "data_kind = 'card_approval' AND stage = 'review_box' "
    "AND ($1::uuid IS NULL OR tenant_id = $1::uuid) AND ($2::text IS NULL OR business_id = $2::text) "
    "AND tenant_id::text <> ALL($3::text[]) AND COALESCE(business_id, '') <> ALL($4::text[])"
)
SELECT_SQL = (
    "SELECT item_id, tenant_id, business_id, payload, amount, counterparty FROM obys_clobe_item "
    f"WHERE {_SCOPE_WHERE} ORDER BY item_id"
)
# 잠금은 apply 에서만. ORDER BY item_id 는 잠금 순서를 고정해 다른 쓰기 경로와의 교착 가능성을 줄인다.
SELECT_FOR_UPDATE_SQL = SELECT_SQL + " FOR UPDATE"
UPDATE_SQL = (
    "UPDATE obys_clobe_item SET amount = $2, counterparty = $3 "
    "WHERE item_id = $1 AND data_kind = 'card_approval' AND stage = 'review_box' "
    "AND (amount IS DISTINCT FROM $2 OR counterparty IS DISTINCT FROM $3)"
)
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"


def recompute(payload: Any) -> tuple[Any, str]:
    """payload → (amount, counterparty). normalize_item 과 같은 규칙."""
    raw = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    if not isinstance(raw, dict):
        return None, ""
    norm = core.normalize_item(core.KIND_CARD, raw)
    return norm["amount"], norm["counterparty"]


def plan_changes(rows: list[Any]) -> tuple[list[tuple[Any, Any, str]], int]:
    """(변경 목록[(item_id, amount, counterparty)], 변경 없음 건수)."""
    changes: list[tuple[Any, Any, str]] = []
    unchanged = 0
    for row in rows:
        amount, counterparty = recompute(row["payload"])
        new_amount = amount if amount is not None else row["amount"]
        new_counterparty = counterparty or row["counterparty"] or ""
        if new_amount == row["amount"] and new_counterparty == (row["counterparty"] or ""):
            unchanged += 1
            continue
        changes.append((row["item_id"], new_amount, new_counterparty))
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
            for item_id, amount, counterparty in changes:
                written += _affected(await conn.execute(UPDATE_SQL, item_id, amount, counterparty))
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
