#!/usr/bin/env python3
"""FOOD HR M1 — 기존 러너 작업 두 건의 목표 연결 소급 (기본은 dry-run, 운영 실행은 별도 승인).

대상은 아래 TARGETS 두 건뿐이다. 임의 job 을 받는 인자는 없다.
  - 지시서에 GOAL_ID/MILESTONE_ID 가 있는데 DB goal_id/milestone_id 가 NULL 이었던 작업
  - AADS 호스팅 러너가 FOOD 목표를 명시한 경우 (goal_link_policy 가 허용)

검증(하나라도 어긋나면 그 작업은 건너뛰고 사유를 출력한다)
  - job 존재, project=AADS, status=done, commit_hash == 지정 SHA
  - 지시서 `GOAL_ID:`/`MILESTONE_ID:` 줄이 지정 값과 같다
  - goal·milestone·job 이 같은 tenant, goal.project 가 정책상 허용(FOOD), milestone 이 그 goal 소속
  - goal 상태 draft/active/blocked (blocked 이면 milestone 이 열려 있어야 함)

쓰는 것 (apply 시, 한 트랜잭션)
  - pipeline_jobs.goal_id / milestone_id  (goal_id IS NULL 인 행만. updated_at 은 건드리지 않는다)
  - goal_task_links 한 행: status='pending', link_state='active', bind_source='explicit_directive',
    bound_by='backfill:food-hr-m1:v2:job-created' 또는 ':link-only'. 완료/배포 증거(release_*)는 쓰지 않는다 — done 은 배포완료가 아니다.
    완료 승격은 release evidence 재조정이 인증된 배포 증거로만 한다.
쓰지 않는 것: goals, milestones(dispatch_blocked_at·예산·상태 포함), 배포 관련 컬럼, HANDOVER.
  전후 goals/milestones 행 전체를 비교해 달라졌으면 예외로 롤백한다.

재실행 안전: v2 생성분의 같은 활성 연결만 noop. 나머지 기존 링크(되돌린 detached 포함)는 보존하고 skip한다.
 v1 표지는 덮어쓰기 이력이 가능하므로 revert 소유권으로 인정하지 않는다.
롤백: `--revert [--apply]` — 이 스크립트가 만든 링크(bound_by 표지)만 detached 로 돌리고
      job-created 표지가 있는 경우만 pipeline_jobs 연결을 NULL 로 되돌린다.
      link-only는 원래 작업 연결을 보존한다. 기존 링크는 수정하지 않는다. 행을 삭제하지 않는다.

사용 (운영 컨테이너 안):
    python3 scripts/backfill_food_hr_m1_links.py            # dry-run, 전후 SELECT 만
    python3 scripts/backfill_food_hr_m1_links.py --apply    # 승인 후
    python3 scripts/backfill_food_hr_m1_links.py --revert   # 되돌림 dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.goal_binding import (
    BIND_SOURCE_EXPLICIT_DIRECTIVE,
    parse_goal_binding,
)
from app.services.goal_link_policy import is_goal_project_allowed

GOAL_ID = "f00d0916-0916-4a01-9a01-000000000001"
MILESTONE_ID = "f00d0916-0916-4a01-9a01-000000000011"
JOB_PROJECT = "AADS"
BOUND_BY = "backfill:food-hr-m1:v2:job-created"
LINK_ONLY_BOUND_BY = "backfill:food-hr-m1:v2:link-only"
TARGETS: dict[str, str] = {
    "runner-09c0fef5": "1ac76d9faeb2cd6869a2cae979b8d9f04295c0ba",
    "runner-e7791459": "6cfd72fe9f2ea4507c57accb4448065af2f5d1df",
}
OPEN_MILESTONE_STATUSES = ("pending", "in_progress", "blocked")
LINKABLE_GOAL_STATUSES = ("draft", "active", "blocked")
GUARDED_KEYS = ("status", "dispatch_blocked_at", "updated_at")

JOB_SQL = """
SELECT job_id, tenant_id::text AS tenant_id, project, status, phase, commit_hash,
       goal_id::text AS goal_id, milestone_id::text AS milestone_id, instruction,
       deployed_at
  FROM pipeline_jobs WHERE job_id = ANY($1::text[]) ORDER BY job_id
"""
GOAL_SQL = "SELECT id::text AS goal_id, tenant_id::text AS tenant_id, project, status FROM goals WHERE id = $1::uuid"
MILESTONE_SQL = """
SELECT id::text AS milestone_id, goal_id::text AS goal_id, tenant_id::text AS tenant_id, project, status
  FROM milestones WHERE id = $1::uuid
"""
LINKS_SQL = """
SELECT goal_task_links.*, id::text AS id, tenant_id::text AS tenant_id,
       goal_id::text AS goal_id, milestone_id::text AS milestone_id
  FROM goal_task_links
 WHERE task_type = 'pipeline_job' AND task_id = ANY($1::text[]) ORDER BY task_id, created_at FOR UPDATE
"""
GUARD_SQL = """
SELECT (SELECT to_jsonb(g) FROM goals g WHERE g.id = $1::uuid)::text AS goal,
       (SELECT to_jsonb(m) FROM milestones m WHERE m.id = $2::uuid)::text AS milestone
"""
LINK_COLUMNS_SQL = """
SELECT column_name FROM information_schema.columns
 WHERE table_schema = current_schema() AND table_name = 'goal_task_links'
"""
LINK_UPSERT_SQL = """
INSERT INTO goal_task_links
  (id, tenant_id, goal_id, milestone_id, task_type, task_id, status, bind_source, bound_by,
   link_state, last_job_status, updated_at)
VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid, 'pipeline_job', $4, 'pending', $5, $6,
        'active', 'pending', NOW())
ON CONFLICT (goal_id, task_type, task_id) WHERE goal_id IS NOT NULL
DO NOTHING
"""
JOB_UPDATE_SQL = """
UPDATE pipeline_jobs SET goal_id = $2::uuid, milestone_id = $3::uuid
 WHERE job_id = $1 AND goal_id IS NULL AND milestone_id IS NULL AND tenant_id = $4::uuid AND project = $5
"""
JOB_LOCK_SQL = "SELECT job_id FROM pipeline_jobs WHERE job_id = ANY($1::text[]) FOR UPDATE"
LINK_DETACH_SQL = """
UPDATE goal_task_links SET link_state = 'detached', detach_reason = 'backfill_revert', updated_at = NOW()
 WHERE task_type = 'pipeline_job' AND task_id = $1 AND goal_id = $2::uuid
   AND bound_by = $3 AND release_deploy_run_id IS NULL
   AND tenant_id = $4::uuid AND milestone_id = $5::uuid
   AND link_state = 'active' AND bind_source = 'explicit_directive'
"""
JOB_REVERT_SQL = """
UPDATE pipeline_jobs SET goal_id = NULL, milestone_id = NULL
 WHERE job_id = $1 AND goal_id = $2::uuid AND milestone_id = $3::uuid
   AND tenant_id = $4::uuid AND project = $5
"""


def plan_target(
    job_id: str,
    job: Mapping[str, Any] | None,
    goal: Mapping[str, Any] | None,
    milestone: Mapping[str, Any] | None,
    links: list[Mapping[str, Any]],
) -> tuple[str, str]:
    """(action, reason). action: link | noop | skip. 순수 함수 — DB 접근 없음."""
    if not job:
        return "skip", "job_not_found"
    if job["project"] != JOB_PROJECT:
        return "skip", f"job_project={job['project']}"
    if job["status"] != "done":
        return "skip", f"job_status={job['status']}"
    if (job.get("commit_hash") or "") != TARGETS[job_id]:
        return "skip", "commit_mismatch"
    binding = parse_goal_binding(job.get("instruction"))
    if binding.goal_id != GOAL_ID or binding.milestone_id != MILESTONE_ID:
        return "skip", "directive_mismatch"
    if not goal or goal["goal_id"] != GOAL_ID:
        return "skip", "goal_not_found"
    if goal["tenant_id"] != job["tenant_id"]:
        return "skip", "cross_tenant_goal"
    if not is_goal_project_allowed(job["project"], goal["project"]):
        return "skip", f"cross_project goal={goal['project']}"
    if goal["status"] not in LINKABLE_GOAL_STATUSES:
        return "skip", f"goal_status={goal['status']}"
    if (
        not milestone
        or milestone["milestone_id"] != MILESTONE_ID
        or milestone["goal_id"] != GOAL_ID
        or milestone["tenant_id"] != job["tenant_id"]
        or milestone["project"] != goal["project"]
    ):
        return "skip", "milestone_mismatch"
    if goal["status"] == "blocked" and milestone["status"] not in OPEN_MILESTONE_STATUSES:
        return "skip", "blocked_milestone_closed"
    if job["goal_id"] and (job["goal_id"], job["milestone_id"]) != (GOAL_ID, MILESTONE_ID):
        return "skip", "conflict_job_bound_elsewhere"
    if not job["goal_id"] and job["milestone_id"]:
        return "skip", "conflict_job_bound_elsewhere"
    for link in links:
        if link.get("tenant_id", job["tenant_id"]) != job["tenant_id"]:
            return "skip", "cross_tenant_link"
        if link["goal_id"] != GOAL_ID and (link.get("link_state") or "active") == "active":
            return "skip", "conflict_link_bound_elsewhere"
        if (link["goal_id"] == GOAL_ID and link.get("release_deploy_run_id")
                and (job["goal_id"], job["milestone_id"], link["milestone_id"]) != (GOAL_ID, MILESTONE_ID, MILESTONE_ID)):
            return "skip", "conflict_release_evidence"
    if links:
        # v1 overwrote pre-existing rows: its markers cannot prove ownership.
        if (len(links) == 1 and owned_link(links[0], job) and links[0].get("link_state") == "active"
                and (job["goal_id"], job["milestone_id"]) == (GOAL_ID, MILESTONE_ID)):
            return "noop", "already_linked"
        return "skip", "existing_link_preserved"
    return "link", "linkable"


def owned_link(link: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    """Only insert-only v2 provenance authorizes revert; v1 markers are ambiguous."""
    return (
        link.get("bound_by") in (BOUND_BY, LINK_ONLY_BOUND_BY)
        and link.get("tenant_id") == job["tenant_id"]
        and link.get("goal_id") == GOAL_ID
        and link.get("milestone_id") == MILESTONE_ID
        and link.get("bind_source") == BIND_SOURCE_EXPLICIT_DIRECTIVE
        and not link.get("release_deploy_run_id")
    )


async def _snapshot(conn: Any, ids: list[str]) -> dict[str, Any]:
    jobs = [dict(r) for r in await conn.fetch(JOB_SQL, ids)]
    links = [dict(r) for r in await conn.fetch(LINKS_SQL, ids)]
    guard = await conn.fetchrow(GUARD_SQL, GOAL_ID, MILESTONE_ID)
    goal_row = json.loads(guard["goal"]) if guard and guard["goal"] else None
    ms_row = json.loads(guard["milestone"]) if guard and guard["milestone"] else None
    return {"jobs": jobs, "links": links, "goal_row": goal_row, "milestone_row": ms_row}


def _print_snapshot(label: str, snap: Mapping[str, Any]) -> None:
    print(f"--- {label} ---")
    for job in snap["jobs"]:
        print(
            f"  job {job['job_id']} status={job['status']} commit={job['commit_hash']} "
            f"goal_id={job['goal_id']} milestone_id={job['milestone_id']} deployed_at={job['deployed_at']}"
        )
    print(f"  goal_task_links rows={len(snap['links'])}")
    for link in snap["links"]:
        print(
            f"    {link['task_id']} goal={link['goal_id']} ms={link['milestone_id']} status={link['status']} "
            f"state={link['link_state']} src={link['bind_source']} by={link['bound_by']} "
            f"release_run={link['release_deploy_run_id']}"
        )
    for name, row in (("goal", snap["goal_row"]), ("milestone", snap["milestone_row"])):
        if row is None:
            print(f"  {name}: NOT FOUND")
            continue
        print(f"  {name}: " + " ".join(f"{k}={row.get(k)}" for k in GUARDED_KEYS if k in row))


async def run(apply: bool, revert: bool) -> int:
    import asyncpg

    ids = sorted(TARGETS)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        columns = {r["column_name"] for r in await conn.fetch(LINK_COLUMNS_SQL)}
        needed = {"bind_source", "bound_by", "link_state", "detach_reason", "last_job_status", "release_deploy_run_id"}
        if not needed.issubset(columns):
            print(f"abort: goal_task_links 컬럼 부족 {sorted(needed - columns)}")
            return 2
        mode = ("REVERT-" if revert else "") + ("APPLY" if apply else "DRY-RUN")
        print(f"mode={mode} goal={GOAL_ID} milestone={MILESTONE_ID}")
        async with conn.transaction():
            await conn.fetch(JOB_LOCK_SQL, ids)
            before = await _snapshot(conn, ids)
            _print_snapshot("before", before)
            jobs = {j["job_id"]: j for j in before["jobs"]}
            goal = await conn.fetchrow(GOAL_SQL, GOAL_ID)
            milestone = await conn.fetchrow(MILESTONE_SQL, MILESTONE_ID)
            written = 0
            applied = set()
            for job_id in ids:
                links = [lk for lk in before["links"] if lk["task_id"] == job_id]
                action, reason = plan_target(
                    job_id, jobs.get(job_id), dict(goal) if goal else None,
                    dict(milestone) if milestone else None, links,
                )
                if revert:
                    mine = [lk for lk in links if jobs.get(job_id)
                            and owned_link(lk, jobs[job_id]) and lk.get("link_state") == "active"]
                    print(f"plan {job_id}: revert links={len(mine)} (plan_check={action}/{reason})")
                    if apply and mine and action != "skip":
                        owner = mine[0]["bound_by"]
                        detached = await conn.execute(LINK_DETACH_SQL, job_id, GOAL_ID, owner, jobs[job_id]["tenant_id"], MILESTONE_ID)
                        if detached != "UPDATE 1":
                            raise RuntimeError(f"link detach failed: {job_id}: {detached}")
                        if owner == BOUND_BY:
                            reverted = await conn.execute(JOB_REVERT_SQL, job_id, GOAL_ID, MILESTONE_ID, jobs[job_id]["tenant_id"], JOB_PROJECT)
                            if reverted != "UPDATE 1":
                                raise RuntimeError(f"job revert failed: {job_id}: {reverted}")
                        written += 1
                    continue
                print(f"plan {job_id}: {action} ({reason})")
                if apply and action == "link":
                    job = jobs[job_id]
                    updated = await conn.execute(
                        JOB_UPDATE_SQL, job_id, GOAL_ID, MILESTONE_ID, job["tenant_id"], job["project"],
                    )
                    if updated not in ("UPDATE 1",) and not job["goal_id"]:
                        raise RuntimeError(f"pipeline_jobs update failed for {job_id}: {updated}")
                    owner = BOUND_BY if updated == "UPDATE 1" else LINK_ONLY_BOUND_BY
                    result = await conn.execute(
                        LINK_UPSERT_SQL, job["tenant_id"], GOAL_ID, MILESTONE_ID, job_id,
                        BIND_SOURCE_EXPLICIT_DIRECTIVE, owner,
                    )
                    if result != "INSERT 0 1":
                        raise RuntimeError(f"link upsert failed: {job_id}: {result}")
                    applied.add(job_id)
                    written += 1
            after = await _snapshot(conn, ids)
            _print_snapshot("after (트랜잭션 안)", after)
            for job_id in applied:
                job = next(j for j in after["jobs"] if j["job_id"] == job_id)
                matching = [lk for lk in after["links"] if lk["task_id"] == job_id and lk["goal_id"] == GOAL_ID]
                if ((job["goal_id"], job["milestone_id"]) != (GOAL_ID, MILESTONE_ID)
                    or len(matching) != 1
                    or any(lk.get("tenant_id") != job["tenant_id"] or lk["milestone_id"] != MILESTONE_ID
                           or lk["status"] != "pending" or lk.get("last_job_status") != "pending"
                           or lk["link_state"] != "active" or lk["bound_by"] not in (BOUND_BY, LINK_ONLY_BOUND_BY)
                           or lk["bind_source"] != BIND_SOURCE_EXPLICIT_DIRECTIVE
                           or lk.get("release_deploy_run_id") for lk in matching)):
                    raise RuntimeError(f"final binding mismatch: {job_id}")
            if after["goal_row"] != before["goal_row"] or after["milestone_row"] != before["milestone_row"]:
                raise RuntimeError("goals/milestones 행이 달라졌다 — 롤백")
            original_links = {lk["id"]: lk for lk in before["links"] if "id" in lk}
            for lk in after["links"]:
                original = original_links.get(lk.get("id"))
                if original and not (revert and jobs.get(lk["task_id"])
                                     and owned_link(original, jobs[lk["task_id"]])) and lk != original:
                    raise RuntimeError("existing link changed — rollback")
            if not apply:
                print("dry-run: 아무것도 쓰지 않았다 (트랜잭션 롤백). 반영하려면 --apply")
                raise _DryRunRollback
            print(f"applied: {written}")
    except _DryRunRollback:
        return 0
    finally:
        await conn.close()
    return 0


class _DryRunRollback(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제로 쓴다 (기본은 dry-run)")
    parser.add_argument("--revert", action="store_true", help="이 스크립트가 만든 연결만 되돌린다")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.apply, args.revert))


if __name__ == "__main__":
    sys.exit(main())
