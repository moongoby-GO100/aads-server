#!/usr/bin/env python3
"""approval_policy → goal_auto_approval_grants 이관 사전 점검 (읽기 전용).

    python3 scripts/goal_grant_migration_dry_run.py          # 표
    python3 scripts/goal_grant_migration_dry_run.py --json   # 기계용

마이그레이션(migrations/20261015_goal_auto_approval_grants_from_policy.sql)의
`@@PLAN_BEGIN`~`@@PLAN_END` SELECT 를 **그대로** 읽어 `BEGIN READ ONLY` 안에서
돌린다. 계획 규칙을 이 파일에 다시 쓰지 않는다(두 벌이면 한쪽이 낡는다).

PRD goal-priority-dormancy-autoapproval-display-prd §8 — "이관 스크립트 dry-run
결과(목표별 전후 비교) 첨부 후 적용".

접속: PGHOST 와 psql 이 있으면 psql 직접, 아니면 `docker exec aads-postgres psql`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "migrations" / "20261015_goal_auto_approval_grants_from_policy.sql"
PG_CONTAINER = os.getenv("AADS_PG_CONTAINER", "aads-postgres")


def plan_select(text: str) -> str:
    """마이그레이션에서 계획 SELECT 를 꺼내 함수 인자를 NULL 로 바꾼다."""
    m = re.search(r"-- @@PLAN_BEGIN\n(.*?)-- @@PLAN_END", text, re.S)
    if not m:
        raise SystemExit("plan markers not found in %s" % MIGRATION)
    return m.group(1).replace("p_goal_id", "NULL::uuid")


def build_sql(plan: str) -> str:
    return f"""
BEGIN READ ONLY;
WITH plan AS ({plan}),
existing AS (
    SELECT goal_id, count(*) AS n
      FROM goal_auto_approval_grants
     WHERE 'live_trading_gate' = ANY(actions)
       AND revoke_reason IS DISTINCT FROM 'migration_rollback'
     GROUP BY goal_id)
SELECT json_build_object(
  'plan', COALESCE((SELECT json_agg(json_build_object(
      'goal_id', p.goal_id, 'title', left(g.title, 40), 'project', p.project,
      'goal_status', p.goal_status, 'goal_paused', p.goal_paused, 'priority', g.priority,
      'before_high', p.auto_high, 'before_critical', p.auto_critical,
      'before_used', p.legacy_used, 'before_max', p.max_executions,
      'before_set_at', p.legacy_policy->>'set_at',
      'after_status', p.plan_status, 'after_reason', p.plan_reason,
      'after_tier', p.max_risk_tier, 'after_used', p.used_executions,
      'after_max', p.max_executions, 'assignment_source', p.assignment_source,
      'existing_grants', COALESCE(e.n, 0))
      ORDER BY p.plan_status, p.project, g.title)
    FROM plan p JOIN goals g ON g.id = p.goal_id LEFT JOIN existing e ON e.goal_id = p.goal_id), '[]'::json),
  'skipped_no_owner', COALESCE((SELECT json_agg(json_build_object('goal_id', g.id, 'title', left(g.title, 40), 'status', g.status))
    FROM goals g
   WHERE g.status IN ('draft','active','blocked')
     AND COALESCE(g.approval_policy, '{{}}'::jsonb) ?| ARRAY['auto_approve_high','auto_approve_critical']
     AND g.owner_session_id IS NULL), '[]'::json),
  'excluded_closed', (SELECT count(*) FROM goals g
   WHERE g.status NOT IN ('draft','active','blocked')
     AND COALESCE(g.approval_policy, '{{}}'::jsonb) ?| ARRAY['auto_approve_high','auto_approve_critical']),
  'role_key_only', (SELECT count(*) FROM goals g
   WHERE g.status IN ('draft','active','blocked')
     AND COALESCE(g.approval_policy, '{{}}'::jsonb) <> '{{}}'::jsonb
     AND NOT COALESCE(g.approval_policy, '{{}}'::jsonb) ?| ARRAY['auto_approve_high','auto_approve_critical']),
  'grants_total', (SELECT count(*) FROM goal_auto_approval_grants)
);
ROLLBACK;
"""


def run_psql(sql: str) -> str:
    env = dict(os.environ)
    if env.get("PGHOST") and shutil.which("psql"):
        argv = ["psql", "-h", env["PGHOST"], "-p", env.get("PGPORT", "5432"),
                "-U", env.get("PGUSER", "aads"), "-d", env.get("PGDATABASE", "aads")]
    else:
        argv = ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", "aads", "-d", "aads"]
    proc = subprocess.run(
        argv + ["-At", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, text=True, capture_output=True, timeout=90, env=env,
    )
    if proc.returncode != 0:
        raise SystemExit("psql failed: " + (proc.stderr or "")[:400])
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("{")]
    if not lines:
        raise SystemExit("no plan output")
    return lines[-1]


def summarize(data: dict) -> dict:
    plan = data["plan"]
    by = lambda key, val: [p for p in plan if p[key] == val]  # noqa: E731
    paused = by("after_status", "paused")
    active = by("after_status", "active")
    return {
        "policy_goals": len(plan),
        "grants_to_create": sum(1 for p in plan if not p["existing_grants"]),
        "active": len(active),
        "active_exhausted": sum(1 for p in active if p["after_used"] >= p["after_max"]),
        "paused": len(paused),
        "paused_blocked": sum(1 for p in paused if p["goal_status"] == "blocked"),
        "paused_draft": sum(1 for p in paused if p["goal_status"] == "draft"),
        "paused_goal_paused": sum(1 for p in paused if p["goal_status"] == "active"),
        "paused_was_effective": sum(
            1 for p in paused
            if (p["before_high"] or p["before_critical"]) and p["before_used"] < p["before_max"]
        ),
        "revoked_disabled": len(by("after_status", "revoked")),
        "bridge_assignments": sorted({p["project"] for p in plan if p["assignment_source"] == "bridge_assignment"}),
        "skipped_no_owner": len(data["skipped_no_owner"]),
        "excluded_closed": data["excluded_closed"],
        "role_key_only_not_migrated": data["role_key_only"],
        "grants_total_now": data["grants_total"],
    }


def _flags(p: dict) -> str:
    return "전체" if p["before_critical"] else ("고위험" if p["before_high"] else "해제")


def print_table(data: dict) -> None:
    print("%-8s %-7s %-6s %-12s %-40s | %-6s %9s | %-8s %-4s %9s %-18s %s" % (
        "goal", "status", "prio", "project", "title", "before", "used/max",
        "after", "tier", "used/max", "assignment", "reason"))
    for p in data["plan"]:
        print("%-8s %-7s %-6s %-12s %-40s | %-6s %9s | %-8s %-4s %9s %-18s %s" % (
            p["goal_id"][:8], p["goal_status"] + ("*" if p["goal_paused"] else ""),
            (p["priority"] or "")[:6], p["project"][:12], p["title"],
            _flags(p), "%d/%d" % (p["before_used"], p["before_max"]),
            p["after_status"], p["after_tier"], "%d/%d" % (p["after_used"], p["after_max"]),
            p["assignment_source"], p["after_reason"] or ""))
    for s in data["skipped_no_owner"]:
        print("SKIP %s %s (%s) — 주도 세션 없음, approval_policy 호환 경로에 남음" % (
            s["goal_id"][:8], s["title"], s["status"]))
    print()
    print("expires_at: 적용 시각 + 30일 (AA-4). approval_policy 원본은 변경하지 않는다.")
    print(json.dumps(summarize(data), ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = json.loads(run_psql(build_sql(plan_select(MIGRATION.read_text(encoding="utf-8")))))
    if args.json:
        print(json.dumps({"summary": summarize(data), **data}, ensure_ascii=False, indent=2))
    else:
        print_table(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
