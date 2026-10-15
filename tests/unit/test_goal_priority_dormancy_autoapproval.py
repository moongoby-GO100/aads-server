"""목표 휴면·P0 상한·자동승인 정본 이관·게이트 status 조건.

PRD goal-priority-dormancy-autoapproval-display-prd (§3 PR-1·PR-2, §4 DO-1·DO-2·DO-5,
§5 AA-1~AA-7). DB 는 쓰지 않는다 — 경계(I/O)만 대역으로 바꾸고 판정 코드는 그대로 돈다.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.services import goal_auto_approval as gaa
from app.services import goal_signals as signals
from app.services import live_trading_guard as guard

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "22222222-2222-4222-8222-222222222222"
GOAL = "11111111-1111-4111-8111-111111111111"
GRANT = "33333333-3333-4333-8333-333333333333"
POLICY_VERSION = "44444444-4444-4444-8444-444444444444"


# ── 휴면 (DO-1·DO-2) ────────────────────────────────────────────────────────
def test_dormant_boundary_is_strictly_more_than_14_days():
    exactly = NOW - timedelta(days=14)
    assert signals.derive_dormancy("active", exactly, has_block_reason=False, now=NOW) == (False, None)
    over = exactly - timedelta(seconds=1)
    assert signals.derive_dormancy("active", over, has_block_reason=False, now=NOW) == (
        True, signals.DORMANT_INACTIVE,
    )


def test_no_activity_record_is_dormant_and_closed_goals_never_are():
    assert signals.derive_dormancy("draft", None, has_block_reason=False, now=NOW)[0] is True
    for closed in ("completed", "cancelled", "archived"):
        assert signals.derive_dormancy(closed, None, has_block_reason=False, now=NOW) == (False, None)


def test_blocked_without_reason_is_dormant_even_with_recent_activity():
    recent = NOW - timedelta(hours=1)
    assert signals.derive_dormancy("blocked", recent, has_block_reason=False, now=NOW) == (
        True, signals.DORMANT_BLOCKED_WITHOUT_REASON,
    )
    assert signals.derive_dormancy("blocked", recent, has_block_reason=True, now=NOW) == (False, None)
    # 사유는 차단 감사행 note 또는 일시정지 사유.
    assert signals.has_block_reason("  ", None) is False
    assert signals.has_block_reason(None, "대표님 결정 대기") is True
    # rev2: 차단 목표는 휴면 카드가 아니라 차단 해소 카드로 간다.
    assert signals.cleanup_lane("blocked", True) == signals.CLEANUP_BLOCKED
    assert signals.cleanup_lane("active", True) == signals.CLEANUP_DORMANT
    assert signals.cleanup_lane("active", False) is None


def test_activity_sql_ignores_goals_updated_at():
    """자동화가 계속 갱신하는 goals.updated_at 은 활동 근거가 아니다."""
    sql = signals.LAST_ACTIVITY_SQL
    assert "updated_at" in sql  # 러너 작업·링크의 updated_at 은 본다
    assert not re.search(r"\bg\.updated_at\b", sql)
    assert "goals.updated_at" not in sql
    # rev2: 지시문 GOAL_ID 줄과 pipeline_jobs.goal_id 둘 다 본다.
    assert "GOAL_ID:" in sql and "JOIN g ON g.id = j.goal_id" in sql


def test_no_auto_close_path_in_derivation_modules():
    """DO-5: 휴면 판정은 표시만 한다. 상태를 쓰는 문장이 없어야 한다."""
    src = (REPO / "app/services/goal_signals.py").read_text(encoding="utf-8")
    assert "UPDATE goals" not in src and "DELETE" not in src


def test_priority_rank_orders_p0_critical_first():
    ranks = [signals.priority_rank(p) for p in ("P0-CRITICAL", "P0", "P1", "P2", "P3", None)]
    assert ranks == sorted(ranks) and ranks[0] == 0 and ranks[-1] == 9
    assert signals.is_p0("P0-CRITICAL") and signals.is_p0("p0") and not signals.is_p0("P1")


# ── 자동승인 표시 상태 ──────────────────────────────────────────────────────
def _grant(**over):
    base = {
        "id": GRANT, "status": "active", "max_risk_tier": "A1",
        "max_executions": 10, "used_executions": 2,
        "valid_from": NOW - timedelta(days=1), "expires_at": NOW + timedelta(days=29),
        "issued_at": NOW - timedelta(days=1), "conditions": {"set_by": "CEO", "set_at": "2026-10-14"},
        "policy_version": POLICY_VERSION, "grant_version": 1, "revocation_epoch": 0,
        "tenant_id": TENANT,
    }
    base.update(over)
    return base


@pytest.mark.parametrize("kwargs,state", [
    ({"status": "active", "policy": {}, "grant": None}, "manual"),
    ({"status": "active", "policy": {"db_ddl": "lead"}, "grant": None}, "delegated"),
    ({"status": "active", "policy": {"auto_approve_high": False, "max_executions": 5}, "grant": None}, "off"),
    ({"status": "active", "policy": {"auto_approve_high": True, "max_executions": 5, "used": 1}, "grant": None}, "high"),
    ({"status": "active", "policy": {"auto_approve_high": True, "auto_approve_critical": True,
                                      "max_executions": 5}, "grant": None}, "all"),
    ({"status": "active", "policy": {"auto_approve_high": True, "max_executions": 5, "used": 5}, "grant": None}, "exhausted"),
    ({"status": "blocked", "policy": {"auto_approve_high": True, "max_executions": 5}, "grant": None}, "paused"),
    ({"status": "active", "policy": {}, "grant": _grant()}, "high"),
    ({"status": "active", "policy": {}, "grant": _grant(max_risk_tier="A2")}, "all"),
    ({"status": "active", "policy": {}, "grant": _grant(used_executions=10)}, "exhausted"),
    ({"status": "active", "policy": {}, "grant": _grant(expires_at=NOW - timedelta(seconds=1))}, "expired"),
    ({"status": "active", "policy": {}, "grant": _grant(status="paused")}, "paused"),
    ({"status": "blocked", "policy": {}, "grant": _grant()}, "paused"),
    ({"status": "active", "policy": {}, "grant": _grant(status="revoked")}, "off"),
])
def test_auto_approval_view_states(kwargs, state):
    view = signals.auto_approval_view(paused_at=None, dormant=False, now=NOW, **kwargs)
    assert view["state"] == state
    assert {"state", "used", "max", "expires_at", "set_by", "set_at"} <= set(view)


def test_auto_approval_view_dormant_or_paused_goal_is_paused():
    for paused_at, dormant in ((NOW, False), (None, True)):
        view = signals.auto_approval_view(
            status="active", paused_at=paused_at, dormant=dormant,
            policy={}, grant=_grant(), now=NOW,
        )
        assert view["state"] == "paused"


# ── 게이트 판정 (AA-2·AA-4) ─────────────────────────────────────────────────
def _cand(status="active", grant=None, policy=None, **over):
    c = gaa.Candidate(
        goal_id=GOAL, title="목표", status=status, project="AADS", tenant_id=TENANT,
        paused_at=None, policy=policy if policy is not None else {}, grant=grant,
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_blocked_goal_grant_is_ignored():
    v = gaa.evaluate(_cand("blocked", grant=_grant()), "high", now=NOW)
    assert v.allowed is False and "goal_status_blocked" in v.reasons
    for status in ("draft",):
        assert gaa.evaluate(_cand(status, grant=_grant()), "high", now=NOW).allowed is False


def test_paused_or_dormant_goal_grant_is_ignored():
    assert gaa.evaluate(_cand(grant=_grant(), paused_at=NOW), "high", now=NOW).reasons == ["goal_paused"]
    v = gaa.evaluate(_cand(grant=_grant(), dormant=True, dormant_reason="inactive_14d"), "high", now=NOW)
    assert v.reasons == ["goal_dormant_inactive_14d"]


def test_expired_grant_is_rejected():
    v = gaa.evaluate(_cand(grant=_grant(expires_at=NOW)), "high", now=NOW)
    assert v.allowed is False and v.reasons == ["grant_expired"]


def test_exhausted_grant_is_rejected():
    v = gaa.evaluate(_cand(grant=_grant(used_executions=10, max_executions=10)), "high", now=NOW)
    assert v.allowed is False and v.reasons == ["grant_exhausted"]


def test_paused_grant_on_active_goal_is_rejected():
    v = gaa.evaluate(_cand(grant=_grant(status="paused")), "high", now=NOW)
    assert v.reasons == ["grant_paused"]


def test_critical_needs_a2_grant():
    assert gaa.evaluate(_cand(grant=_grant()), "critical", now=NOW).reasons == ["risk_tier_exceeds_grant"]
    assert gaa.evaluate(_cand(grant=_grant(max_risk_tier="A2")), "critical", now=NOW).allowed is True
    assert gaa.evaluate(_cand(grant=_grant(max_risk_tier="A2")), "high", now=NOW).allowed is True


def test_legacy_policy_gets_the_same_goal_conditions():
    policy = {"auto_approve_high": True, "max_executions": 5, "used": 0}
    assert gaa.evaluate(_cand(policy=dict(policy)), "high", now=NOW).allowed is True
    blocked = gaa.evaluate(_cand("blocked", policy=dict(policy)), "high", now=NOW)
    assert blocked.allowed is False and blocked.source == gaa.SOURCE_POLICY
    assert gaa.evaluate(_cand(policy=dict(policy)), "critical", now=NOW).reasons == ["policy_tier_not_enabled"]
    assert gaa.evaluate(_cand(policy={**policy, "used": 5}), "high", now=NOW).reasons == ["policy_exhausted"]


# ── goal_policy_allows 통합 (경계만 대역) ─────────────────────────────────────
class _GatePool:
    """goal_policy_allows 가 닿는 DB 경계. 조건부 UPDATE 를 실제처럼 흉내 낸다."""

    def __init__(self, *, status="active", grant=None, policy=None, last_activity=None):
        self.goal = {
            "id": GOAL, "title": "목표", "status": status, "project": "AADS",
            "tenant_id": TENANT, "paused_at": None,
            "approval_policy": policy if policy is not None else {},
        }
        self.grant = grant
        self.last_activity = last_activity or datetime.now(timezone.utc)
        self.candidate_args = None
        self.decisions = []
        self.executed = []

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query, *args):
        if "FROM goal_task_links l" in query and "JOIN goals g" in query and "legacy_allowed" in query:
            self.candidate_args = args
            return [dict(self.goal)] if args[2] == "AADS" else []
        if "FROM goal_auto_approval_grants" in query:
            return [{"goal_id": GOAL, **self.grant}] if self.grant else []
        if "WITH g AS" in query:
            return [{"goal_id": GOAL, "last_activity_at": self.last_activity, "block_note": None}]
        return []

    async def fetchrow(self, query, *args):
        if "UPDATE goal_auto_approval_grants" in query:
            g = self.grant
            now = datetime.now(timezone.utc)
            if (g and g["status"] == "active" and g["used_executions"] < g["max_executions"]
                    and g["valid_from"] <= now < g["expires_at"]):
                g["used_executions"] += 1
                return {"used_executions": g["used_executions"], "max_executions": g["max_executions"]}
            return None
        if "UPDATE goals" in query:
            p = self.goal["approval_policy"]
            if int(p.get("used", 0)) < int(p.get("max_executions", 0)):
                p["used"] = int(p.get("used", 0)) + 1
                return {"used": p["used"], "max_exec": p["max_executions"]}
            return None
        return None

    async def fetchval(self, query, *args):
        if "goal_approval_policy_versions" in query:
            return POLICY_VERSION
        return None

    async def execute(self, query, *args):
        if "INSERT INTO goal_policy_decisions" in query:
            self.decisions.append(args)
        self.executed.append(query)
        return "OK"


def _live_grant(**over):
    now = datetime.now(timezone.utc)
    return _grant(valid_from=now - timedelta(days=1), expires_at=now + timedelta(days=29), **over)


async def _noop(*_a, **_k):
    return None


def _allows(pool, risk="high", project="aads"):
    with patch("app.core.db_pool.get_pool", return_value=pool), \
            patch.object(guard, "notify_only", _noop):
        return asyncio.run(guard.goal_policy_allows(
            SESSION, "patch_remote_file", {"project": project}, risk,
        ))


def _decision_result(args):
    # _INSERT_DECISION 의 $15 = result, $16 = reason_codes, $18 = matched_grant_id
    return args[14], args[15], args[17]


def test_gate_grant_pass_increments_used_and_records_decision():
    pool = _GatePool(grant=_live_grant(used_executions=0, max_executions=3))
    assert _allows(pool) == GOAL
    assert pool.grant["used_executions"] == 1  # AA-7: 통과 1회당 +1
    assert _allows(pool) == GOAL
    assert pool.grant["used_executions"] == 2
    result, reasons, matched = _decision_result(pool.decisions[-1])
    assert result == "AUTO" and reasons == ["auto_approved"] and matched == GRANT
    # approval_policy 호환 뷰도 같은 숫자로 맞춘다.
    assert any("jsonb_set(approval_policy" in q for q in pool.executed)


def test_gate_ignores_blocked_goal_grant_and_records_denial():
    pool = _GatePool(status="blocked", grant=_live_grant())
    assert _allows(pool) is None
    assert pool.grant["used_executions"] == 2  # 세지 않는다
    result, reasons, matched = _decision_result(pool.decisions[-1])
    assert result == "APPROVAL_REQUIRED" and "goal_status_blocked" in reasons and matched == GRANT


def test_gate_rejects_expired_and_exhausted_grants():
    now = datetime.now(timezone.utc)
    expired = _GatePool(grant=_grant(valid_from=now - timedelta(days=31), expires_at=now - timedelta(seconds=1)))
    assert _allows(expired) is None
    assert "grant_expired" in _decision_result(expired.decisions[-1])[1]
    exhausted = _GatePool(grant=_live_grant(used_executions=10, max_executions=10))
    assert _allows(exhausted) is None
    assert "grant_exhausted" in _decision_result(exhausted.decisions[-1])[1]


def test_gate_rejects_dormant_goal():
    pool = _GatePool(grant=_live_grant(), last_activity=datetime.now(timezone.utc) - timedelta(days=15))
    assert _allows(pool) is None
    assert "goal_dormant_inactive_14d" in _decision_result(pool.decisions[-1])[1]


def test_gate_legacy_policy_path_counts_and_blocks_non_active():
    policy = {"auto_approve_high": True, "max_executions": 2, "used": 0}
    pool = _GatePool(policy=dict(policy))
    assert _allows(pool) == GOAL and pool.goal["approval_policy"]["used"] == 1
    assert _allows(pool) == GOAL and pool.goal["approval_policy"]["used"] == 2
    assert _allows(pool) is None  # 소진
    blocked = _GatePool(status="blocked", policy=dict(policy))
    assert _allows(blocked) is None and blocked.goal["approval_policy"]["used"] == 0


def test_gate_consume_race_loses_closed():
    """판정 뒤 다른 호출이 마지막 1회를 가져갔으면 통과하지 않는다."""
    pool = _GatePool(grant=_live_grant(used_executions=0, max_executions=1))

    original = pool.fetchrow

    async def racing(query, *args):
        if "UPDATE goal_auto_approval_grants" in query:
            pool.grant["used_executions"] = 1  # 다른 호출이 먼저 씀
        return await original(query, *args)

    pool.fetchrow = racing
    assert _allows(pool) is None
    assert "grant_consume_failed" in _decision_result(pool.decisions[-1])[1]


def test_gate_project_binding_kept():
    pool = _GatePool(grant=_live_grant())
    assert _allows(pool, project="go100") is None
    assert pool.candidate_args[2] == "GO100"


def test_decision_row_contract():
    cand = _cand(grant=_grant())
    row = gaa.build_decision(
        cand, gaa.Verdict(True, gaa.SOURCE_GRANT, [], 3, 10), session_id=SESSION,
        tool_name="patch_remote_file", risk_level="high", target_project="AADS",
        policy_version=POLICY_VERSION, decided_at=NOW,
    )
    assert row["result"] == "AUTO" and row["effective_application_result"] == "AUTO"
    assert row["matched_grant_id"] == GRANT and row["grant_version"] == 1
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", row["decision_input_hash"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", row["precondition_snapshot_hash"])
    assert row["environment"] in {"dev", "staging", "production"}
    assert row["signature_key_id"].strip() and row["signature_key_version"] > 0
    # 무서명 기록은 실행 근거(HMAC 검증)로 쓰일 수 없다.
    assert row["signature_algorithm"] != "HMAC-SHA-256"
    legacy = gaa.build_decision(
        _cand(policy={"auto_approve_high": True}), gaa.Verdict(False, gaa.SOURCE_POLICY, ["policy_exhausted"]),
        session_id=SESSION, tool_name="t", risk_level="critical", target_project="AADS",
        policy_version=POLICY_VERSION, decided_at=NOW,
    )
    assert legacy["matched_grant_id"] is None and legacy["grant_version"] is None
    assert legacy["risk_tier"] == "A2" and legacy["approval_route"] == "CEO_APPROVAL"


# ── P0 상한·CEO 전용 (PR-1·PR-2) ─────────────────────────────────────────────
class _PriorityDB:
    def __init__(self, current, others):
        self.current = current
        self.others = others

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        assert "SELECT priority, status FROM goals" in query
        return dict(self.current)

    async def fetch(self, query, *args):
        assert "LIKE 'P0%'" in query and args[1] == GOAL
        return [dict(o) for o in self.others]


class _Machine:
    def __init__(self):
        self.calls = []

    async def update_goal(self, goal_id, *, tenant_id=None, actor=None, **kw):
        self.calls.append(kw)
        return {"goal_id": goal_id, "updated": list(kw)}

    async def activate_goal(self, goal_id):
        self.calls.append({"activate": goal_id})
        return {"goal_id": goal_id, "status": "active"}


def _p0(n):
    return [{"goal_id": f"g{i}", "title": f"P0 목표 {i}", "project": "AADS", "priority": "P0"} for i in range(n)]


CEO = {"tenant": {"id": TENANT}, "user": {"user_id": "u-ceo", "is_internal_admin": True, "user_role": "admin"}}
RUNNER = {"tenant": {"id": TENANT}, "user": {"user_id": "system:pipeline-runner",
                                              "is_internal_admin": True, "user_role": "system"}}
MEMBER = {"tenant": {"id": TENANT}, "user": {"user_id": "u-member", "is_internal_admin": False}}


def _put(monkeypatch, db, req, context):
    from app.routers import goals
    from app.services import goal_manager

    machine = _Machine()

    async def validated(goal_id, context, *, member=False):
        return TENANT

    monkeypatch.setattr(goals, "_validated_tenant_goal", validated)
    monkeypatch.setattr(goal_manager, "goal_state_machine", machine)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: db)
    return machine, asyncio.run(goals.update_goal(GOAL, req, context))


def test_fourth_active_p0_is_rejected_with_current_list(monkeypatch):
    from app.routers import goals

    db = _PriorityDB({"priority": "P1", "status": "active"}, _p0(3))
    with pytest.raises(HTTPException) as exc:
        _put(monkeypatch, db, goals.GoalUpdateRequest(priority="P0"), CEO)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "p0_cap_exceeded"
    assert [g["goal_id"] for g in exc.value.detail["current_p0"]] == ["g0", "g1", "g2"]


def test_third_p0_is_allowed_for_ceo(monkeypatch):
    from app.routers import goals

    db = _PriorityDB({"priority": "P1", "status": "active"}, _p0(2))
    machine, result = _put(monkeypatch, db, goals.GoalUpdateRequest(priority="P0"), CEO)
    assert result["updated"] == ["priority"] and machine.calls == [{"priority": "P0"}]


def test_p0_critical_counts_as_p0_and_reactivation_is_capped(monkeypatch):
    from app.routers import goals

    db = _PriorityDB({"priority": "P0-CRITICAL", "status": "blocked"}, _p0(3))
    with pytest.raises(HTTPException) as exc:
        _put(monkeypatch, db, goals.GoalUpdateRequest(status="active"), MEMBER)
    assert exc.value.status_code == 409


def test_existing_active_p0_edit_is_not_blocked_by_cap(monkeypatch):
    from app.routers import goals

    db = _PriorityDB({"priority": "P0", "status": "active"}, _p0(5))
    _machine, result = _put(monkeypatch, db, goals.GoalUpdateRequest(priority="P0", title="제목"), MEMBER)
    assert set(result["updated"]) == {"priority", "title"}


@pytest.mark.parametrize("context", [RUNNER, MEMBER], ids=["runner", "member"])
def test_non_ceo_priority_change_is_forbidden(monkeypatch, context):
    from app.routers import goals

    db = _PriorityDB({"priority": "P1", "status": "active"}, [])
    with pytest.raises(HTTPException) as exc:
        _put(monkeypatch, db, goals.GoalUpdateRequest(priority="P2"), context)
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "priority_change_ceo_only"


def test_activate_endpoint_enforces_cap(monkeypatch):
    from app.routers import goals
    from app.services import goal_manager

    async def validated(goal_id, context, *, member=False):
        return TENANT

    monkeypatch.setattr(goals, "_validated_tenant_goal", validated)
    monkeypatch.setattr(goal_manager, "goal_state_machine", _Machine())
    monkeypatch.setattr("app.core.db_pool.get_pool",
                        lambda: _PriorityDB({"priority": "P0", "status": "draft"}, _p0(3)))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.activate_goal(GOAL, CEO))
    assert exc.value.status_code == 409


# ── GET /goals 신규 필드 ────────────────────────────────────────────────────
class _ListConn:
    def __init__(self, rows, activity, grants):
        self.rows, self.activity, self.grants = rows, activity, grants

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query, *args):
        if "WITH g AS" in query:
            return self.activity
        if "FROM goal_auto_approval_grants" in query:
            return self.grants
        return self.rows


def test_list_goals_adds_signals_and_keeps_existing_fields():
    from app.services.goal_manager import GoalStateMachine

    recent = datetime.now(timezone.utc) - timedelta(days=1)
    old = datetime.now(timezone.utc) - timedelta(days=20)
    rows = [
        {"id": GOAL, "project": "AADS", "title": "활동 중", "priority": "P0", "status": "active",
         "progress": 0.5, "created_at": recent, "completed_at": None, "paused_at": None,
         "paused_reason": None, "approval_policy": {}},
        {"id": "55555555-5555-4555-8555-555555555555", "project": "GO100", "title": "멈춤",
         "priority": "P1", "status": "blocked", "progress": 0, "created_at": old,
         "completed_at": None, "paused_at": None, "paused_reason": None,
         "approval_policy": '{"auto_approve_high": true, "max_executions": 5, "used": 1, "set_by": "CEO"}'},
    ]
    activity = [
        {"goal_id": GOAL, "last_activity_at": recent, "block_note": None},
        {"goal_id": rows[1]["id"], "last_activity_at": old, "block_note": None},
    ]
    grants = [{"goal_id": GOAL, **_live_grant(max_risk_tier="A2")}]
    conn = _ListConn(rows, activity, grants)
    machine = GoalStateMachine()

    async def pool():
        return conn

    machine._pool = pool
    out = asyncio.run(machine.list_goals(tenant_id=TENANT))

    legacy_keys = {"goal_id", "project", "title", "priority", "status", "goal_paused",
                   "goal_paused_reason", "progress", "created_at", "completed_at"}
    new_keys = {"priority_rank", "last_activity_at", "dormant", "dormant_reason", "auto_approval"}
    for item in out:
        assert legacy_keys | new_keys <= set(item)
        assert {"state", "used", "max", "expires_at", "set_by", "set_at"} <= set(item["auto_approval"])
    first, second = out
    assert first["priority_rank"] == 1 and first["dormant"] is False
    assert first["auto_approval"]["state"] == "all" and first["auto_approval"]["source"] == "grant"
    assert second["dormant"] is True and second["dormant_reason"] == "blocked_without_reason"
    assert second["auto_approval"]["state"] == "paused" and second["auto_approval"]["used"] == 1


def test_list_goals_survives_signal_lookup_failure():
    from app.services.goal_manager import GoalStateMachine

    class _Broken(_ListConn):
        async def fetch(self, query, *args):
            if "WITH g AS" in query or "goal_auto_approval_grants" in query:
                raise RuntimeError("relation does not exist")
            return self.rows

    rows = [{"id": GOAL, "project": "AADS", "title": "t", "priority": "P2", "status": "active",
             "progress": 0, "created_at": None, "completed_at": None, "paused_at": None,
             "paused_reason": None, "approval_policy": {}}]
    machine = GoalStateMachine()

    async def pool():
        return _Broken(rows, [], [])

    machine._pool = pool
    out = asyncio.run(machine.list_goals())
    assert out[0]["goal_id"] == GOAL and out[0]["dormant"] is True  # 근거 없음 → 휴면 표시
    assert out[0]["auto_approval"]["state"] == "manual"


# ── 마이그레이션·dry-run ─────────────────────────────────────────────────────
MIGRATION = REPO / "migrations" / "20261015_goal_auto_approval_grants_from_policy.sql"
DOWN = REPO / "migrations" / "rollback" / "20261015_goal_auto_approval_grants_from_policy.down.sql"


def test_migration_keeps_policy_source_and_defaults():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert not re.search(r"UPDATE\s+goals\b", sql), "approval_policy 원본을 바꾸면 안 된다"
    assert "interval '30 days'" in sql
    assert "'paused'" in sql and "goal_not_active:" in sql
    assert "'live_trading_gate'" in sql
    assert DOWN.exists()
    down = DOWN.read_text(encoding="utf-8")
    assert "migration_rollback" in down and "DELETE" not in down.upper()


def test_rollback_marker_matches_guard_constant():
    assert f"'{signals.ROLLBACK_REVOKE_REASON}'" in DOWN.read_text(encoding="utf-8")
    assert f"'{gaa.BRIDGE_POLICY_PROJECT}'" in MIGRATION.read_text(encoding="utf-8")


def test_dry_run_reads_the_migration_plan_verbatim():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "goal_grant_dry_run", REPO / "scripts" / "goal_grant_migration_dry_run.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    plan = mod.plan_select(MIGRATION.read_text(encoding="utf-8"))
    assert "p_goal_id" not in plan and "NULL::uuid" in plan
    sql = mod.build_sql(plan)
    assert sql.lstrip().startswith("BEGIN READ ONLY;") and sql.rstrip().endswith("ROLLBACK;")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER)\b", sql)
