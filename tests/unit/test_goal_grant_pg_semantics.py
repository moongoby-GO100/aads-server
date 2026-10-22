"""목표 ↔ 자동승인 grant 정합의 실제 PostgreSQL 검증 (수동 회수 보존 · 오래된 plan · 원자성 · 잠금).

대상 SQL 은 app/services/goal_grant_suspension.py 의 SUSPEND_SQL/RESTORE_SQL 과
app/services/goal_workflow_approval.revoke_grant 의 UPDATE 다. 대역 연결이 아니라 **진짜 PostgreSQL** 에서
돌린다 — 잠금·트랜잭션 롤백은 대역으로 증명되지 않는다.

운영 DB 는 건드리지 않는다. GOAL_GRANT_TEST_ADMIN_URL(예: postgresql://…/postgres, CREATEDB 권한)로 접속해
일회용 DB `goal_grant_pg_<난수>` 를 만들고 아래의 최소 스키마를 깐 뒤, 끝나면 DROP 한다.
환경변수가 없으면 이 파일은 skip 된다. 실행:

    GOAL_GRANT_TEST_ADMIN_URL=postgresql://aads:…@host.docker.internal:5433/postgres \\
        bash scripts/run_unit_tests.sh tests/unit/test_goal_grant_pg_semantics.py

스키마는 마이그레이션(20260919 m12 · 20261015)의 같은 이름 테이블에서 이 로직이 읽고 쓰는 열·CHECK·FK 만 옮긴 것이다.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ADMIN_URL = os.getenv("GOAL_GRANT_TEST_ADMIN_URL", "")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="GOAL_GRANT_TEST_ADMIN_URL (CREATEDB 가능한 접속 URL) 필요 — 일회용 DB 를 만든다")

REPO = Path(__file__).resolve().parents[2]
T1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
POLICY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ASSIGN = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
ACTOR_SESSION = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

SCHEMA = """
CREATE TABLE goals (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL, project TEXT NOT NULL DEFAULT 'AADS',
    title TEXT, priority TEXT DEFAULT 'P1', status TEXT NOT NULL DEFAULT 'active',
    progress NUMERIC DEFAULT 0, completed_at TIMESTAMPTZ,
    paused_at TIMESTAMPTZ, paused_reason TEXT, paused_by TEXT, updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE project_role_assignments (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE goal_approval_policy_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL, project TEXT,
    mode TEXT NOT NULL DEFAULT 'audit_only' CHECK (mode IN ('audit_only','canary','enabled','retired')),
    effective_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
CREATE TABLE goal_auto_approval_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    project TEXT NOT NULL DEFAULT 'AADS', assignment_id UUID NOT NULL,
    goal_id UUID NOT NULL REFERENCES goals(id) ON DELETE RESTRICT,
    parent_grant_id UUID REFERENCES goal_auto_approval_grants(id) ON DELETE RESTRICT,
    policy_version UUID NOT NULL REFERENCES goal_approval_policy_versions(id),
    grant_version INTEGER NOT NULL DEFAULT 1,
    revocation_strategy TEXT NOT NULL DEFAULT 'finish_current'
        CHECK (revocation_strategy IN ('cancel_now','finish_current','compensate')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','revoked','stale','expired','superseded','paused')),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_by UUID, revoked_at TIMESTAMPTZ, revoke_reason TEXT);
CREATE TABLE goal_auto_approval_uses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL, grant_id UUID NOT NULL,
    status TEXT NOT NULL, completed_at TIMESTAMPTZ, result JSONB NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE goal_approval_decision_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('AUTO','NOTIFY','PROJECT_APPROVAL','CEO_APPROVAL','DENY')),
    policy_version UUID, matched_grant_id UUID REFERENCES goal_auto_approval_grants(id) ON DELETE RESTRICT,
    reason_codes TEXT[] NOT NULL DEFAULT '{}', input_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    simulated BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
CREATE TABLE goal_status_audit (
    id BIGSERIAL PRIMARY KEY, goal_id UUID NOT NULL, old_status TEXT, new_status TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(), actor TEXT, source TEXT, tenant_id UUID, note TEXT);
CREATE FUNCTION inject_failure() RETURNS trigger LANGUAGE plpgsql AS
    $$ BEGIN RAISE EXCEPTION 'injected failure on %', TG_TABLE_NAME; END $$;
"""
TABLES = ("goal_approval_decision_logs goal_auto_approval_uses goal_status_audit "
          "goal_auto_approval_grants goals project_role_assignments goal_approval_policy_versions")

LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def db_url():
    import asyncpg

    name = "goal_grant_pg_" + uuid.uuid4().hex[:10]
    parts = urlsplit(ADMIN_URL)
    url = urlunsplit(parts._replace(path="/" + name))

    async def create():
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute('CREATE DATABASE "%s"' % name)
        finally:
            await admin.close()
        conn = await asyncpg.connect(url)
        try:
            await conn.execute(SCHEMA)
        finally:
            await conn.close()

    async def drop():
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        finally:
            await admin.close()

    run(create())
    try:
        yield url
    finally:
        run(drop())


@pytest.fixture
def pg(db_url):
    """테스트마다 비운 일회용 DB + 접속 공장. 끝나면 주입한 trigger 도 걷는다."""
    import asyncpg

    opened = []

    async def connect():
        conn = await asyncpg.connect(db_url)
        opened.append(conn)
        return conn

    async def reset():
        conn = await connect()
        await conn.execute("TRUNCATE %s CASCADE" % TABLES.replace(" ", ", "))
        await conn.execute(
            "INSERT INTO goal_approval_policy_versions (id, tenant_id, mode, created_at) "
            "VALUES ($1::uuid, $2::uuid, 'enabled', clock_timestamp() - interval '30 days')", POLICY, T1)
        await conn.execute(
            "INSERT INTO project_role_assignments (id, tenant_id) VALUES ($1::uuid, $2::uuid)", ASSIGN, T1)
        return conn

    class Env:
        pass

    env = Env()
    env.connect = connect
    env.conn = run(reset())
    try:
        yield env
    finally:
        async def cleanup():
            for tbl in ("goal_auto_approval_grants", "goal_approval_decision_logs"):
                await env.conn.execute("DROP TRIGGER IF EXISTS inj ON %s" % tbl)
            for c in opened:
                await c.close()
        run(cleanup())


def gid(n: int) -> str:
    return "%08d-0000-4000-8000-000000000000" % n


def grid(n: int) -> str:
    return "%08d-1111-4111-8111-000000000000" % n


async def mk_goal(c, n, status="active", paused=False, tenant=T1):
    await c.execute(
        "INSERT INTO goals (id, tenant_id, title, status, paused_at, paused_reason) "
        "VALUES ($1::uuid, $2::uuid, $3, $4, CASE WHEN $5 THEN now() END, CASE WHEN $5 THEN 'x' END)",
        gid(n), tenant, "goal-%d" % n, status, paused)
    return gid(n)


async def mk_grant(c, n, goal, status="active", reason=None, days=20, parent=None, tenant=T1,
                   strategy="finish_current", assignment=ASSIGN, policy=POLICY):
    await c.execute(
        "INSERT INTO goal_auto_approval_grants (id, tenant_id, assignment_id, goal_id, parent_grant_id, "
        "policy_version, revocation_strategy, status, expires_at, revoked_at, revoke_reason) "
        "VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6::uuid, $7, $8, "
        "now() + make_interval(days => $9), CASE WHEN $8 <> 'active' THEN now() END, $10)",
        grid(n), tenant, assignment, goal, parent, policy, strategy, status, days, reason)
    return grid(n)


async def grant(c, n):
    return await c.fetchrow(
        "SELECT status, revoke_reason, revoked_by::text AS revoked_by FROM goal_auto_approval_grants "
        "WHERE id = $1::uuid", grid(n))


async def count(c, table, where="TRUE"):
    return await c.fetchval("SELECT count(*) FROM %s WHERE %s" % (table, where))


def inject(table):
    return ("CREATE TRIGGER inj BEFORE %s ON %s FOR EACH ROW EXECUTE FUNCTION inject_failure()"
            % ("UPDATE" if table == "goal_auto_approval_grants" else "INSERT", table))


@pytest.fixture(scope="module")
def rules():
    return _load("goal_grant_suspension_pg", "app/services/goal_grant_suspension.py")


@pytest.fixture
def wf(monkeypatch):
    from app.services import goal_workflow_approval as mod

    monkeypatch.setattr(mod, "workflow_approval_enabled", lambda: True)
    return mod


def actor():
    from app.services.goal_work_hierarchy import ActorScope

    return ActorScope(tenant_id=T1, session_id=ACTOR_SESSION, role_key="Lead", project="AADS",
                      workspace_kind="project")


async def revoke(wf, c, n, tenant=T1, reason="ceo_revoked"):
    async with c.transaction():
        return await wf.revoke_grant(c, tenant_id=tenant, grant_id=grid(n), actor=actor(), reason=reason)


def resume_sql():
    return "UPDATE goals SET paused_at = NULL, paused_reason = NULL, paused_by = NULL WHERE id = $1::uuid"


# ── 1. 수동 회수 보존 ─────────────────────────────────────────────────────────
def test_revoke_of_paused_grant_is_durable_across_resume(pg, wf, rules):
    c = pg.conn

    async def go():
        g = await mk_goal(c, 1, status="blocked")
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active")
        out = await revoke(wf, c, 1)
        assert out["id"] == grid(1)
        row = await grant(c, 1)
        assert row["status"] == "revoked" and row["revoke_reason"] == "ceo_revoked"
        assert row["revoked_by"] == ACTOR_SESSION
        await c.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)
        synced = await rules.sync_goal_grants(c, g, source="update_goal")
        assert synced["restored"] == []
        assert (await grant(c, 1))["status"] == "revoked"

    run(go())


def test_revoke_paused_parent_cascades_to_paused_children_and_none_is_restored(pg, wf, rules):
    c = pg.conn

    async def go():
        g = await mk_goal(c, 1, status="blocked")
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active")
        await mk_grant(c, 2, g, status="paused", reason="goal_not_active", parent=grid(1))
        await mk_grant(c, 3, g, status="paused", reason="goal_not_active", parent=grid(2))  # 손자
        await revoke(wf, c, 1)
        assert [(await grant(c, n))["status"] for n in (1, 2, 3)] == ["revoked"] * 3
        assert (await grant(c, 2))["revoke_reason"] == "parent_grant_revoked"
        await c.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)
        assert (await rules.sync_goal_grants(c, g, source="update_goal"))["restored"] == []
        assert [(await grant(c, n))["status"] for n in (1, 2, 3)] == ["revoked"] * 3

    run(go())


def test_revoke_paused_child_only_keeps_parent_restorable(pg, wf, rules):
    c = pg.conn

    async def go():
        g = await mk_goal(c, 1, status="blocked")
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active")
        await mk_grant(c, 2, g, status="paused", reason="goal_not_active", parent=grid(1))
        await revoke(wf, c, 2)
        await c.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)
        out = await rules.sync_goal_grants(c, g, source="update_goal")
        assert out["restored"] == [grid(1)]
        assert (await grant(c, 1))["status"] == "active" and (await grant(c, 2))["status"] == "revoked"

    run(go())


def test_revoke_active_grant_unchanged_and_other_states_are_returned_as_is(pg, wf):
    c = pg.conn

    async def go():
        g = await mk_goal(c, 1)
        await mk_grant(c, 1, g)
        await mk_grant(c, 2, g, parent=grid(1))
        await mk_grant(c, 3, g, status="revoked", reason="earlier")
        await mk_grant(c, 4, g, status="expired", reason="ttl")
        await mk_grant(c, 5, g, status="stale", reason="stale_policy")
        await mk_grant(c, 6, g, status="superseded", reason="v2")
        await revoke(wf, c, 1)
        assert [(await grant(c, n))["status"] for n in (1, 2)] == ["revoked", "revoked"]
        for n, status, reason in ((3, "revoked", "earlier"), (4, "expired", "ttl"),
                                  (5, "stale", "stale_policy"), (6, "superseded", "v2")):
            out = await revoke(wf, c, n, reason="late")
            assert out["status"] == status
            row = await grant(c, n)
            assert row["status"] == status and row["revoke_reason"] == reason and row["revoked_by"] is None

    run(go())


def test_revoke_is_tenant_scoped_and_leaves_other_tenant_and_goal_untouched(pg, wf):
    from fastapi import HTTPException

    c = pg.conn

    async def go():
        g1 = await mk_goal(c, 1, status="blocked")
        g2 = await mk_goal(c, 2, status="blocked", tenant=T2)
        await mk_grant(c, 1, g1, status="paused", reason="goal_not_active")
        await mk_grant(c, 2, g2, status="paused", reason="goal_not_active", tenant=T2)
        with pytest.raises(HTTPException) as exc:
            await revoke(wf, c, 2, tenant=T1)
        assert exc.value.status_code == 404
        await revoke(wf, c, 1)
        assert (await grant(c, 2))["status"] == "paused"
        assert (await c.fetchval("SELECT status FROM goals WHERE id = $1::uuid", g2)) == "blocked"
        assert (await c.fetchval("SELECT status FROM goals WHERE id = $1::uuid", g1)) == "blocked"

    run(go())


def test_revoke_cancel_now_strategy_still_cancels_running_uses_for_paused_grant(pg, wf):
    c = pg.conn

    async def go():
        g = await mk_goal(c, 1, status="blocked")
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active", strategy="cancel_now")
        await c.execute("INSERT INTO goal_auto_approval_uses (tenant_id, grant_id, status) "
                        "VALUES ($1::uuid, $2::uuid, 'executing')", T1, grid(1))
        await revoke(wf, c, 1)
        assert await c.fetchval("SELECT status FROM goal_auto_approval_uses") == "failed"

    run(go())


def test_end_to_end_block_revoke_reactivate_stays_revoked(pg, wf, monkeypatch):
    from app.core import db_pool
    from app.services import goal_manager

    c = pg.conn
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(c))

    async def go():
        g = await mk_goal(c, 1)
        await mk_grant(c, 1, g)
        await mk_grant(c, 2, g)
        await goal_manager.goal_state_machine.update_goal(g, tenant_id=T1, actor="ceo", status="blocked")
        assert [(await grant(c, n))["status"] for n in (1, 2)] == ["paused", "paused"]
        await revoke(wf, c, 1)
        await goal_manager.goal_state_machine.update_goal(g, tenant_id=T1, actor="ceo", status="active")
        assert (await grant(c, 1))["status"] == "revoked"
        assert (await grant(c, 2))["status"] == "active"

    run(go())


class _Pool:
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


# ── 복원 가드: 사람 회수 · stale · expired · 정책/배정 변경은 되살리지 않는다 ─────────────
def test_restore_skips_grants_whose_assignment_ended_or_policy_was_replaced_while_paused(pg, rules):
    c = pg.conn

    async def go():
        g = await mk_goal(c, 1, status="blocked")
        # 정지 동안 배정이 끝난 grant — trigger 는 status='active' 만 stale 처리하므로 paused 는 놓친다
        ended_assign = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        await c.execute("INSERT INTO project_role_assignments (id, tenant_id, active) "
                        "VALUES ($1::uuid, $2::uuid, FALSE)", ended_assign, T1)
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active", assignment=ended_assign)
        # 정지 동안 정책이 교체된 grant (새 정책이 발급 이후에 enabled)
        await mk_grant(c, 2, g, status="paused", reason="goal_not_active")
        await c.execute("UPDATE goal_auto_approval_grants SET issued_at = clock_timestamp() - interval '1 hour'")
        newer = await c.fetchval(
            "INSERT INTO goal_approval_policy_versions (tenant_id, mode, effective_at) "
            "VALUES ($1::uuid, 'enabled', clock_timestamp()) RETURNING id::text", T1)
        # 새 정책 아래 발급된 grant 와, 새 정책이 audit_only 라 교체가 아닌 경우는 되살린다
        await mk_grant(c, 3, g, status="paused", reason="goal_not_active", policy=newer)
        await c.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)
        out = await rules.sync_goal_grants(c, g, source="update_goal")
        assert out["restored"] == [grid(3)]
        assert [(await grant(c, n))["status"] for n in (1, 2, 3)] == ["paused", "paused", "active"]

    run(go())


def test_restore_restores_only_goal_inactive_paused_unexpired_valid_grants(pg, rules):
    c = pg.conn

    async def go():
        g = await mk_goal(c, 1, status="blocked")
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active")
        await mk_grant(c, 2, g, status="paused", reason="goal_not_active:blocked")
        await mk_grant(c, 3, g, status="paused", reason="goal_paused")
        await mk_grant(c, 4, g, status="revoked", reason="ceo_revoked")
        await mk_grant(c, 5, g, status="paused", reason="manual_pause")
        await mk_grant(c, 6, g, status="paused", reason="goal_not_active", days=-1)
        await c.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)
        out = await rules.sync_goal_grants(c, g, source="update_goal")
        assert sorted(out["restored"]) == [grid(1), grid(2), grid(3)]
        assert [(await grant(c, n))["status"] for n in (4, 5, 6)] == ["revoked", "paused", "paused"]
        assert await count(c, "goal_approval_decision_logs", "decision = 'NOTIFY'") == 3
        # 멱등
        assert (await rules.sync_goal_grants(c, g, source="update_goal")) == {"suspended": [], "restored": []}

    run(go())


# ── 2. 오래된 plan ───────────────────────────────────────────────────────────
def _plan(goal, status="blocked", paused=False):
    return [{"goal_id": goal, "tenant_id": T1, "goal_status": status, "goal_paused": paused}]


def test_stale_plan_does_not_touch_goal_that_is_active_now(pg):
    c = pg.conn
    script = _load("reconcile_pg", "scripts/goal_grant_suspend_reconcile.py")

    async def go():
        g = await mk_goal(c, 1, status="blocked")
        await mk_grant(c, 1, g)
        sql = script.build_apply_sql(_plan(g))  # plan 은 blocked 일 때 떠 둔 것
        await c.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)  # 그 뒤 재개
        await c.execute(sql)
        assert (await grant(c, 1))["status"] == "active"
        assert await count(c, "goal_approval_decision_logs") == 0

    run(go())


def test_stale_plan_new_grant_on_now_active_goal_is_untouched_and_blocked_goal_is_suspended(pg):
    c = pg.conn
    script = _load("reconcile_pg2", "scripts/goal_grant_suspend_reconcile.py")

    async def go():
        g1 = await mk_goal(c, 1, status="blocked")
        g2 = await mk_goal(c, 2, status="blocked")
        await mk_grant(c, 1, g1)
        await mk_grant(c, 2, g2)
        sql = script.build_apply_sql(_plan(g1) + _plan(g2))
        await c.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g1)
        await mk_grant(c, 3, g1)  # plan 뒤 새 grant 발급
        await c.execute(sql)
        assert [(await grant(c, n))["status"] for n in (1, 3)] == ["active", "active"]
        assert (await grant(c, 2))["status"] == "paused"
        logs = await c.fetch("SELECT decision, input_context->>'goal_status' AS gs, "
                             "input_context->>'source' AS src FROM goal_approval_decision_logs")
        assert [(r["decision"], r["gs"], r["src"]) for r in logs] == [
            ("DENY", "blocked", "goal_grant_suspend_reconcile")]

    run(go())


def test_apply_uses_current_status_and_labels_paused_active_goal(pg):
    c = pg.conn
    script = _load("reconcile_pg3", "scripts/goal_grant_suspend_reconcile.py")

    async def go():
        g1 = await mk_goal(c, 1, status="active", paused=True)
        g2 = await mk_goal(c, 2, status="blocked")
        await mk_grant(c, 1, g1)
        await mk_grant(c, 2, g2)
        await c.execute("UPDATE goals SET status = 'completed' WHERE id = $1::uuid", g2)  # plan 은 blocked
        await c.execute(script.build_apply_sql(_plan(g1, "active", True) + _plan(g2)))
        assert [(await grant(c, n))["status"] for n in (1, 2)] == ["paused", "paused"]
        got = {r["gid"]: r["gs"] for r in await c.fetch(
            "SELECT input_context->>'goal_id' AS gid, input_context->>'goal_status' AS gs "
            "FROM goal_approval_decision_logs")}
        assert got == {g1: "paused", g2: "completed"}

    run(go())


def test_apply_blocks_on_goal_lock_then_sees_concurrent_resume(pg, rules):
    script = _load("reconcile_pg4", "scripts/goal_grant_suspend_reconcile.py")

    async def go():
        a, b = pg.conn, await pg.connect()
        g = await mk_goal(a, 1, status="blocked")
        await mk_grant(a, 1, g)
        sql = script.build_apply_sql(_plan(g))
        tx = a.transaction()
        await tx.start()
        await a.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)  # 재개, 미커밋
        task = asyncio.ensure_future(b.execute(sql))
        await asyncio.sleep(0.7)
        assert not task.done(), "apply 는 목표 행 잠금 앞에서 기다려야 한다"
        await rules.sync_goal_grants(a, g, source="goal_resume")
        await tx.commit()
        await asyncio.wait_for(task, 10)
        assert (await grant(a, 1))["status"] == "active"
        assert await count(a, "goal_approval_decision_logs") == 0

    run(go())


def test_apply_waits_for_in_flight_grant_issuance_then_suspends_it_too(pg):
    script = _load("reconcile_pg5", "scripts/goal_grant_suspend_reconcile.py")

    async def go():
        a, b = pg.conn, await pg.connect()
        g = await mk_goal(a, 1, status="blocked")
        await mk_grant(a, 1, g)
        sql = script.build_apply_sql(_plan(g))
        tx = a.transaction()
        await tx.start()
        await mk_grant(a, 2, g)  # 발급 중(FK 가 목표 행에 KEY SHARE 를 건다), 미커밋
        task = asyncio.ensure_future(b.execute(sql))
        await asyncio.sleep(0.7)
        assert not task.done()
        await tx.commit()
        await asyncio.wait_for(task, 10)
        assert [(await grant(a, n))["status"] for n in (1, 2)] == ["paused", "paused"]

    run(go())


def test_apply_and_resume_race_never_deadlocks_and_ends_consistent(pg, rules):
    script = _load("reconcile_pg6", "scripts/goal_grant_suspend_reconcile.py")

    async def resume(conn, g):
        async with conn.transaction():
            await conn.execute("UPDATE goals SET status = 'active' WHERE id = $1::uuid", g)
            await rules.sync_goal_grants(conn, g, source="goal_resume")

    async def go():
        a, b = pg.conn, await pg.connect()
        for i in range(1, 9):
            g = await mk_goal(a, i, status="blocked")
            await mk_grant(a, i, g)
            sql = script.build_apply_sql(_plan(g))
            await asyncio.wait_for(asyncio.gather(b.execute(sql), resume(a, g)), 15)
            # 어느 쪽이 먼저든 목표가 active 면 grant 도 active 다.
            assert (await grant(a, i))["status"] == "active", "iteration %d" % i

    run(go())


# ── 3. 원자성 ─────────────────────────────────────────────────────────────────
class _Fail:
    """주입한 실패 뒤 목표·grant·감사·결정 로그가 모두 원래대로인지."""

    @staticmethod
    async def assert_untouched(c, g, status="active", grant_status="active"):
        assert (await c.fetchval("SELECT status FROM goals WHERE id = $1::uuid", g)) == status
        assert (await grant(c, 1))["status"] == grant_status
        assert await count(c, "goal_status_audit") == 0
        assert await count(c, "goal_approval_decision_logs") == 0


@pytest.mark.parametrize("target", ["goal_auto_approval_grants", "goal_approval_decision_logs"])
def test_status_update_rolls_back_goal_grant_and_audit_when_sync_fails(pg, target):
    from app.services import goal_manager

    c = pg.conn

    async def go():
        g = await mk_goal(c, 1)
        await mk_grant(c, 1, g)
        await c.execute(inject(target))
        with pytest.raises(Exception) as exc:
            await goal_manager._audited_goal_status_update(
                c, g, "blocked", "UPDATE goals SET status = 'blocked' WHERE id = $1::uuid", g,
                source="task_failure_cascade", actor="system:goal_manager")
        assert "injected failure" in str(exc.value)
        await _Fail.assert_untouched(c, g)
        await c.execute("DROP TRIGGER inj ON %s" % target)
        # 대조: 주입을 걷으면 같은 호출이 성공하고 정합된다 (테스트가 실제 경로를 탄다는 증거).
        await goal_manager._audited_goal_status_update(
            c, g, "blocked", "UPDATE goals SET status = 'blocked' WHERE id = $1::uuid", g,
            source="task_failure_cascade", actor="system:goal_manager")
        assert (await grant(c, 1))["status"] == "paused"
        assert await count(c, "goal_approval_decision_logs", "decision = 'DENY'") == 1
        assert await count(c, "goal_status_audit") == 1

    run(go())


@pytest.mark.parametrize("target", ["goal_auto_approval_grants", "goal_approval_decision_logs"])
def test_update_goal_rolls_back_everything_when_sync_fails(pg, monkeypatch, target):
    from app.core import db_pool
    from app.services import goal_manager

    c = pg.conn
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(c))

    async def go():
        g = await mk_goal(c, 1)
        await mk_grant(c, 1, g)
        await c.execute(inject(target))
        with pytest.raises(Exception, match="injected failure"):
            await goal_manager.goal_state_machine.update_goal(
                g, tenant_id=T1, actor="ceo", status="blocked", title="새 제목")
        await _Fail.assert_untouched(c, g)
        assert (await c.fetchval("SELECT title FROM goals WHERE id = $1::uuid", g)) == "goal-1"

    run(go())


@pytest.mark.parametrize("target", ["goal_auto_approval_grants", "goal_approval_decision_logs"])
def test_pause_endpoint_rolls_back_when_sync_fails(pg, monkeypatch, target):
    from app.core import db_pool
    from app.routers import goals

    c = pg.conn
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-goal-pause-secret")
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(c))
    ctx = {"tenant": {"id": T1}, "user": {"id": "ceo", "is_internal_admin": True}}

    async def go():
        g = await mk_goal(c, 1)
        await mk_grant(c, 1, g)
        await c.execute(inject(target))
        with pytest.raises(Exception, match="injected failure"):
            await goals.pause_goal(g, goals.GoalPauseRequest(reason="재기획"), ctx)
        assert (await c.fetchval("SELECT paused_at FROM goals WHERE id = $1::uuid", g)) is None
        await _Fail.assert_untouched(c, g)

    run(go())


def test_resume_endpoint_rolls_back_when_restore_fails(pg, monkeypatch):
    from app.core import db_pool
    from app.routers import goals

    c = pg.conn
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-goal-pause-secret")
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(c))
    ctx = {"tenant": {"id": T1}, "user": {"id": "ceo", "is_internal_admin": True}}

    async def go():
        g = await mk_goal(c, 1, paused=True)
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active")
        await c.execute(inject("goal_auto_approval_grants"))
        with pytest.raises(Exception, match="injected failure"):
            await goals.resume_goal(g, ctx)
        assert (await c.fetchval("SELECT paused_at FROM goals WHERE id = $1::uuid", g)) is not None
        assert (await grant(c, 1))["status"] == "paused"
        await c.execute("DROP TRIGGER inj ON goal_auto_approval_grants")
        assert await goals.resume_goal(g, ctx) == {"goal_paused": False, "goal_paused_reason": None}
        assert (await grant(c, 1))["status"] == "active"

    run(go())


def test_pause_then_resume_endpoints_roundtrip_grants(pg, monkeypatch):
    from app.core import db_pool
    from app.routers import goals

    c = pg.conn
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-goal-pause-secret")
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(c))
    ctx = {"tenant": {"id": T1}, "user": {"id": "ceo", "is_internal_admin": True}}

    async def go():
        g = await mk_goal(c, 1)
        await mk_grant(c, 1, g)
        await goals.pause_goal(g, goals.GoalPauseRequest(reason="재기획"), ctx)
        assert (await grant(c, 1))["status"] == "paused"
        await goals.resume_goal(g, ctx)
        assert (await grant(c, 1))["status"] == "active"

    run(go())


def test_progress_reopen_is_one_transaction_with_grant_restore(pg, monkeypatch):
    from app.core import db_pool
    from app.services import goal_manager

    c = pg.conn
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(c))

    async def stats(_conn, _goal_id):
        return 2, 1  # 완료 아님 → 되돌림 분기

    monkeypatch.setattr(goal_manager.GoalStateMachine, "_milestone_completion_stats", staticmethod(stats))

    async def go():
        g = await mk_goal(c, 1, status="completed")
        await mk_grant(c, 1, g, status="paused", reason="goal_not_active")
        await c.execute(inject("goal_auto_approval_grants"))
        with pytest.raises(Exception, match="injected failure"):
            await goal_manager.goal_state_machine._update_goal_progress(g)
        row = await c.fetchrow("SELECT status, progress FROM goals WHERE id = $1::uuid", g)
        assert row["status"] == "completed" and float(row["progress"]) == 0.0
        assert (await grant(c, 1))["status"] == "paused"
        await c.execute("DROP TRIGGER inj ON goal_auto_approval_grants")
        await goal_manager.goal_state_machine._update_goal_progress(g)
        row = await c.fetchrow("SELECT status, progress FROM goals WHERE id = $1::uuid", g)
        assert row["status"] == "active" and float(row["progress"]) == 0.5
        assert (await grant(c, 1))["status"] == "active"

    run(go())
