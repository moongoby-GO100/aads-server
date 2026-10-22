"""목표 첫 정리 일괄 반영 + 비활성 목표 자동승인 정지·복원.

PRD goal-first-triage-execution-prd §3-1 TR-1~6, §3-2 AS-1~4. DB 는 쓰지 않는다 — 상태를 가진 대역
연결(StateConn)이 SQL 문장을 알아보고 같은 규칙으로 메모리 상태를 바꾼다. 트랜잭션은 스냅샷 롤백이다.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.routers import goals as goals_router
from app.services import goal_grant_suspension as gs
from app.services import goal_manager
from app.services import goal_triage as triage

REPO = Path(__file__).resolve().parents[2]
TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime.now(timezone.utc)
FUTURE = NOW + timedelta(days=20)
PAST = NOW - timedelta(days=1)


def gid(n: int) -> str:
    return "%08d-0000-4000-8000-000000000000" % n


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.snapshot = copy.deepcopy(self.conn.state)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.state = self.snapshot
        return False


class StateConn:
    def __init__(self):
        self.state = {"goals": {}, "grants": {}, "audit": [], "logs": [], "runs": {}}

    # ── 구성 ────────────────────────────────────────────────────────────────
    def goal(self, n, status="active", priority="P1", paused=False, title=None):
        self.state["goals"][gid(n)] = {
            "id": gid(n), "title": title or "goal-%d" % n, "project": "AADS", "status": status,
            "priority": priority, "paused_at": NOW if paused else None, "created_at": NOW + timedelta(seconds=n),
        }
        return gid(n)

    def grant(self, n, goal, status="active", reason=None, expires=FUTURE):
        gr = "%08d-1111-4111-8111-000000000000" % n
        self.state["grants"][gr] = {
            "id": gr, "goal_id": goal, "tenant_id": TENANT, "status": status, "revoke_reason": reason,
            "revoked_at": None if status == "active" else NOW, "expires_at": expires,
            "policy_version": "pv", "grant_version": 1,
        }
        return gr

    def transaction(self):
        return _Tx(self)

    def version(self):
        return run(triage.current_candidates_version(self, TENANT))

    # ── SQL 대역 ────────────────────────────────────────────────────────────
    def _goal_blocks(self, g):
        return g["status"] != "active" or g["paused_at"] is not None

    async def execute(self, sql, *a):
        s = self.state
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if sql.startswith("UPDATE goals SET priority"):
            s["goals"][a[0]]["priority"] = a[2]
            return "UPDATE 1"
        if sql.startswith("UPDATE goals SET status = 'active', paused_at = NULL"):
            s["goals"][a[0]].update(status="active", paused_at=None)
            return "UPDATE 1"
        if sql.startswith("UPDATE goals SET paused_at = NOW()"):
            s["goals"][a[0]]["paused_at"] = NOW
            return "UPDATE 1"
        if sql.startswith("UPDATE goals SET status = 'cancelled'"):
            s["goals"][a[0]]["status"] = "cancelled"
            return "UPDATE 1"
        if sql.startswith("UPDATE goals SET status = 'archived'"):
            s["goals"][a[0]]["status"] = "archived"
            return "UPDATE 1"
        if "UPDATE goals SET status = 'blocked'" in " ".join(sql.split()):
            g = s["goals"][a[0]]
            if g["status"] in ("draft", "active"):
                g["status"] = "blocked"
                return "UPDATE 1"
            return "UPDATE 0"
        if sql.startswith("INSERT INTO goal_status_audit"):
            s["audit"].append({"goal_id": a[0], "old": a[1], "new": a[2], "actor": a[3], "source": a[4],
                               "note": a[6]})
            return "INSERT 0 1"
        if sql.startswith("INSERT INTO goal_triage_runs"):
            s["runs"][a[2]] = {"request_hash": a[3], "response": a[5]}
            return "INSERT 0 1"
        raise AssertionError("unexpected execute: " + sql[:80])

    async def fetchrow(self, sql, *a):
        s = self.state
        if "FROM goal_triage_runs" in sql:
            return s["runs"].get(a[1])
        if sql.startswith("SELECT tenant_id::text AS tenant_id, status, paused_at FROM goals"):
            g = s["goals"].get(a[0])
            return {"tenant_id": TENANT, "status": g["status"], "paused_at": g["paused_at"]} if g else None
        if sql.startswith("SELECT status, tenant_id::text AS tenant_id FROM goals"):
            g = s["goals"].get(a[0])
            return {"status": g["status"], "tenant_id": TENANT} if g else None
        raise AssertionError("unexpected fetchrow: " + sql[:80])

    async def fetch(self, sql, *a):
        s = self.state
        if sql == gs.SUSPEND_SQL:
            goal_id, _tenant, reason, source = a
            g = s["goals"][goal_id]
            goal_status = "paused" if g["status"] == "active" else g["status"]
            out = []
            if not self._goal_blocks(g):
                return out
            for gr in s["grants"].values():
                if gr["goal_id"] == goal_id and gr["status"] == "active":
                    gr.update(status="paused", revoked_at=NOW, revoke_reason=reason)
                    s["logs"].append({"grant": gr["id"], "decision": "DENY", "event": "grant_suspended",
                                      "goal_status": goal_status, "source": source})
                    out.append({"id": gr["id"]})
            return out
        if sql == gs.RESTORE_SQL:
            goal_id, _tenant, source = a
            g = s["goals"][goal_id]
            out = []
            if g["status"] != "active" or g["paused_at"] is not None:
                return out
            for gr in s["grants"].values():
                reason = gr["revoke_reason"] or ""
                if (gr["goal_id"] == goal_id and gr["status"] == "paused"
                        and (reason in ("goal_not_active", "goal_paused") or reason.startswith("goal_not_active:"))
                        and gr["expires_at"] > NOW):
                    gr.update(status="active", revoked_at=None, revoke_reason=None)
                    s["logs"].append({"grant": gr["id"], "decision": "NOTIFY", "event": "grant_restored",
                                      "source": source})
                    out.append({"id": gr["id"]})
            return out
        if sql.startswith("SELECT id, title, project, status, priority, paused_at FROM goals"):
            open_s, wanted = set(a[1]), set(a[2])
            return [dict(g) for g in sorted(s["goals"].values(), key=lambda g: g["id"])
                    if g["status"] in open_s or g["id"] in wanted]
        if sql.startswith("SELECT id, status, priority, paused_at FROM goals"):
            return [dict(g) for g in s["goals"].values() if g["status"] in set(a[1])]
        if "FROM goals" in sql and "LIKE 'P0%'" in sql:
            return [dict(g, goal_id=g["id"]) for g in sorted(s["goals"].values(), key=lambda g: g["created_at"])
                    if g["status"] == "active" and (g["priority"] or "").upper().startswith("P0")]
        raise AssertionError("unexpected fetch: " + sql[:80])

    # ── 관찰 ────────────────────────────────────────────────────────────────
    def active_p0(self):
        return sorted(g["id"] for g in self.state["goals"].values()
                      if g["status"] == "active" and g["priority"].startswith("P0"))


class StatePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


CEO = {"tenant": {"id": TENANT}, "user": {"user_id": "ceo-1", "is_internal_admin": True, "user_role": "admin"}}


def call(conn, *, keep=(), actions=(), version=None, key="key-1", context=CEO):
    req = goals_router.TriageApplyRequest(
        keep_p0=list(keep), actions=list(actions),
        candidates_version=version if version is not None else conn.version(),
    )
    with patch("app.core.db_pool.get_pool", return_value=StatePool(conn)):
        return run(goals_router.apply_goal_triage(req, idempotency_key=key, context=context))


def act(goal, action, **kw):
    return goals_router.TriageActionRequest(goal_id=goal, action=action, **kw)


def err(exc: pytest.ExceptionInfo) -> dict:
    return exc.value.detail


def five_p0() -> tuple[StateConn, list[str]]:
    conn = StateConn()
    ids = [conn.goal(n, priority="P0") for n in range(1, 6)]
    return conn, ids


# ── TR-1·2·6: 정상 반영 ─────────────────────────────────────────────────────
def test_keep_p0_demotes_the_rest_and_audits_each_change():
    conn, ids = five_p0()
    res = call(conn, keep=ids[:3])
    assert conn.active_p0() == sorted(ids[:3])
    assert {d["goal_id"] for d in res["downgraded"]} == {ids[3], ids[4]}
    assert all(d["to"] == "P1" for d in res["downgraded"])
    audit = conn.state["audit"]
    assert len(audit) == res["audit_rows"] == 2
    for row in audit:
        assert row["actor"] == "user:ceo-1" and row["source"] == "triage_apply"
        assert row["old"] == row["new"] == "active"
        note = json.loads(row["note"])
        assert note["triage_run_id"] == res["triage_run_id"]
        assert note["priority"]["from"] == "P0" and note["priority"]["to"] == "P1"


def test_close_is_cancelled_merge_is_archived_and_nothing_is_deleted():
    conn = StateConn()
    a = conn.goal(1, priority="P1")
    b = conn.goal(2, priority="P2")
    c = conn.goal(3, status="blocked")
    before_ids = set(conn.state["goals"])
    res = call(conn, actions=[act(b, "close", reason="중복"), act(c, "merge", merge_into=a)])
    goals = conn.state["goals"]
    assert set(goals) == before_ids  # 행 삭제 없음
    assert goals[b]["status"] == "cancelled" and goals[c]["status"] == "archived"
    by_goal = {r["goal_id"]: r for r in conn.state["audit"]}
    assert (by_goal[b]["old"], by_goal[b]["new"]) == ("active", "cancelled")
    note_c = json.loads(by_goal[c]["note"])
    assert (by_goal[c]["old"], by_goal[c]["new"]) == ("blocked", "archived")
    assert note_c["merged_into"] == a and note_c["action"] == "merge"
    assert res["audit_rows"] == len(res["changed_goal_ids"]) == 2


def test_noop_actions_write_no_audit_rows():
    conn = StateConn()
    a = conn.goal(1)
    res = call(conn, actions=[act(a, "resume")])
    assert res["audit_rows"] == 0 and conn.state["audit"] == []


# ── TR-3: P0 상한 → 전체 롤백 ───────────────────────────────────────────────
def test_p0_over_cap_rolls_back_everything_with_409():
    conn = StateConn()
    keep = [conn.goal(n, priority="P0") for n in (1, 2, 3)]
    extra = conn.goal(4, priority="P0")
    blocked_p0 = conn.goal(5, status="blocked", priority="P0")
    conn.grant(1, extra)
    conn.grant(2, blocked_p0)
    snapshot = copy.deepcopy(conn.state)
    with pytest.raises(HTTPException) as exc:
        call(conn, keep=keep, actions=[act(blocked_p0, "resume")])
    assert exc.value.status_code == 409 and err(exc)["error"] == "p0_cap_exceeded"
    assert err(exc)["cap"] == 3
    assert conn.state == snapshot  # 4번 목표 강등·5번 재개·감사·grant 모두 되돌려짐
    assert conn.state["goals"][extra]["priority"] == "P0"


def test_resume_within_cap_passes():
    conn = StateConn()
    keep = [conn.goal(n, priority="P0") for n in (1, 2)]
    b = conn.goal(3, status="blocked", priority="P0")
    res = call(conn, keep=keep, actions=[act(b, "unblock")])
    assert res["active_p0"] == 3 and conn.state["goals"][b]["status"] == "active"


# ── TR-4: stale_candidates ──────────────────────────────────────────────────
def test_stale_candidates_returns_409_with_latest_and_changes_nothing():
    conn, ids = five_p0()
    old_version = conn.version()
    conn.state["goals"][ids[0]]["priority"] = "P1"  # 카드를 만든 뒤 누가 바꿈
    snapshot = copy.deepcopy(conn.state)
    with pytest.raises(HTTPException) as exc:
        call(conn, keep=ids[1:4], version=old_version)
    d = err(exc)
    assert exc.value.status_code == 409 and d["error"] == "stale_candidates"
    assert d["candidates_version"] == conn.version() != old_version
    assert {c["goal_id"] for c in d["candidates"]} == set(ids)
    assert conn.state == snapshot


def test_candidates_version_ignores_title_but_tracks_priority_status_and_pause():
    conn = StateConn()
    a = conn.goal(1, priority="P1")
    v0 = conn.version()
    conn.state["goals"][a]["title"] = "renamed"
    assert conn.version() == v0
    conn.state["goals"][a]["paused_at"] = NOW
    v1 = conn.version()
    assert v1 != v0
    conn.state["goals"][a]["status"] = "blocked"
    assert conn.version() not in (v0, v1)


# ── TR-5: 멱등 ──────────────────────────────────────────────────────────────
def test_same_idempotency_key_replays_same_response_without_reapplying():
    conn, ids = five_p0()
    version = conn.version()
    first = call(conn, keep=ids[:3], version=version, key="k-1")
    audit_after_first = len(conn.state["audit"])
    again = call(conn, keep=ids[:3], version=version, key="k-1")
    assert again == first
    assert len(conn.state["audit"]) == audit_after_first


def test_same_key_different_request_is_422_and_missing_key_is_400():
    conn, ids = five_p0()
    version = conn.version()
    call(conn, keep=ids[:3], version=version, key="k-1")
    with pytest.raises(HTTPException) as exc:
        call(conn, keep=ids[1:4], version=version, key="k-1")
    assert exc.value.status_code == 422 and err(exc)["error"] == "idempotency_key_reused"
    with pytest.raises(HTTPException) as exc:
        call(conn, keep=ids[:3], key=None)
    assert exc.value.status_code == 400 and err(exc)["error"] == "idempotency_key_required"


# ── CEO 전용 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("user", [
    {"user_id": "member-1", "is_internal_admin": False, "user_role": "member"},
    {"user_id": "system:pipeline-runner", "is_internal_admin": True, "user_role": "system"},
])
def test_non_ceo_gets_403_and_database_is_not_touched(user):
    conn, ids = five_p0()
    snapshot = copy.deepcopy(conn.state)
    with pytest.raises(HTTPException) as exc:
        call(conn, keep=ids[:3], context={"tenant": {"id": TENANT}, "user": user})
    assert exc.value.status_code == 403 and err(exc)["error"] == "triage_ceo_only"
    assert conn.state == snapshot


# ── 요청 검증 ────────────────────────────────────────────────────────────────
def test_keep_p0_cap_in_request_model_and_invalid_actions():
    with pytest.raises(ValueError):
        goals_router.TriageApplyRequest(keep_p0=[gid(n) for n in range(1, 5)], candidates_version="v")
    conn = StateConn()
    a = conn.goal(1)
    done = conn.goal(2, status="completed")
    for actions, code in [
        ([act(a, "merge")], "merge_into_required"),
        ([act(a, "merge", merge_into=a)], "invalid_merge_target"),
        ([act(a, "unblock")], "not_blocked"),
        ([act(done, "close")], "goal_already_closed"),
        ([act(a, "pause"), act(a, "close")], "duplicate_action"),
    ]:
        snapshot = copy.deepcopy(conn.state)
        with pytest.raises(HTTPException) as exc:
            call(conn, actions=actions, key="k-" + code)
        assert err(exc)["error"] == code
        assert conn.state == snapshot


def test_keep_p0_must_be_an_active_p0():
    conn = StateConn()
    p1 = conn.goal(1, priority="P1")
    with pytest.raises(HTTPException) as exc:
        call(conn, keep=[p1])
    assert err(exc)["error"] == "keep_p0_not_active_p0"


# ── AS-1: 상태가 active 를 떠나면 grant 정지 ────────────────────────────────
def test_blocked_transition_pauses_active_grants_with_reason_and_event():
    conn = StateConn()
    g = conn.goal(1)
    gr = conn.grant(1, g)
    other = conn.goal(2)
    other_gr = conn.grant(2, other)
    run(goal_manager._audited_goal_status_update(
        conn, g, "blocked",
        "UPDATE goals SET status = 'blocked', updated_at = NOW() WHERE id = $1::uuid AND status IN ('draft', 'active')",
        g, source="task_failure_cascade", actor="system:goal_manager",
    ))
    row = conn.state["grants"][gr]
    assert row["status"] == "paused" and row["revoke_reason"] == "goal_not_active" and row["revoked_at"]
    assert conn.state["grants"][other_gr]["status"] == "active"  # 다른 목표는 그대로
    assert [(e["decision"], e["event"], e["goal_status"]) for e in conn.state["logs"]] == [
        ("DENY", "grant_suspended", "blocked")]


def test_triage_actions_suspend_and_restore_grants():
    conn = StateConn()
    to_pause = conn.goal(1)
    to_close = conn.goal(2)
    blocked = conn.goal(3, status="blocked")
    g1, g2 = conn.grant(1, to_pause), conn.grant(2, to_close)
    g3 = conn.grant(3, blocked, status="paused", reason="goal_not_active:blocked")
    res = call(conn, actions=[act(to_pause, "pause"), act(to_close, "close"), act(blocked, "unblock")])
    grants = conn.state["grants"]
    assert grants[g1]["status"] == "paused" and grants[g2]["status"] == "paused"
    assert grants[g3]["status"] == "active" and grants[g3]["revoke_reason"] is None
    assert sorted(res["grants"]["suspended"]) == sorted([g1, g2]) and res["grants"]["restored"] == [g3]


# ── AS-2: 복원은 정지 사유가 목표 비활성인 만료 전 grant 만 ─────────────────
def test_restore_only_goal_inactive_paused_unexpired_grants():
    conn = StateConn()
    g = conn.goal(1, status="blocked")
    ok = conn.grant(1, g, status="paused", reason="goal_not_active")
    migrated = conn.grant(2, g, status="paused", reason="goal_not_active:blocked")
    human = conn.grant(3, g, status="revoked", reason="ceo_revoked")
    human_paused = conn.grant(4, g, status="paused", reason="manual_pause")
    expired = conn.grant(5, g, status="paused", reason="goal_not_active", expires=PAST)
    stale = conn.grant(6, g, status="stale", reason="goal_not_active")
    conn.state["goals"][g]["status"] = "active"
    out = run(gs.sync_goal_grants(conn, g, source="update_goal"))
    assert sorted(out["restored"]) == sorted([ok, migrated]) and out["suspended"] == []
    grants = conn.state["grants"]
    assert grants[ok]["status"] == grants[migrated]["status"] == "active"
    for untouched in (human, human_paused, expired, stale):
        assert grants[untouched]["status"] != "active"
    assert grants[human]["revoke_reason"] == "ceo_revoked"
    assert {e["decision"] for e in conn.state["logs"]} == {"NOTIFY"}


def test_no_restore_while_goal_is_paused_and_sync_is_idempotent():
    conn = StateConn()
    g = conn.goal(1, paused=True)
    gr = conn.grant(1, g, status="paused", reason="goal_not_active")
    assert run(gs.sync_goal_grants(conn, g, source="goal_resume"))["restored"] == []
    assert conn.state["grants"][gr]["status"] == "paused"
    conn.state["goals"][g]["paused_at"] = None
    assert run(gs.sync_goal_grants(conn, g, source="goal_resume"))["restored"] == [gr]
    assert run(gs.sync_goal_grants(conn, g, source="goal_resume")) == {"suspended": [], "restored": []}


def test_sync_failure_always_propagates_and_rolls_back_the_goal_update():
    class Broken(StateConn):
        async def fetchrow(self, sql, *a):
            raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        run(gs.sync_goal_grants(Broken(), gid(1), source="update_goal"))

    # 목표 UPDATE 뒤 grant 정합이 실패하면 목표 변경·감사가 함께 되돌아간다(삼켜서 불일치로 커밋하지 않는다).
    class GrantFails(StateConn):
        async def fetch(self, sql, *a):
            if sql == gs.SUSPEND_SQL:
                raise RuntimeError("grant update failed")
            return await super().fetch(sql, *a)

    conn = GrantFails()
    g = conn.goal(1)
    gr = conn.grant(1, g)
    with pytest.raises(RuntimeError):
        run(goal_manager._audited_goal_status_update(
            conn, g, "blocked",
            "UPDATE goals SET status = 'blocked', updated_at = NOW() WHERE id = $1::uuid AND status IN ('draft', 'active')",
            g, source="task_failure_cascade", actor="system:goal_manager",
        ))
    assert conn.state["goals"][g]["status"] == "active"
    assert conn.state["grants"][gr]["status"] == "active" and conn.state["audit"] == []


# ── 정합 스크립트 · SQL 한 벌 · 라우트 노출 ─────────────────────────────────
def _load_script():
    spec = importlib.util.spec_from_file_location(
        "goal_grant_suspend_reconcile", REPO / "scripts" / "goal_grant_suspend_reconcile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reconcile_script_reuses_the_helper_sql_and_never_deletes():
    mod = _load_script()
    assert mod.RULES.SUSPEND_SQL == gs.SUSPEND_SQL
    plan = [{"goal_id": gid(1), "tenant_id": TENANT, "goal_status": "blocked", "goal_paused": False}]
    sql = mod.build_apply_sql(plan)
    assert sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")
    assert "FOR UPDATE" in sql  # 적용 트랜잭션 안에서 목표 행을 먼저 잠근다
    assert "$" not in sql.replace("$$", "") and "goal_not_active" in sql
    assert "DELETE" not in sql.upper() and "UPDATE GOALS" not in sql.upper()
    assert "'goal_grant_suspend_reconcile'" in sql
    bad = [dict(plan[0], goal_id="1'; DROP TABLE goals;--")]
    with pytest.raises(SystemExit):
        mod.build_apply_sql(bad)


def test_restore_sql_never_touches_revoked_or_unexpired_check_missing():
    sql = gs.RESTORE_SQL
    assert "gr.status = 'paused'" in sql and "gr.expires_at > clock_timestamp()" in sql
    assert "'revoked'" not in sql and "DELETE" not in (gs.SUSPEND_SQL + sql).upper()


def test_openapi_exposes_triage_apply():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(goals_router.router, prefix="/api/v1")
    schema = app.openapi()
    post = schema["paths"]["/api/v1/goals/triage/apply"]["post"]
    assert any(p["name"] == "Idempotency-Key" for p in post.get("parameters", []))
