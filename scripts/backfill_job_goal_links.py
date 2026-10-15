#!/usr/bin/env python3
"""goal_id 가 비어 있는 러너 작업을 지시서 GOAL_ID/MILESTONE_ID 줄로 채운다 (기본은 dry-run).

PRD goal-priority-dormancy-autoapproval-display-prd rev5 §6-1 I-3 의 근거 (b) **만** 쓴다.
세션 단일 후보 (c) 는 제출 순간의 상태에 달린 판정이라 과거 작업에 소급하지 않는다 —
추정 연결 금지(I-2 B).

연결 가능 조건은 `_link_job_to_goal_explicit` 과 같다.
  - 지시서에 `GOAL_ID:` 줄이 있고 UUID 형식이다 (`parse_goal_binding` 과 같은 해석)
  - 목표가 실재하고 작업과 같은 tenant·project 이다
  - 목표 상태가 draft/active, 또는 blocked + 열린(pending/in_progress/blocked) 마일스톤 명시
  - MILESTONE_ID 가 그 목표의 마일스톤이 아니면 (blocked 가 아닐 때) 목표에만 연결한다

쓰는 것은 pipeline_jobs.goal_id/milestone_id **두 칸뿐**이다. updated_at 은 건드리지 않는다 —
작업 시각이 곧 목표의 마지막 활동 시각(휴면 판정 근거)이라, 과거 작업을 만져 시각을
올리면 휴면 목표가 살아난 것처럼 보인다. goal_task_links 도 만들지 않는다(링크 행은 제출 시점의
의미를 가지므로 소급하지 않는다). 재실행 안전: goal_id IS NULL 인 행에만 걸린다.

사용 (운영 컨테이너 안):
    python3 scripts/backfill_job_goal_links.py                  # dry-run (기본)
    python3 scripts/backfill_job_goal_links.py --days 30        # 조회 기간
    python3 scripts/backfill_job_goal_links.py --apply          # 실제 반영 (별도 승인 후)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.goal_binding import parse_goal_binding  # noqa: E402

OPEN_MILESTONE_STATUSES = ("pending", "in_progress", "blocked")
LINKABLE_GOAL_STATUSES = ("draft", "active", "blocked")

CANDIDATES_SQL = """
SELECT j.job_id, j.tenant_id::text AS tenant_id, j.project, j.instruction, j.created_at
  FROM pipeline_jobs j
 WHERE j.goal_id IS NULL
   AND j.created_at > now() - make_interval(days => $1::int)
   AND j.instruction LIKE '%GOAL_ID:%'
   AND COALESCE(j.status, '') <> 'dedup_blocked'
   AND COALESCE(j.phase, '') <> 'dedup_blocked'
 ORDER BY j.created_at
"""

GOALS_SQL = """
SELECT id::text AS goal_id, tenant_id::text AS tenant_id, project, status
  FROM goals WHERE id = ANY($1::uuid[])
"""

MILESTONES_SQL = """
SELECT id::text AS milestone_id, goal_id::text AS goal_id, tenant_id::text AS tenant_id,
       project, status
  FROM milestones WHERE id = ANY($1::uuid[])
"""

APPLY_SQL = """
UPDATE pipeline_jobs SET goal_id = $2::uuid, milestone_id = $3::uuid
 WHERE job_id = $1 AND goal_id IS NULL AND tenant_id = $4::uuid AND project = $5
"""


def plan_link(
    job: Mapping[str, Any],
    goal: Optional[Mapping[str, Any]],
    milestone: Optional[Mapping[str, Any]],
) -> tuple[Optional[tuple[str, Optional[str]]], str]:
    """((goal_id, milestone_id) | None, 사유). 규칙은 `_link_job_to_goal_explicit` 과 같다."""
    binding = parse_goal_binding(job.get("instruction"))
    if not binding.is_explicit:
        return None, "no_directive"
    if not goal:
        return None, "goal_not_found"
    if goal["tenant_id"] != job["tenant_id"]:
        return None, "cross_tenant"
    if goal["project"] != job["project"]:
        return None, "cross_project"
    status = goal["status"]
    if status not in LINKABLE_GOAL_STATUSES:
        return None, "goal_inactive"
    blocked = status == "blocked"

    resolved: Optional[str] = binding.milestone_id
    if resolved:
        valid = bool(
            milestone
            and milestone["goal_id"] == goal["goal_id"]
            and milestone["tenant_id"] == job["tenant_id"]
            and milestone["project"] == job["project"]
        )
        if not valid:
            if blocked:
                return None, "blocked_milestone_mismatch"
            resolved = None
        elif blocked and milestone["status"] not in OPEN_MILESTONE_STATUSES:
            return None, "blocked_milestone_closed"
    elif blocked:
        return None, "blocked_milestone_required"
    return (goal["goal_id"], resolved), "linkable"


async def _plan_all(conn: Any, days: int) -> tuple[list[dict[str, Any]], Counter]:
    jobs = [dict(r) for r in await conn.fetch(CANDIDATES_SQL, days)]
    goal_ids: set[str] = set()
    milestone_ids: set[str] = set()
    for job in jobs:
        binding = parse_goal_binding(job["instruction"])
        if binding.goal_id:
            goal_ids.add(binding.goal_id)
        if binding.milestone_id:
            milestone_ids.add(binding.milestone_id)
    goals = {r["goal_id"]: dict(r) for r in await conn.fetch(GOALS_SQL, sorted(goal_ids))} if goal_ids else {}
    milestones = (
        {r["milestone_id"]: dict(r) for r in await conn.fetch(MILESTONES_SQL, sorted(milestone_ids))}
        if milestone_ids else {}
    )
    plans: list[dict[str, Any]] = []
    reasons: Counter = Counter()
    for job in jobs:
        binding = parse_goal_binding(job["instruction"])
        target, reason = plan_link(
            job, goals.get(binding.goal_id or ""), milestones.get(binding.milestone_id or ""),
        )
        reasons[reason] += 1
        if target:
            plans.append({
                "job_id": job["job_id"], "tenant_id": job["tenant_id"], "project": job["project"],
                "goal_id": target[0], "milestone_id": target[1],
            })
    return plans, reasons


async def run(days: int, apply: bool) -> int:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        plans, reasons = await _plan_all(conn, days)
        print(f"mode={'APPLY' if apply else 'DRY-RUN'} days={days} candidates={sum(reasons.values())}")
        for reason, count in sorted(reasons.items()):
            print(f"  {reason}: {count}")
        print(f"linkable: {len(plans)}")
        for plan in plans[:20]:
            print(f"  {plan['job_id']} -> goal={plan['goal_id']} milestone={plan['milestone_id']}")
        if len(plans) > 20:
            print(f"  ... (+{len(plans) - 20})")
        if not apply:
            print("dry-run: 아무것도 쓰지 않았다. 반영하려면 --apply")
            return 0
        written = 0
        async with conn.transaction():
            for plan in plans:
                result = await conn.execute(
                    APPLY_SQL, plan["job_id"], plan["goal_id"], plan["milestone_id"],
                    plan["tenant_id"], plan["project"],
                )
                written += 1 if result == "UPDATE 1" else 0
        print(f"applied: {written}")
        return 0
    finally:
        await conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=14, help="조회 기간(일), 기본 14")
    parser.add_argument("--apply", action="store_true", help="실제로 UPDATE 한다 (기본은 dry-run)")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days 는 1 이상")
    return asyncio.run(run(args.days, args.apply))


if __name__ == "__main__":
    sys.exit(main())
