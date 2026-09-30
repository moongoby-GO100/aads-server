#!/usr/bin/env python3
"""마일스톤 자동전환 초안의 비어 있는 source_message_ids 를 채운다 (기본은 dry-run).

2026-09-29 이전 save_milestone_draft 는 source_message_ids 를 INSERT 하지 않아
source_mode=milestone_auto_advance 초안의 "원문 메시지 → 설계버전" 연결이 끊겨 있다.
이 스크립트는 지금 코드가 전환 시점에 골랐을 메시지를 **확실히 재현할 수 있는 행만**
채우고, 그렇지 않은 행은 비워 둔 채 사유를 출력한다.

특정 조건 (하나라도 어기면 미특정):
  1. 마일스톤 started_at 과 초안 created_at 차이가 TRANSITION_TOLERANCE 이내
     — 초안이 전환 순간에 만들어졌으므로 그 시각이 "전환 시점" 이다.
  2. 초안 세션이 지금도 같은 테넌트 목표의 chat_session 링크로 남아 있다.
  3. 채팅 경로와 같은 기준(_fetch_recent_context_messages, 기본 8턴)으로
     created_at <= 초안 created_at 창을 잡았을 때 사용자 요청이 1건 이상 있다.

classification·revision·events 는 건드리지 않는다. UPDATE 는 source_message_ids 가
여전히 비어 있는 행에만 걸린다(재실행 안전).

사용 (운영 컨테이너 안):
    python3 scripts/backfill_milestone_draft_sources.py            # dry-run
    python3 scripts/backfill_milestone_draft_sources.py --apply    # 실제 반영
    python3 scripts/backfill_milestone_draft_sources.py --trace <draft_id> --tenant <tenant_id>
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.directive_draft_service import (  # noqa: E402
    _fetch_recent_context_messages,
    _source_ids_with_user_request,
    load_draft_trace,
)

TRANSITION_TOLERANCE = dt.timedelta(seconds=60)

CANDIDATES_SQL = """
SELECT d.id, d.tenant_id, d.session_id, d.created_at,
       d.classification->>'goal_id' AS goal_id,
       d.classification->>'milestone_id' AS milestone_id,
       m.started_at AS milestone_started_at,
       EXISTS (
           SELECT 1 FROM goal_task_links l
             JOIN goals g ON g.id = l.goal_id AND g.tenant_id = d.tenant_id
            WHERE l.goal_id = (d.classification->>'goal_id')::uuid
              AND l.task_type = 'chat_session'
              AND l.task_id = d.session_id::text
              AND COALESCE(l.link_state, 'active') = 'active'
       ) AS session_linked
  FROM directive_drafts d
  LEFT JOIN milestones m ON m.id = (d.classification->>'milestone_id')::uuid
 WHERE d.classification->>'source_mode' = 'milestone_auto_advance'
   AND cardinality(d.source_message_ids) = 0
 ORDER BY d.created_at
"""

APPLY_SQL = """
UPDATE directive_drafts SET source_message_ids = $3::uuid[]
 WHERE id = $1 AND tenant_id = $2
   AND classification->>'source_mode' = 'milestone_auto_advance'
   AND cardinality(source_message_ids) = 0
"""


async def plan_backfill(conn) -> list[dict[str, Any]]:
    plans = []
    for row in await conn.fetch(CANDIDATES_SQL):
        plan: dict[str, Any] = {
            "draft_id": str(row["id"]), "session_id": str(row["session_id"]),
            "goal_id": row["goal_id"], "milestone_id": row["milestone_id"],
            "draft_created_at": row["created_at"].isoformat(),
            "decision": "unresolved", "reason": None, "source_message_ids": [],
        }
        plans.append(plan)
        started = row["milestone_started_at"]
        if started is None or abs(row["created_at"] - started) > TRANSITION_TOLERANCE:
            plan["reason"] = f"transition_time_unknown milestone_started_at={started}"
            continue
        if not row["session_linked"]:
            plan["reason"] = "session_not_linked_to_goal"
            continue
        rows = await _fetch_recent_context_messages(
            conn, session_id=row["session_id"], tenant_id=row["tenant_id"],
            as_of=row["created_at"],
        )
        source_ids = _source_ids_with_user_request(rows)
        if not source_ids:
            plan["reason"] = f"no_user_request_in_window rows={len(rows)}"
            continue
        plan.update(
            decision="fill",
            reason=(f"window={len(rows)} msgs {rows[0]['created_at'].isoformat()}"
                    f" .. {rows[-1]['created_at'].isoformat()}"
                    f" (transition {started.isoformat()})"),
            source_message_ids=[str(value) for value in source_ids],
            _tenant_id=row["tenant_id"], _ids=source_ids, _id=row["id"],
        )
    return plans


async def apply_backfill(conn, plans: list[dict[str, Any]]) -> int:
    updated = 0
    async with conn.transaction():
        for plan in plans:
            if plan["decision"] != "fill":
                continue
            status = await conn.execute(APPLY_SQL, plan["_id"], plan["_tenant_id"], plan["_ids"])
            updated += int(status.split()[-1])
    return updated


def _public(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if not k.startswith("_")}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제 UPDATE (기본은 dry-run)")
    parser.add_argument("--trace", metavar="DRAFT_ID", help="초안 1건의 3단 추적 출력")
    parser.add_argument("--tenant", metavar="TENANT_ID", help="--trace 대상 테넌트")
    args = parser.parse_args()

    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        if args.trace:
            if not args.tenant:
                parser.error("--trace 에는 --tenant 가 필요하다")
            trace = await load_draft_trace(conn, tenant_id=args.tenant, draft_id=args.trace)
            print(json.dumps(trace, ensure_ascii=False, indent=2, default=str))
            return 0 if trace["linked"] else 1
        plans = await plan_backfill(conn)
        for plan in plans:
            print(json.dumps(_public(plan), ensure_ascii=False))
        filled = sum(1 for plan in plans if plan["decision"] == "fill")
        print(f"대상 {len(plans)}행: 백필 가능 {filled}행 / 미특정 {len(plans) - filled}행"
              f" ({'apply' if args.apply else 'dry-run'})")
        if args.apply:
            print(f"UPDATE 반영 {await apply_backfill(conn, plans)}행")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
