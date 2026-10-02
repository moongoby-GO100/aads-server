#!/usr/bin/env python3
"""READ 자동 승인 레시피를 일괄 비활성화한다 (기본은 dry-run).

SMART_BROWSER_AUTO_APPROVE_READ 롤백 절차: 플래그를 0 으로 내린 뒤 이 스크립트로
decided_by='system:auto_read_policy' 가 만든 레시피를 enabled=false 로 돌린다.
삭제하지 않는다 — 행·버전·감사 근거는 그대로 남고 플레이어만 못 쓰게 된다.
재실행해도 이미 꺼진 행은 건드리지 않는다.

사용 (운영 컨테이너 안):
    python3 scripts/smart_browser_revoke_auto_read.py            # dry-run
    python3 scripts/smart_browser_revoke_auto_read.py --apply    # 실제 반영
    python3 scripts/smart_browser_revoke_auto_read.py --tenant <tenant_id>
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.work_recipe.auto_approve import AUTO_DECIDER  # noqa: E402

_ACTIVE_SQL = """
    SELECT id, tenant_id, name, domain, version, created_at
      FROM work_recipes
     WHERE created_by = $1 AND enabled = TRUE
       AND ($2::uuid IS NULL OR tenant_id = $2::uuid)
     ORDER BY created_at, id
"""


async def revoke(conn: Any, *, apply: bool, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """대상 행 목록을 돌려준다. apply=True 면 같은 트랜잭션 안에서 비활성화한다."""
    tenant = str(uuid.UUID(tenant_id)) if tenant_id else None
    async with conn.transaction():
        rows = [dict(r) for r in await conn.fetch(_ACTIVE_SQL + " FOR UPDATE", AUTO_DECIDER, tenant)]
        if apply and rows:
            await conn.execute(
                "UPDATE work_recipes SET enabled = FALSE, updated_at = NOW() WHERE id = ANY($1::uuid[])",
                [str(r["id"]) for r in rows],
            )
    return rows


def render(rows: list[dict[str, Any]], *, apply: bool) -> str:
    verb = "비활성화함" if apply else "비활성화 대상 (dry-run, --apply 로 반영)"
    lines = [f"[revoke_auto_read] {verb}: {len(rows)}건"]
    lines += [f"  - {r['domain']} / {r['name']} v{r['version']} (id={r['id']})" for r in rows]
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await revoke(conn, apply=args.apply, tenant_id=args.tenant)
    finally:
        await conn.close()
    print(render(rows, apply=args.apply))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제로 enabled=false 로 반영 (기본 dry-run)")
    parser.add_argument("--tenant", help="특정 테넌트만")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
