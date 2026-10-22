#!/usr/bin/env python3
"""주도담당(owner_role_key / owner_session_id)이 비어 있는 active/draft 목표를 찾아 보정한다 (기본은 dry-run).

보정 규칙은 목표 생성 때와 같은 `goal_owner_resolver` 를 쓴다.
  - 이미 있는 칸은 덮어쓰지 않는다 (비어 있는 칸만 채운다).
  - 후보가 하나로 확정되는 건만 쓴다. 모호한 건은 후보와 함께 보고만 한다.
  - goals.updated_at 은 건드리지 않는다 — 휴면 판정의 근거 시각이라, 보정으로 올리면
    잠자던 목표가 살아난 것처럼 보인다.
  - 세션을 만들지 않는다 (승인 카드 없는 세션 생성 금지).

사용 (운영 컨테이너 안):
    python3 scripts/goal_owner_backfill.py                     # dry-run (기본)
    python3 scripts/goal_owner_backfill.py --status active     # 상태 한정 (반복 가능)
    python3 scripts/goal_owner_backfill.py --apply             # 실제 반영 (별도 승인 후)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.goal_owner_resolver import find_goals_missing_owner, plan_owner_fix  # noqa: E402

APPLY_SQL = """
UPDATE goals
   SET owner_role_key = CASE WHEN COALESCE(owner_role_key, '') = '' THEN $3 ELSE owner_role_key END,
       owner_session_id = COALESCE(owner_session_id, $4::uuid)
 WHERE id = $1::uuid AND tenant_id = $2::uuid
   AND status = ANY($5::text[])
   AND (COALESCE(owner_role_key, '') = '' OR owner_session_id IS NULL)
"""


async def plan_all(conn: Any, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    return [await plan_owner_fix(conn, goal) for goal in await find_goals_missing_owner(conn, statuses=statuses)]


async def apply_plans(conn: Any, plans: list[dict[str, Any]], statuses: tuple[str, ...]) -> int:
    written = 0
    async with conn.transaction():
        for plan in plans:
            fix = plan.get("fix")
            if not fix:
                continue
            result = await conn.execute(
                APPLY_SQL, plan["goal_id"], plan["tenant_id"],
                fix["owner_role_key"], fix["owner_session_id"], list(statuses),
            )
            written += 1 if result == "UPDATE 1" else 0
    return written


def render(plans: list[dict[str, Any]], apply: bool) -> str:
    fixable = [p for p in plans if p.get("fix")]
    lines = [f"mode={'APPLY' if apply else 'DRY-RUN'} missing={len(plans)} fixable={len(fixable)} "
             f"ambiguous={len(plans) - len(fixable)}"]
    reasons = Counter(p.get("reason") for p in plans)
    lines += [f"  {reason}: {count}" for reason, count in sorted(reasons.items(), key=lambda kv: str(kv[0]))]
    for plan in plans:
        head = f"  {plan['goal_id'][:8]} [{plan['project']}/{plan['status']}] {str(plan.get('title') or '')[:40]}"
        if plan.get("fix"):
            lines.append(f"{head} -> role={plan['fix']['owner_role_key']} session={plan['fix']['owner_session_id']}")
        else:
            cands = ", ".join(f"{c['role_key']}@{c['session_id'][:8]}" for c in plan.get("candidates", [])[:5])
            lines.append(f"{head} !! 모호({plan.get('reason')}) 후보: {cands or '없음'}")
    return "\n".join(lines)


async def run(statuses: tuple[str, ...], apply: bool) -> int:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        plans = await plan_all(conn, statuses)
        print(render(plans, apply))
        if not apply:
            print("dry-run: 아무것도 쓰지 않았다. 반영하려면 --apply")
            return 0
        print(f"applied: {await apply_plans(conn, plans, statuses)}")
        return 0
    finally:
        await conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="append", choices=["active", "draft"],
                        help="대상 상태 (기본 active+draft, 반복 가능)")
    parser.add_argument("--apply", action="store_true", help="실제로 UPDATE 한다 (기본은 dry-run)")
    args = parser.parse_args(argv)
    return asyncio.run(run(tuple(args.status or ("active", "draft")), args.apply))


if __name__ == "__main__":
    sys.exit(main())
