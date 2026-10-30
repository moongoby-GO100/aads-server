"""완료 마일스톤의 검토 중 업무를 보정한다. 기본 실행은 읽기 전용이다."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.goal_work_item_sync import sync_milestone


async def backfill(conn, *, goal_id=None, milestone_id=None, apply=False):
    candidates = await conn.fetch(
        """SELECT w.id, w.milestone_id, w.goal_id, w.tenant_id, w.project
           FROM work_items w JOIN milestones m ON m.id=w.milestone_id
           WHERE m.status='completed' AND w.status='in_review'
             AND ($1::uuid IS NULL OR w.goal_id=$1::uuid)
             AND ($2::uuid IS NULL OR m.id=$2::uuid)
           ORDER BY w.milestone_id, w.id""", goal_id, milestone_id,
    )
    changed = 0
    if apply:
        for mid in dict.fromkeys(str(row['milestone_id']) for row in candidates):
            async with conn.transaction():
                status = await conn.fetchval(
                    'SELECT status FROM milestones WHERE id=$1::uuid FOR UPDATE', mid,
                )
                if status == 'completed':
                    changed += await sync_milestone(conn, mid, 'completed')
    return {'mode': 'apply' if apply else 'dry-run', 'count': len(candidates),
            'changed': changed, 'items': [dict(row) for row in candidates]}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--goal-id', type=UUID)
    parser.add_argument('--milestone-id', type=UUID)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    return parser.parse_args(argv)


async def main(args):
    import asyncpg

    conn = await asyncpg.connect(os.environ['DATABASE_URL'], timeout=10)
    try:
        async with conn.transaction(readonly=not args.apply):
            result = await backfill(conn, goal_id=args.goal_id,
                                    milestone_id=args.milestone_id, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        await conn.close()


if __name__ == '__main__':
    asyncio.run(main(parse_args()))
