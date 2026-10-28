#!/usr/bin/env python3
"""비활성 목표에 남은 active 자동승인 grant 를 정지한다 (1회성 정합).

    python3 scripts/goal_grant_suspend_reconcile.py --dry-run          # 정지·복원 두 계획 표 (읽기 전용)
    python3 scripts/goal_grant_suspend_reconcile.py --dry-run --json   # 기계용
    python3 scripts/goal_grant_suspend_reconcile.py --apply            # 정지만. 대표님 승인 후에만
    python3 scripts/goal_grant_suspend_reconcile.py --apply-restore    # 복원만. 대표님 승인 후에만

PRD goal-first-triage-execution-prd §3-2 AS-3. 대상은 목표 status 가 active 가 아니거나
일시정지(paused_at) 중인데 goal_auto_approval_grants.status='active' 인 행이다.
정지 규칙의 SQL 은 app/services/goal_grant_suspension.py 의 SUSPEND_SQL **하나**를 그대로 쓴다
(R-RELEASE: 두 벌 금지). 서버 경로(PUT /goals, pause, triage apply, 자동 차단)와 같은 문장이다.

--apply 는 목표마다 한 문장씩, 전체를 한 트랜잭션으로 실행한다. 하나라도 실패하면 전부 되돌린다.
--apply 는 정지만 한다. 복원은 평소 목표가 active 로 돌아올 때 서버 경로(sync_goal_grants)가 한다.

복원 계획·--apply-restore: 서버 경로는 목표 status/paused_at 을 바꾸는 트랜잭션 안에서만 복원을 부른다.
이 코드가 들어오기 전(또는 이관 마이그레이션으로) 이미 active 로 돌아온 목표는 이후 상태 전이가 없어
복원이 한 번도 불리지 않고, "active 목표 + 복원 가능한 paused grant" 가 영구히 남는다. 그 행을 찾아
보여주고(--dry-run), --apply-restore 가 목표마다 RESTORE_SQL 을 그대로 실행한다(전체 한 트랜잭션,
goals id 순 FOR UPDATE). 만료·배정 비활성·신규 정책 등 RESTORE_SQL 이 거르는 행은 표에 "복원 안 됨 사유"
로 보이고 실제로도 복원되지 않는다. --apply-restore 도 대표님 승인 후에만 실행한다.

접속: PGHOST 와 psql 이 있으면 psql 직접, 아니면 `docker exec aads-postgres psql`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PG_CONTAINER = os.getenv("AADS_PG_CONTAINER", "aads-postgres")
SOURCE = "goal_grant_suspend_reconcile"
RESTORE_SOURCE = "reconcile_script_restore"
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _load_rules():
    # app.services/__init__ 가 무거운 의존성을 끌어오므로 파일 경로로 직접 읽는다(표준 라이브러리만 쓰는 모듈).
    path = REPO / "app" / "services" / "goal_grant_suspension.py"
    spec = importlib.util.spec_from_file_location("goal_grant_suspension_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULES = _load_rules()

PLAN_SQL = """
BEGIN READ ONLY;
SELECT COALESCE(json_agg(row_to_json(t) ORDER BY t.project, t.title), '[]'::json) FROM (
  SELECT g.id::text AS goal_id, g.tenant_id::text AS tenant_id, left(g.title, 40) AS title,
         g.project, g.status AS goal_status, g.paused_at IS NOT NULL AS goal_paused,
         g.priority,
         count(*) AS grants,
         string_agg(gr.id::text, ',' ORDER BY gr.issued_at) AS grant_ids,
         string_agg(DISTINCT gr.max_risk_tier, ',') AS tiers,
         min(gr.expires_at)::date::text AS earliest_expiry,
         sum(gr.used_executions) AS used, sum(gr.max_executions) AS max_exec
    FROM goals g JOIN goal_auto_approval_grants gr
      ON gr.goal_id = g.id AND gr.tenant_id = g.tenant_id AND gr.status = 'active'
   WHERE g.status <> 'active' OR g.paused_at IS NOT NULL
   GROUP BY g.id, g.tenant_id, g.title, g.project, g.status, g.paused_at, g.priority
) t;
ROLLBACK;
"""


def _restore_plan_sql() -> str:
    """복원 후보 조회(읽기 전용). 대상 판정은 RULES 의 사유 상수를 그대로 쓴다.
    blockers 는 표시용이다 — 실제 복원 여부는 적용 시점의 RULES.RESTORE_SQL 이 정한다.
    """
    reasons = ", ".join(_literal(r) for r in RULES.RESTORABLE_REASONS)
    prefix = _literal(RULES.RESTORABLE_REASON_PREFIX)
    return """
BEGIN READ ONLY;
SELECT COALESCE(json_agg(row_to_json(t) ORDER BY t.project, t.title), '[]'::json) FROM (
  SELECT x.goal_id, x.tenant_id, x.title, x.project, x.priority,
         count(*) AS grants,
         string_agg(x.grant_id, ',' ORDER BY x.issued_at) AS grant_ids,
         string_agg(DISTINCT x.revoke_reason, ',') AS reasons,
         min(x.expires_at)::date::text AS earliest_expiry,
         count(*) FILTER (WHERE x.blockers = '') AS restorable,
         COALESCE(string_agg(DISTINCT NULLIF(x.blockers, ''), ','), '') AS blocked_by
    FROM (
      SELECT g.id::text AS goal_id, g.tenant_id::text AS tenant_id, left(g.title, 40) AS title,
             g.project, g.priority, gr.id::text AS grant_id, gr.issued_at, gr.expires_at,
             gr.revoke_reason,
             concat_ws('+',
               CASE WHEN gr.expires_at <= clock_timestamp() THEN 'expired' END,
               CASE WHEN NOT EXISTS (SELECT 1 FROM project_role_assignments a
                                      WHERE a.id = gr.assignment_id AND a.tenant_id = gr.tenant_id
                                        AND a.active) THEN 'assignment_inactive' END,
               CASE WHEN EXISTS (SELECT 1 FROM goal_approval_policy_versions cur
                                  WHERE cur.tenant_id = gr.tenant_id AND cur.id <> gr.policy_version
                                    AND cur.mode IN ('canary', 'enabled')
                                    AND (cur.project IS NULL OR cur.project = gr.project)
                                    AND GREATEST(cur.created_at, COALESCE(cur.effective_at, cur.created_at))
                                        > gr.issued_at) THEN 'newer_policy' END) AS blockers
        FROM goals g JOIN goal_auto_approval_grants gr
          ON gr.goal_id = g.id AND gr.tenant_id = g.tenant_id AND gr.status = 'paused'
       WHERE g.status = 'active' AND g.paused_at IS NULL
         AND (gr.revoke_reason IN (%s) OR starts_with(gr.revoke_reason, %s))
    ) x
   GROUP BY x.goal_id, x.tenant_id, x.title, x.project, x.priority
) t;
ROLLBACK;
""" % (reasons, prefix)


def run_psql(sql: str) -> list[str]:
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
    return (proc.stdout or "").splitlines()


def _fetch_json(sql: str) -> list[dict]:
    lines = [ln for ln in run_psql(sql) if ln.startswith("[")]
    if not lines:
        raise SystemExit("no plan output")
    return json.loads(lines[-1])


def fetch_plan() -> list[dict]:
    return _fetch_json(PLAN_SQL)


def fetch_restore_plan() -> list[dict]:
    return _fetch_json(_restore_plan_sql())


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render(sql: str, *args: str) -> str:
    """asyncpg 번호 인자($1…)를 psql 용 리터럴로 바꾼다. 인자는 호출부에서 검증한 값만 받는다."""
    return re.sub(r"\$(\d+)", lambda m: _literal(args[int(m.group(1)) - 1]), sql)


def _build_goal_tx(plan: list[dict], statement: str, *tail_args: str) -> str:
    """목표 행을 id 순으로 먼저 잠그고, 목표마다 statement 를 (goal_id, tenant_id, *tail_args) 로 실행한다."""
    for p in plan:
        if not (_UUID.match(p["goal_id"]) and _UUID.match(p["tenant_id"])):
            raise SystemExit("unexpected value in plan: %r" % (p,))
    ordered = sorted(plan, key=lambda p: p["goal_id"])
    parts = ["BEGIN;"]
    if ordered:
        parts.append(
            "SELECT count(*) FROM (SELECT id FROM goals WHERE id IN (%s) ORDER BY id FOR UPDATE) locked;"
            % ", ".join(_literal(p["goal_id"]) + "::uuid" for p in ordered))
    for p in ordered:
        parts.append(render(statement.strip().rstrip(";"), p["goal_id"], p["tenant_id"], *tail_args) + ";")
    parts.append("COMMIT;")
    return "\n".join(parts)


def build_apply_sql(plan: list[dict]) -> str:
    """plan 은 대상 목표의 후보일 뿐이다. 적용 트랜잭션 안에서 목표 행을 id 순으로 먼저 잠그고,
    SUSPEND_SQL 이 현재 status/paused_at 을 다시 읽어 지금도 비활성인 목표의 grant 만 정지한다.
    (plan 과 적용 사이에 재개된 목표는 건드리지 않는다. 잠금 순서 goals(id 순) → grants 는 서버 경로와 같다.)
    """
    return _build_goal_tx(plan, RULES.SUSPEND_SQL, RULES.SUSPEND_REASON, SOURCE)


def build_restore_sql(plan: list[dict]) -> str:
    """복원 문장은 RULES.RESTORE_SQL 그대로다. 현재 status/paused_at·만료·배정·정책은 SQL 이 다시 판정한다."""
    return _build_goal_tx(plan, RULES.RESTORE_SQL, RESTORE_SOURCE)


def print_table(plan: list[dict]) -> None:
    print("%-8s %-8s %-6s %-12s %-40s | %6s %-6s %9s %-10s | %s" % (
        "goal", "status", "prio", "project", "title", "grants", "tier", "used/max", "expires",
        "after"))
    for p in plan:
        status = p["goal_status"] + ("*" if p["goal_paused"] else "")
        print("%-8s %-8s %-6s %-12s %-40s | %6d %-6s %9s %-10s | %s -> %s (%s)" % (
            p["goal_id"][:8], status, (p["priority"] or "")[:6], p["project"][:12], p["title"],
            p["grants"], p["tiers"], "%s/%s" % (p["used"], p["max_exec"]), p["earliest_expiry"],
            "active", RULES.SUSPENDED_STATUS, RULES.SUSPEND_REASON))
    print()
    print("목표 %d개 · grant %d건이 active → %s(%s) 로 바뀐다. (* = 일시정지 중)" % (
        len(plan), sum(p["grants"] for p in plan), RULES.SUSPENDED_STATUS, RULES.SUSPEND_REASON))
    print("goals 는 건드리지 않는다. 복원은 목표가 active 로 돌아올 때 서버 경로가 한다.")


def print_restore_table(plan: list[dict]) -> None:
    print("%-8s %-6s %-12s %-40s | %6s %-26s %-10s | %9s | %s" % (
        "goal", "prio", "project", "title", "grants", "reason", "expires", "restorable",
        "복원 안 됨 사유"))
    for p in plan:
        print("%-8s %-6s %-12s %-40s | %6d %-26s %-10s | %9d | %s" % (
            p["goal_id"][:8], (p["priority"] or "")[:6], p["project"][:12], p["title"],
            p["grants"], p["reasons"], p["earliest_expiry"], p["restorable"],
            p["blocked_by"] or "-"))
        print("         grant: %s" % p["grant_ids"])
    print()
    print("목표 %d개 · 복원 가능 grant %d건 / 후보 %d건 (paused → active). --apply-restore 로만 적용된다." % (
        len(plan), sum(p["restorable"] for p in plan), sum(p["grants"] for p in plan)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="읽기 전용. 목표별 전후 표")
    mode.add_argument("--apply", action="store_true", help="실제 정지(복원은 하지 않는다). 대표님 승인 후에만")
    mode.add_argument("--apply-restore", action="store_true", help="실제 복원(정지는 하지 않는다). 대표님 승인 후에만")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.apply_restore:
        restore_plan = fetch_restore_plan()
        if not restore_plan:
            print("복원 후보 없음")
            return 0
        changed = [ln for ln in run_psql(build_restore_sql(restore_plan)) if _UUID.match(ln.strip())]
        print("복원 %d건 (후보 목표 %d개)" % (len(changed), len(restore_plan)))
        remaining = [p for p in fetch_restore_plan() if p["restorable"] > 0]
        print("남은 복원 가능 목표: %d" % len(remaining))
        return 0 if not remaining else 1

    plan = fetch_plan()
    if args.dry_run:
        restore_plan = fetch_restore_plan()
        if args.json:
            print(json.dumps({"targets": len(plan), "grants": sum(p["grants"] for p in plan),
                              "plan": plan,
                              "restore_targets": len(restore_plan),
                              "restore_grants": sum(p["restorable"] for p in restore_plan),
                              "restore_plan": restore_plan}, ensure_ascii=False, indent=2))
        else:
            print_table(plan)
            print()
            print("== 복원 계획 (active 목표 + 복원 가능 사유의 paused grant) ==")
            print_restore_table(restore_plan)
        return 0

    if not plan:
        print("정지할 grant 없음")
        return 0
    changed = [ln for ln in run_psql(build_apply_sql(plan)) if _UUID.match(ln.strip())]
    print("정지 %d건 (목표 %d개)" % (len(changed), len(plan)))
    remaining = fetch_plan()
    print("남은 비활성 목표의 active grant 목표: %d" % len(remaining))
    return 0 if not remaining else 1


if __name__ == "__main__":
    sys.exit(main())
