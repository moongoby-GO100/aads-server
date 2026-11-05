"""막힌 목표 재작업 자동승인 — 정지 grant 소비 원자성(TOCTOU)·재작업 job 귀속.

DB 는 쓰지 않는다. SQL 의 조건은 (1) 문자열이 한 벌인지 구조로 고정하고 (2) 같은 조건을 파이썬으로 옮긴 대역이
판정 뒤에 바뀐 상태(배정 비활성·새 정책 버전)를 어떻게 거르는지로 확인한다. 진짜 PostgreSQL 의미는
test_goal_grant_pg_semantics.py(GOAL_GRANT_TEST_ADMIN_URL 필요)가 RESTORE_SQL 로 이미 증명한다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.services import goal_auto_approval as gaa
from app.services import goal_grant_suspension as suspension
from app.services import live_trading_guard as guard
from tests.unit.test_goal_priority_dormancy_autoapproval import (
    GOAL, GRANT, TENANT, _decision_result, _GatePool, _live_grant, _noop, SESSION,
)

PARENT = "runner-aaaaaaa1"
CHILD = "runner-bbbbbbb2"
OTHER = "runner-ccccccc3"
MS = "55555555-5555-4555-8555-555555555555"


def _instruction(parent=PARENT, milestone=MS):
    return f"GOAL_ID: {GOAL}\nMILESTONE_ID: {milestone}\nRETRY_OF_LINK: {parent}\n"


class _World:
    """consume·detect_rework 가 닿는 DB 경계. 정지 grant 의 소비 조건은 REWORK_GRANT_ELIGIBLE_PREDICATE 를 옮긴 것."""

    def __init__(self, *, grant_status="paused"):
        self.grant = {
            "id": GRANT, "status": grant_status, "revoke_reason": "goal_not_active",
            "used_executions": 0, "max_executions": 5, "goal_id": GOAL, "tenant_id": TENANT,
            "valid_from": datetime.now(timezone.utc) - timedelta(days=1),
            "expires_at": datetime.now(timezone.utc) + timedelta(days=29),
        }
        self.assignment_active = True
        self.newer_policy = False
        self.jobs = {
            CHILD: {"instruction": _instruction(), "status": "running"},
            OTHER: {"instruction": "일반 작업", "status": "running"},
            PARENT: {"instruction": f"GOAL_ID: {GOAL}\nMILESTONE_ID: {MS}\n", "status": "error"},
        }
        self.linked_jobs = {PARENT}
        self.milestone_blocked = True
        self.ledger = [{"job_id": CHILD, "milestone": MS, "lineage": PARENT}]
        self.ledger_error: Exception | None = None
        self.consume_sql: list[str] = []

    def eligible(self, tenant, goal) -> bool:
        g = self.grant
        return (g["status"] == "paused"
                and g["revoke_reason"] in ("goal_not_active", "goal_paused")
                and g["expires_at"] > datetime.now(timezone.utc)
                and self.assignment_active and not self.newer_policy
                and g["goal_id"] == goal and g["tenant_id"] == tenant)

    async def fetchrow(self, query, *args):
        if "FROM pipeline_jobs" in query:
            job = self.jobs.get(args[0])
            if not job or ("status = 'running'" in query and job["status"] != "running"):
                return None
            return {"instruction": job["instruction"]}
        if "UPDATE goal_auto_approval_grants" in query:
            self.consume_sql.append(query)
            g, allow, goal, tenant = self.grant, args[1], args[2], args[3]
            usable = g["used_executions"] < g["max_executions"]
            if g["status"] == "active" or (allow and self.eligible(tenant, goal)):
                if usable:
                    g["used_executions"] += 1
                    return {"used_executions": g["used_executions"], "max_executions": g["max_executions"]}
            return None
        return None

    async def fetchval(self, query, *args):
        if "FROM milestones m" in query:
            return 1 if self.milestone_blocked and args[2] in self.linked_jobs else None
        if "SELECT lineage FROM goal_retry_executions" in query:
            if self.ledger_error:
                raise self.ledger_error
            return next((r["lineage"] for r in self.ledger if r["job_id"] == args[2]), None)
        if "SELECT count(*) FROM goal_retry_executions" in query:
            if self.ledger_error:
                raise self.ledger_error
            return sum(1 for r in self.ledger if r["lineage"] == args[2])
        if "FROM goal_auto_approval_grants gr" in query:
            return 1 if self.eligible(args[2], args[1]) else None
        return None


def _blocked(world, **over):
    cand = gaa.Candidate(
        goal_id=GOAL, title="막힌 목표", status="blocked", project="AADS", tenant_id=TENANT,
        paused_at=None, policy={}, grant=dict(world.grant),
    )
    for k, v in over.items():
        setattr(cand, k, v)
    return cand


def _run(coro):
    return asyncio.run(coro)


# ── 조건 한 벌 ───────────────────────────────────────────────────────────────
def test_eligibility_predicate_is_shared_by_all_three_sql():
    pred = suspension.REWORK_GRANT_ELIGIBLE_PREDICATE
    assert pred in suspension.RESTORE_SQL
    assert pred in suspension.REWORK_GRANT_ELIGIBLE_SQL
    world = _World()
    _run(gaa.consume(world, _blocked(world, rework=True, rework_grant_eligible=True)))
    assert pred in world.consume_sql[0]
    # 조건 요소: 사유 허용값 · 만료 · 배정 active · 더 새 정책 버전 없음
    for needle in ("revoke_reason IN ('goal_not_active', 'goal_paused')", "gr.expires_at > clock_timestamp()",
                   "a.active", "NOT EXISTS (SELECT 1 FROM goal_approval_policy_versions"):
        assert needle in pred


def test_consume_binds_paused_branch_to_goal_tenant_and_flag():
    world = _World()
    _run(gaa.consume(world, _blocked(world, rework=True, rework_grant_eligible=True)))
    sql = world.consume_sql[0]
    assert "gr.status = 'active'" in sql and "gr.status = 'paused' AND $2::boolean" in sql
    assert "gr.goal_id = $3::uuid AND gr.tenant_id = $4::uuid" in sql
    assert "gr.used_executions < gr.max_executions" in sql


# ── 고칠 것 1: 정지 grant 소비 원자성 ────────────────────────────────────────
def test_a_assignment_deactivated_after_verdict_consume_none():
    world = _World()
    cand = _blocked(world, rework=True, rework_grant_eligible=True)  # 판정 때는 적격이었다
    world.assignment_active = False  # 판정과 소비 사이에 배정이 꺼졌다
    assert _run(gaa.consume(world, cand)) is None
    assert world.grant["used_executions"] == 0


def test_b_newer_policy_version_after_verdict_consume_none():
    world = _World()
    cand = _blocked(world, rework=True, rework_grant_eligible=True)
    world.newer_policy = True
    assert _run(gaa.consume(world, cand)) is None
    assert world.grant["used_executions"] == 0


def test_c_paused_rework_consumes_once_and_stays_paused():
    world = _World()
    cand = _blocked(world, rework=True, rework_grant_eligible=True)
    assert _run(gaa.consume(world, cand)) == (1, 5)
    assert world.grant["used_executions"] == 1 and world.grant["status"] == "paused"


def test_paused_grant_without_rework_flag_is_never_consumed():
    world = _World()
    assert _run(gaa.consume(world, _blocked(world))) is None
    assert _run(gaa.consume(world, _blocked(world, rework=True, rework_grant_eligible=False))) is None
    assert world.grant["used_executions"] == 0


def test_d_active_grant_path_unchanged():
    world = _World(grant_status="active")
    assert _run(gaa.consume(world, _blocked(world))) == (1, 5)
    world.assignment_active = False  # 활성 grant 는 정지 조건과 무관하다(stale 트리거 몫)
    assert _run(gaa.consume(world, _blocked(world))) == (2, 5)
    assert world.grant["status"] == "active"


def test_eligible_check_marks_candidate_and_fails_closed_on_error():
    world = _World()
    cand = _blocked(world)
    _run(gaa.mark_rework(world, cand, CHILD))
    assert cand.rework is True and cand.rework_grant_eligible is True and cand.rework_job_id == CHILD

    world.assignment_active = False
    cand = _blocked(world)
    _run(gaa.mark_rework(world, cand, CHILD))
    assert cand.rework is True and cand.rework_grant_eligible is False
    assert gaa.evaluate(cand, "high").allowed is False  # 정지 grant 를 열지 못한다


# ── 고칠 것 2: 재작업 job 귀속 ──────────────────────────────────────────────
def test_e_no_job_id_means_no_exemption():
    world = _World()
    for job in ("", None, "chat-session-uuid", "runner-zzzz"):
        assert _run(gaa.detect_rework(world, _blocked(world), job)) is False
    assert gaa.trusted_runner_job_id() == ""  # 설정한 곳이 없으면 비어 있다


def test_e_same_session_other_job_gets_no_exemption():
    """같은 세션의 다른 running job(재작업 표식 없음)은 면제 못 받는다 — job_id 로만 지시서를 찾는다."""
    world = _World()
    assert _run(gaa.detect_rework(world, _blocked(world), OTHER)) is False


def test_f_ledger_has_lineage_entry_but_current_job_not_charged():
    world = _World()
    world.ledger = [{"job_id": "runner-ddddddd4", "milestone": MS, "lineage": PARENT}]
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False


def test_g_current_job_charged_is_rework():
    world = _World()
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is True
    world.ledger.append({"job_id": "runner-ddddddd4", "milestone": MS, "lineage": PARENT})
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is True  # 총 2 = 상한


def test_lineage_total_above_cap_is_not_rework():
    world = _World()
    world.ledger += [{"job_id": f"runner-eeeeeee{i}", "milestone": MS, "lineage": PARENT} for i in (1, 2)]
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False


def test_h_ledger_exception_means_no_exemption():
    world = _World()
    world.ledger_error = RuntimeError("relation goal_retry_executions does not exist")
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False


def test_strict_lineage_executions_does_not_swallow():
    world = _World()
    world.ledger_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        _run(gaa._strict_lineage_executions(world, TENANT, MS, PARENT))


def test_parent_lineage_must_match_the_jobs_own_ledger_lineage():
    world = _World()
    world.ledger = [{"job_id": CHILD, "milestone": MS, "lineage": "runner-9999999f"}]
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False


def test_rework_requires_blocked_goal_blocked_milestone_and_linked_parent():
    world = _World()
    active = _blocked(world, status="active")
    assert _run(gaa.detect_rework(world, active, CHILD)) is False
    world.milestone_blocked = False
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False
    world.milestone_blocked = True
    world.linked_jobs = set()
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False


def test_job_must_be_running():
    world = _World()
    world.jobs[CHILD]["status"] = "done"
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False


def test_marker_in_body_is_not_a_retry():
    world = _World()
    world.jobs[CHILD]["instruction"] = f"설명 RETRY_OF_LINK: {PARENT} 언급\nMILESTONE_ID: {MS}\n"
    assert _run(gaa.detect_rework(world, _blocked(world), CHILD)) is False


# ── 판정 ────────────────────────────────────────────────────────────────────
def _verdict(cand, risk="high"):
    return gaa.evaluate(cand, risk)


def test_evaluate_exempts_only_blocked_related_reasons():
    world = _World()
    world.grant.update(max_risk_tier="A1", issued_at=datetime.now(timezone.utc))
    cand = _blocked(world, rework=True, rework_grant_eligible=True, dormant=True,
                    dormant_reason="blocked_without_reason",
                    last_activity_at=datetime.now(timezone.utc))
    v = _verdict(cand)
    assert v.allowed is True and v.rework_exempted is True
    assert _verdict(cand, "critical").allowed is False  # 위험 등급 상한은 완화하지 않는다
    cand.paused_at = datetime.now(timezone.utc)
    assert "goal_paused" in _verdict(cand).reasons
    cand.paused_at = None
    cand.last_activity_at = datetime.now(timezone.utc) - timedelta(days=15)
    assert "goal_dormant_inactive_14d" in _verdict(cand).reasons
    cand.last_activity_at = datetime.now(timezone.utc)
    cand.grant["used_executions"] = cand.grant["max_executions"]
    assert "grant_exhausted" in _verdict(cand).reasons


def test_evaluate_without_rework_flag_keeps_denying_blocked():
    world = _World()
    cand = _blocked(world)
    assert "goal_status_blocked" in _verdict(cand).reasons and "grant_paused" in _verdict(cand).reasons


# ── goal_policy_allows 통합 ─────────────────────────────────────────────────
class _ReworkPool(_GatePool):
    def __init__(self, world, **kw):
        super().__init__(status="blocked", grant=world.grant, **kw)
        self.world = world
        self.goal["tenant_id"] = TENANT

    async def fetchrow(self, query, *args):
        if "FROM pipeline_jobs" in query or "UPDATE goal_auto_approval_grants" in query:
            return await self.world.fetchrow(query, *args)
        return await super().fetchrow(query, *args)

    async def fetchval(self, query, *args):
        if "goal_approval_policy_versions" in query and "gr.status = 'paused'" not in query:
            return await super().fetchval(query, *args)
        return await self.world.fetchval(query, *args)


def _gate_world():
    world = _World()
    world.grant.update(max_risk_tier="A1", policy_version="44444444-4444-4444-8444-444444444444",
                       grant_version=1, revocation_epoch=0, issued_at=datetime.now(timezone.utc),
                       actions=[], conditions={})
    return world


def _allows_as(pool, job_id, tool_input=None):
    token = gaa.current_runner_job_id.set(job_id)
    try:
        with patch("app.core.db_pool.get_pool", return_value=pool), patch.object(guard, "notify_only", _noop):
            return asyncio.run(guard.goal_policy_allows(
                SESSION, "patch_remote_file", {"project": "aads", **(tool_input or {})}, "high"))
    finally:
        gaa.current_runner_job_id.reset(token)


def test_gate_runner_job_rework_passes_and_counts_without_unpausing():
    world = _gate_world()
    pool = _ReworkPool(world)
    assert _allows_as(pool, CHILD) == GOAL
    assert world.grant["used_executions"] == 1 and world.grant["status"] == "paused"
    result, reasons, matched = _decision_result(pool.decisions[-1])
    assert result == "AUTO" and gaa.REASON_REWORK in reasons and matched == GRANT


def test_gate_tool_input_job_id_is_not_trusted():
    world = _gate_world()
    pool = _ReworkPool(world)
    assert _allows_as(pool, "", {"job_id": CHILD, "runner_job_id": CHILD}) is None
    assert world.grant["used_executions"] == 0
    assert "goal_status_blocked" in _decision_result(pool.decisions[-1])[1]


def test_gate_same_session_other_job_denied():
    world = _gate_world()
    pool = _ReworkPool(world)
    assert _allows_as(pool, OTHER) is None
    assert world.grant["used_executions"] == 0


def test_gate_ledger_failure_denies():
    world = _gate_world()
    world.ledger_error = RuntimeError("down")
    pool = _ReworkPool(world)
    assert _allows_as(pool, CHILD) is None
    assert world.grant["used_executions"] == 0


def test_gate_assignment_off_denies_paused_grant():
    world = _gate_world()
    world.assignment_active = False
    pool = _ReworkPool(world)
    assert _allows_as(pool, CHILD) is None
    assert world.grant["used_executions"] == 0


def test_gate_live_grant_still_works_for_active_goal():
    pool = _GatePool(grant=_live_grant(used_executions=0, max_executions=3))
    with patch("app.core.db_pool.get_pool", return_value=pool), patch.object(guard, "notify_only", _noop):
        assert asyncio.run(guard.goal_policy_allows(SESSION, "patch_remote_file", {"project": "aads"}, "high")) == GOAL
    assert pool.grant["used_executions"] == 1
