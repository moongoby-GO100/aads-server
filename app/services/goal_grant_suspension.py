"""목표가 active 가 아니면 그 목표의 자동승인 grant 를 정지하고, 되돌아오면 복원한다.

정본: docs/prd/20261022_AADS_목표_첫정리_실행_PRD.md (document_key=goal-first-triage-execution-prd
§3-2 AS-1·2·4). 상위: goal-priority-dormancy-autoapproval-display-prd §5 AA-2·AA-3.

게이트(live_trading_guard)는 비활성 목표의 grant 를 이미 무시한다. 그런데 행은 `active` 로 남아
화면·집계가 "유효" 로 셌다. 여기서는 행의 status 를 `paused` 로 돌려 그 불일치를 없앤다.

- 정지 값은 `paused` 다. `suspended` 는 CHECK(goal_auto_approval_grants_status_check)에 없다.
  20261015 이관 마이그레이션이 같은 목적으로 넣은 값이고, 이관이 만든 paused 행도 같은 사유를 쓴다.
- 정지 사유(revoke_reason)는 `goal_not_active`. 이관이 만든 `goal_not_active:<status>`·`goal_paused`
  도 같은 뜻이라 복원 대상으로 본다. 사람이 회수한 행(status=revoked)과 stale·superseded·expired 는
  건드리지 않는다 — 복원은 status=paused 이고 사유가 위 셋일 때만, 만료 전일 때만 한다.
- **정지·복원 모두 SQL 이 직접 goals 행을 잠그고(FOR UPDATE) 현재 status/paused_at 을 다시 읽는다.**
  호출자가 먼저 읽어 온 값(정합 스크립트의 plan 등)은 신뢰하지 않는다 — plan 과 적용 사이에 목표가
  재개·변경돼도 active 목표의 grant 를 건드리지 않는다. 잠금 순서는 항상 goals → grants 다.
- 복원은 사람의 회수(revoked)뿐 아니라 정책 교체(stale_policy)·배정 종료(stale_assignment)·만료도
  되살리지 않는다. 정지 중에는 trigger/롤아웃의 stale 처리(status='active' 만 대상)를 못 받으므로
  RESTORE_SQL 이 같은 조건을 다시 본다.
- 정지·복원 이벤트는 goal_approval_decision_logs 에 남긴다. goal_auto_approval_usage_events 는
  use_id(실행 건) 가 NOT NULL 이고 goal_policy_decisions 는 서명 필수라 "목표 상태 때문에 grant 가
  멈췄다" 는 사실을 거기에 넣을 수 없다.

규칙의 SQL 은 이 파일에만 있다(R-RELEASE: 두 벌 금지). scripts/goal_grant_suspend_reconcile.py 도
아래 상수를 그대로 쓴다. 상수는 asyncpg 번호 인자($1…)이고 스크립트가 psql 용 리터럴로 바꾼다.
이 모듈은 표준 라이브러리만 가져온다(스크립트가 import 한다).
"""
from __future__ import annotations

from typing import Any

SUSPENDED_STATUS = "paused"
SUSPEND_REASON = "goal_not_active"
RESTORABLE_REASONS = (SUSPEND_REASON, "goal_paused")
RESTORABLE_REASON_PREFIX = SUSPEND_REASON + ":"

# $1 goal_id · $2 tenant_id · $3 사유 · $4 호출 경로. 목표가 **지금** 비활성이거나 일시정지일 때만 움직인다.
SUSPEND_SQL = """
WITH blocking AS (
    SELECT g.id, g.tenant_id,
           CASE WHEN g.status = 'active' THEN 'paused' ELSE g.status END AS goal_status
      FROM goals g
     WHERE g.id = $1::uuid AND g.tenant_id = $2::uuid
       AND (g.status <> 'active' OR g.paused_at IS NOT NULL)
       FOR UPDATE
), changed AS (
    UPDATE goal_auto_approval_grants gr
       SET status = 'paused', revoked_at = clock_timestamp(), revoke_reason = $3
      FROM blocking b
     WHERE gr.goal_id = b.id AND gr.tenant_id = b.tenant_id AND gr.status = 'active'
    RETURNING gr.id, gr.tenant_id, gr.goal_id, gr.policy_version, gr.grant_version, b.goal_status
), logged AS (
    INSERT INTO goal_approval_decision_logs
        (tenant_id, decision, policy_version, matched_grant_id, reason_codes, input_context, simulated)
    SELECT c.tenant_id, 'DENY', c.policy_version, c.id,
           ARRAY['grant_suspended', $3]::text[],
           jsonb_build_object('event', 'grant_suspended', 'goal_id', c.goal_id::text,
                              'goal_status', c.goal_status, 'source', $4::text,
                              'grant_version', c.grant_version),
           FALSE
      FROM changed c
    RETURNING matched_grant_id
)
SELECT id::text FROM changed
"""

# $1 goal_id · $2 tenant_id · $3 호출 경로. 목표가 지금 active 이고 일시정지가 아닐 때만 움직인다.
# paused + 정지 사유가 목표 비활성인 행만 되살린다. revoked(사람 회수)·stale·expired·superseded 는 대상 밖이다.
# 정지 동안 놓친 stale 처리(배정 종료·정책 교체)는 여기서 다시 판정해 되살리지 않는다.
RESTORE_SQL = """
WITH live AS (
    SELECT g.id, g.tenant_id
      FROM goals g
     WHERE g.id = $1::uuid AND g.tenant_id = $2::uuid
       AND g.status = 'active' AND g.paused_at IS NULL
       FOR UPDATE
), changed AS (
    UPDATE goal_auto_approval_grants gr
       SET status = 'active', revoked_at = NULL, revoke_reason = NULL
      FROM live l
     WHERE gr.goal_id = l.id AND gr.tenant_id = l.tenant_id AND gr.status = 'paused'
       AND (gr.revoke_reason IN ('goal_not_active', 'goal_paused')
            OR gr.revoke_reason LIKE 'goal\\_not\\_active:%')
       AND gr.expires_at > clock_timestamp()
       AND EXISTS (SELECT 1 FROM project_role_assignments a
                    WHERE a.id = gr.assignment_id AND a.tenant_id = gr.tenant_id AND a.active)
       AND NOT EXISTS (SELECT 1 FROM goal_approval_policy_versions cur
                        WHERE cur.tenant_id = gr.tenant_id AND cur.id <> gr.policy_version
                          AND cur.mode IN ('canary', 'enabled')
                          AND (cur.project IS NULL OR cur.project = gr.project)
                          AND GREATEST(cur.created_at, COALESCE(cur.effective_at, cur.created_at)) > gr.issued_at)
    RETURNING gr.id, gr.tenant_id, gr.goal_id, gr.policy_version, gr.grant_version
), logged AS (
    INSERT INTO goal_approval_decision_logs
        (tenant_id, decision, policy_version, matched_grant_id, reason_codes, input_context, simulated)
    SELECT c.tenant_id, 'NOTIFY', c.policy_version, c.id,
           ARRAY['grant_restored', 'goal_active']::text[],
           jsonb_build_object('event', 'grant_restored', 'goal_id', c.goal_id::text,
                              'source', $3::text, 'grant_version', c.grant_version),
           FALSE
      FROM changed c
    RETURNING matched_grant_id
)
SELECT id::text FROM changed
"""


def goal_blocks_grants(status: Any, paused_at: Any) -> bool:
    """grant 가 멈춰 있어야 하는 목표인가 — active 가 아니거나 일시정지 중."""
    return str(status or "").strip().lower() != "active" or paused_at is not None


async def sync_goal_grants(conn: Any, goal_id: Any, *, source: str) -> dict[str, Any]:
    """목표의 **현재** 상태에 맞춰 grant 를 정지하거나 복원한다. 호출 순서에 의존하지 않는다.

    목표 status·paused_at 을 바꾼 UPDATE 와 **같은 트랜잭션**에서 부른다(바꾼 뒤의 값을 읽는다).
    inactive → status=active 인 grant 를 paused 로. active·미정지 → 정지 사유가 목표 비활성인
    paused grant 중 만료 전·정책/배정 유효인 것만 active 로. 바뀐 게 없으면 아무것도 하지 않는다(멱등).

    실패는 **삼키지 않는다.** 목표만 바뀌고 grant 가 어긋난 채 커밋되는 것보다 전체 롤백이 낫다 —
    호출자의 트랜잭션(목표 UPDATE·감사·grant·결정 로그)이 함께 되돌아간다.
    """
    goal_id = str(goal_id)
    goal = await conn.fetchrow(
        "SELECT tenant_id::text AS tenant_id, status, paused_at FROM goals WHERE id = $1::uuid FOR UPDATE",
        goal_id,
    )
    if not goal:
        return {"suspended": [], "restored": []}
    if goal_blocks_grants(goal["status"], goal["paused_at"]):
        rows = await conn.fetch(SUSPEND_SQL, goal_id, goal["tenant_id"], SUSPEND_REASON, source)
        return {"suspended": [r["id"] for r in rows], "restored": []}
    rows = await conn.fetch(RESTORE_SQL, goal_id, goal["tenant_id"], source)
    return {"suspended": [], "restored": [r["id"] for r in rows]}
