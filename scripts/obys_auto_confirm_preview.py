#!/usr/bin/env python3
"""클로브 자동 확정 미리보기 — 사업장·종류별 건수만 출력(읽기 전용)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import obys_auto_confirm as auto  # noqa: E402
from app.services import obys_collection_service as svc  # noqa: E402


async def preview(tenant_id: str, business_id: str | None = None) -> None:
    tenant = svc._scope(tenant_id)
    conn = await svc._connect()
    try:
        async with conn.transaction(readonly=True):
            businesses = await conn.fetch(
                "SELECT id FROM yeoljeong_businesses WHERE tenant_id=$1 AND deleted_at IS NULL "
                "AND ($2::text IS NULL OR id=$2) ORDER BY id", tenant, business_id)
    finally:
        await conn.close()
    print("사업장\t종류\t정상\t이상\t사유별 건수")
    for business in businesses:
        totals: dict[str, dict] = {}
        offset = 0
        while True:
            result = await auto.auto_confirm_business(
                tenant_id, business["id"], dry_run=True, limit=5000, preview_offset=offset)
            for kind, counts in result["by_kind"].items():
                total = totals.setdefault(kind, {"normal": 0, "exceptions": 0, "reasons": Counter()})
                total["normal"] += counts["normal"]
                total["exceptions"] += counts["exceptions"]
                total["reasons"].update(counts["reasons"])
            scanned = result["normal"] + result["exceptions"]
            offset += scanned
            if not scanned or offset >= result["remaining"]:
                break
        for kind, counts in sorted(totals.items()):
            reasons = json.dumps(dict(counts["reasons"]), ensure_ascii=False, sort_keys=True)
            print(f"{business['id']}\t{kind}\t{counts['normal']}\t{counts['exceptions']}\t{reasons}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--business-id")
    args = parser.parse_args()
    asyncio.run(preview(args.tenant_id, args.business_id))


if __name__ == "__main__":
    main()
