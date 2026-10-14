"""Canonical work-hierarchy role assignment: evidence, approval, execution, rollback.

Fake-connection tests always run.  The PostgreSQL tests need
CANONICAL_ASSIGN_TEST_DB_URL pointing at a disposable database whose name is
``canonical_assign_test`` (a schema-only copy of the production schema); they
create their own tenants and never clean up, so never aim them at production.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import canonical_hierarchy_assignment as svc
from app.services.canonical_hierarchy_assignment import (
    ROLES,
    build_item_plan,
    decide_assignment,
    execute_assignment,
    ids_fingerprint,
    preview_assignment,
    propose_assignment,
    recommended_role,
    rollback_assignment,
    verify_approval_evidence,
)
from app.services.goal_work_hierarchy import ActorScope, resolve_actor_scope
from app.services.goal_workflow_approval import decide_change_set

ROOT = Path(__file__).parents[2]
SPEC_DIR = ROOT / "docs/specs/canonical-goal-hierarchy"
SPEC = json.loads((SPEC_DIR / "20261014_AADS_업무계층등록명세.json").read_text())
RESULT = json.loads((SPEC_DIR / "20261014_AADS_업무계층등록결과.json").read_text())
PRODUCTION_IDS_MD5 = "c523a6d044c5d0be31f3469161a66beb"
TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000002"
OTHER = "00000000-0000-0000-0000-000000000003"
GOAL = "00000000-0000-0000-0000-000000000004"


def code_of(exc: HTTPException) -> str:
    return exc.detail["code"]


def raises(coro) -> HTTPException:
    with pytest.raises(HTTPException) as info:
        asyncio.run(coro)
    return info.value


# ── pure functions and the real registration artefacts ───────────────────

def test_spec_roles_are_36_30_24_and_every_description_has_one_role():
    counts = {r: 0 for r in ROLES}
    for item in SPEC["items"]:
        role = recommended_role(item["description"])
        assert role in ROLES
        counts[role] += 1
    assert counts == {"PM": 36, "Developer": 30, "QA": 24}
    assert [i["type"] for i in SPEC["items"]].count("epic") == 9


def test_registered_ids_fingerprint_matches_production_md5():
    created = RESULT["created_ids"]
    assert len(created) == 90 and ids_fingerprint(list(created.values())) == PRODUCTION_IDS_MD5


def test_recommended_role_rejects_missing_or_ambiguous():
    assert recommended_role(None) is None
    assert recommended_role("책임 역할: 미배정") is None
    assert recommended_role("(권장 역할: PM) 그리고 (권장 역할: QA)") is None
    assert recommended_role("(권장 역할: Reviewer)") is None
    assert recommended_role("(권장 역할: Developer; 실제 assignment_id 없음)") == "Developer"


def test_item_plan_requires_exact_counts_and_roles():
    items = [{"id": f"i{n}", "description": f"(권장 역할: {r})"} for n, r in enumerate(["PM", "PM", "QA"])]
    plan = build_item_plan(items, {"PM": 2, "Developer": 0, "QA": 1})
    assert plan == {"PM": ["i0", "i1"], "Developer": [], "QA": ["i2"]}
    assert code_of(raises(_async(build_item_plan, items, {"PM": 1, "Developer": 1, "QA": 1}))) == "target_set_mismatch"
    items[0]["description"] = "역할 없음"
    assert code_of(raises(_async(build_item_plan, items, {"PM": 2, "Developer": 0, "QA": 1}))) == "target_set_mismatch"


async def _async(fn, *args):
    return fn(*args)


def test_goal_role_key_is_goal_scoped_and_never_a_project_wide_role():
    assert svc.goal_role_key("PM", "ac431290-66be-4d00-8bbb-9bf162917240") == "PM@ac431290"


# ── actor scope: owner verification is derived, never claimed ────────────

class ScopeConn:
    def __init__(self, user_id):
        self.user_id = user_id

    async def fetchrow(self, sql, *args):
        return {"session_id": SESSION, "role_key": "CEO", "project": "CEO", "workspace_name": "[CEO] 통합지시",
                "display_name": "", "user_id": self.user_id}


def test_actor_scope_owner_verified_only_when_session_belongs_to_authenticated_user():
    owner = asyncio.run(resolve_actor_scope(ScopeConn("u1"), tenant_id=TENANT, user_id="u1", actor_session_id=SESSION))
    assert owner.owner_verified and not owner.internal_admin and owner.user_id == "u1"
    borrowed = asyncio.run(resolve_actor_scope(
        ScopeConn("u1"), tenant_id=TENANT, user_id="svc", actor_session_id=SESSION, internal_admin=True))
    assert borrowed.internal_admin and not borrowed.owner_verified and borrowed.user_id == "svc"
    assert code_of(raises(resolve_actor_scope(
        ScopeConn("u1"), tenant_id=TENANT, user_id="svc", actor_session_id=SESSION))) == "project_scope_denied"


def test_actor_scope_defaults_are_unverified():
    scope = ActorScope(TENANT, SESSION, "CEO", "CEO", "ceo_integrated")
    assert scope.owner_verified is False and scope.internal_admin is False and scope.user_id == ""


class CeoRouteConn:
    def __init__(self):
        self.updates = 0

    async def fetchrow(self, sql, *args):
        if "FROM work_item_change_sets WHERE id" in sql:
            return {"id": OTHER, "project": "AADS", "risk_tier": "A3", "state": "pending",
                    "requested_by": OTHER, "approval_request_id": OTHER}
        if "FROM work_item_change_set_approval_routes" in sql:
            return {"id": OTHER, "required_role": "ceo", "approval_request_id": OTHER}
        self.updates += 1
        return {}

    async def fetchval(self, sql, *args):
        return True

    async def execute(self, sql, *args):
        self.updates += 1


def test_ceo_route_needs_owner_verified_session_not_just_ceo_role():
    ceo = dict(role_key="CEO", workspace_kind="ceo_integrated")
    borrowed = ActorScope(TENANT, SESSION, "CEO", "CEO", "ceo_integrated", user_id="svc", internal_admin=True)
    conn = CeoRouteConn()
    err = raises(decide_change_set(conn, tenant_id=TENANT, change_set_id=GOAL, actor=borrowed, approve=True, reason=""))
    assert err.status_code == 403 and code_of(err) == "ceo_owner_verification_required"
    assert conn.updates == 0
    del ceo


# ── evidence: blank references never count ───────────────────────────────

class NoDbConn:
    async def fetchrow(self, *a):
        raise AssertionError("blank references must be rejected before any query")


def test_blank_approval_refs_are_rejected_before_any_query():
    for ref, message in (("", "m"), ("  ", "m"), ("decision:x", ""), ("decision:x", "  ")):
        err = raises(verify_approval_evidence(
            NoDbConn(), tenant_id=TENANT, goal_id=GOAL, decision_ref=ref, approval_message_id=message))
        assert err.status_code == 400 and code_of(err) == "approval_ref_required"
    err = raises(verify_approval_evidence(
        NoDbConn(), tenant_id=TENANT, goal_id=GOAL, decision_ref="decision:x", approval_message_id="not-a-uuid"))
    assert code_of(err) == "approval_ref_invalid"


def test_endpoints_and_flags_are_wired():
    router = (ROOT / "app/routers/work_items.py").read_text()
    for path in ("/goals/{goal_id}/canonical-assignment/preview", "/goals/{goal_id}/canonical-assignment/proposals",
                 "/work-item-change-sets/{change_set_id}/assignment/decision",
                 "/work-item-change-sets/{change_set_id}/assignment/execute",
                 "/work-item-change-sets/{change_set_id}/assignment/rollback"):
        assert path in router
    assert "_require_m14()" in router.split("post_canonical_assignment_proposal")[1]
    source = (ROOT / "app/services/canonical_hierarchy_assignment.py").read_text()
    for forbidden in ("created_by=", "ANTHROPIC", "SET status", "UPDATE goals"):
        assert forbidden not in source


def test_registration_script_service_key_block_is_unchanged():
    register = (SPEC_DIR / "register_work_hierarchy.py").read_text()
    assert "서비스 principal(AADS_MONITOR_KEY/X-Monitor-Key) 쓰기는 무조건 차단한다" in register


# ── operator script: offline by default, never trusts its own arguments ──

def _load_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("assign_work_hierarchy", SPEC_DIR / "assign_work_hierarchy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_offline_plan_matches_registered_ids():
    plan = _load_script().offline_plan()
    assert plan["role_counts"] == {"PM": 36, "Developer": 30, "QA": 24} and plan["ids_md5"] == PRODUCTION_IDS_MD5


def test_script_refuses_service_principal_blank_refs_and_foreign_base():
    script = _load_script()
    ok = {"AADS_BEARER_TOKEN": "t", "AADS_ACTOR_SESSION_ID": "b749ff17-43d3-4b87-b67f-482b1748b82b"}
    assert script.resolve_auth(ok)[1] is None
    for extra in ({"AADS_MONITOR_KEY": "k"}, {"AADS_USE_SERVICE_PRINCIPAL": "1"}, {"AADS_API_BASE": "https://evil.example/api/v1"}):
        assert script.resolve_auth({**ok, **extra})[0] is None
    assert script.resolve_auth({"AADS_BEARER_TOKEN": "t"})[0] is None
    assert script.resolve_auth({**ok, "AADS_ACTOR_SESSION_ID": ""})[0] is None
    assert script.check_refs("", "29671ea6-0bf9-4155-a411-43cedb470c31")
    assert script.check_refs("decision:x", "  ")
    assert script.check_refs("decision:x", "29671ea6-0bf9-4155-a411-43cedb470c31") is None


# ── PostgreSQL (isolated, disposable) ────────────────────────────────────

DB_URL = os.getenv("CANONICAL_ASSIGN_TEST_DB_URL", "")
pg = pytest.mark.skipif(not DB_URL, reason="CANONICAL_ASSIGN_TEST_DB_URL (disposable canonical_assign_test DB) required")
GOAL8_ROLE = {"PM": 36, "Developer": 30, "QA": 24}


def uid() -> str:
    return str(uuid.uuid4())


async def connect():
    import asyncpg

    assert DB_URL.rstrip("/").endswith("/canonical_assign_test"), "refusing to run outside the disposable DB"
    return await asyncpg.connect(DB_URL)


def run_pg(body):
    """Run ``body(conn, world, helpers)`` against a fresh seeded world."""
    async def main():
        conn = await connect()
        try:
            world = await seed_world(conn)
            return await body(conn, world)
        finally:
            await conn.close()
    return asyncio.run(main())


async def tx(conn, fn, **kwargs):
    async with conn.transaction():
        return await fn(conn, **kwargs)


async def err_of(conn, fn, **kwargs) -> tuple[int, str]:
    try:
        await tx(conn, fn, **kwargs)
    except HTTPException as exc:
        return exc.status_code, code_of(exc)
    raise AssertionError("expected HTTPException")


async def seed_world(conn, *, project="AADS", approval_content="⏸보류 3건 권장안으로 진행해") -> SimpleNamespace:
    w = SimpleNamespace(project=project)
    w.tenant, w.ceo_user, w.other_user = uid(), uid(), uid()
    await conn.execute("INSERT INTO tenants(id,slug,name) VALUES($1,$2,'t')", uuid.UUID(w.tenant), f"t-{w.tenant[:12]}")
    for user in (w.ceo_user, w.other_user):
        await conn.execute("INSERT INTO saas_users(id,email,password_hash) VALUES($1,$2,'x')", user, f"{user}@e.test")
    for user, role in ((w.ceo_user, "owner"), (w.other_user, "member")):
        await conn.execute(
            "INSERT INTO tenant_memberships(tenant_id,user_id,role) VALUES($1,$2,$3)", uuid.UUID(w.tenant), user, role)

    async def workspace(name, key):
        return await conn.fetchval(
            "INSERT INTO chat_workspaces(name,project_key,display_name,tenant_id) VALUES($1::text,$2::text,$1::text,$3) RETURNING id",
            name, key, uuid.UUID(w.tenant))

    async def session(ws, role, user, title="s"):
        return str(await conn.fetchval(
            "INSERT INTO chat_sessions(workspace_id,tenant_id,role_key,user_id,title) VALUES($1,$2,$3,$4,$5) RETURNING id",
            ws, uuid.UUID(w.tenant), role, user, title))

    ceo_ws, aads_ws, kis_ws = await workspace("[CEO] 통합지시", "CEO"), await workspace("[AADS] 프로젝트 매니저", project), await workspace("[KIS] 매니저", "KIS")
    w.ceo = await session(ceo_ws, "CEO", w.ceo_user)
    w.prop = await session(aads_ws, "lead", w.other_user)
    w.pm = await session(aads_ws, "PM", w.other_user, "PM")
    w.dev = await session(aads_ws, "Developer", w.other_user, "Developer")
    w.qa = await session(aads_ws, "QA", w.other_user, "QA")
    w.admin = await session(aads_ws, "GoalSystemAdmin", w.other_user)
    w.kis = await session(kis_ws, "PM", w.other_user)
    w.ceo_ws_other = await session(ceo_ws, "PM", w.ceo_user)
    w.plain = await session(aads_ws, "CEO", w.ceo_user)
    w.goal = uid()
    await conn.execute("INSERT INTO goals(id,title,project,status,tenant_id) VALUES($1,'g',$2,'completed',$3)",
                       uuid.UUID(w.goal), project, uuid.UUID(w.tenant))
    w.message = str(await conn.fetchval(
        """INSERT INTO chat_messages(session_id,role,content,tenant_id,created_at)
           VALUES($1,'user',$2,$3,now()-interval '1 minute') RETURNING id""",
        uuid.UUID(w.ceo), approval_content, uuid.UUID(w.tenant)))
    w.ref = f"decision:test-{w.goal[:8]}"
    w.decision = str(await conn.fetchval(
        """INSERT INTO project_handover_entries(tenant_id,project_key,entry_key,entry_type,title,body,status,
               source_session_id,metadata) VALUES($1,$2,$3,'decision','d','본문','active',$4,$5::jsonb) RETURNING id""",
        uuid.UUID(w.tenant), project, w.ref, uuid.UUID(w.ceo), json.dumps({
            "roles": GOAL8_ROLE, "goal_id": w.goal, "deploy_allowed": False, "approval_message_id": w.message})))
    milestones: dict[str, str] = {}
    ids: dict[str, str] = {}
    for item in SPEC["items"]:
        if item["milestone_id"] not in milestones:
            milestones[item["milestone_id"]] = str(await conn.fetchval(
                "INSERT INTO milestones(goal_id,title,tenant_id,project) VALUES($1,'m',$2,$3) RETURNING id",
                uuid.UUID(w.goal), uuid.UUID(w.tenant), project))
    for level in ("epic", "story", "task"):
        for item in (i for i in SPEC["items"] if i["type"] == level):
            ids[item["ref"]] = str(await conn.fetchval(
                """INSERT INTO work_items(tenant_id,project,goal_id,milestone_id,parent_id,type,title,description,
                       idempotency_key,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                uuid.UUID(w.tenant), project, uuid.UUID(w.goal), uuid.UUID(milestones[item["milestone_id"]]),
                uuid.UUID(ids[item["parent_ref"]]) if item.get("parent_ref") else None, level, item["title"],
                item["description"], item["idempotency_key"], uuid.UUID(w.ceo)))
    w.ids = sorted(ids.values())
    w.ids_md5 = ids_fingerprint(w.ids)
    w.holders = {"PM": w.pm, "Developer": w.dev, "QA": w.qa}
    w.ceo_actor = ActorScope(w.tenant, w.ceo, "CEO", "CEO", "ceo_integrated", user_id=w.ceo_user, owner_verified=True)
    w.borrowed_ceo = ActorScope(w.tenant, w.ceo, "CEO", "CEO", "ceo_integrated", user_id="svc-monitor", internal_admin=True)
    w.prop_actor = ActorScope(w.tenant, w.prop, "lead", project, "project", user_id=w.other_user, owner_verified=True)
    w.svc_actor = ActorScope(w.tenant, w.prop, "lead", project, "project", user_id="svc-monitor", internal_admin=True)
    return w


def propose_kwargs(w, **over):
    return {"tenant_id": w.tenant, "actor": w.prop_actor, "goal_id": w.goal, "decision_ref": w.ref,
            "approval_message_id": w.message, "holders": w.holders, **over}


async def snapshot(conn, w) -> dict:
    rows = await conn.fetch(
        """SELECT id::text, status, version, assignment_id::text AS a, created_by::text AS c
             FROM work_items WHERE goal_id=$1 ORDER BY id""", uuid.UUID(w.goal))
    return {"rows": [tuple(r) for r in rows], "md5": ids_fingerprint([r["id"] for r in rows]),
            "assigned": sum(1 for r in rows if r["a"]), "drafts": sum(1 for r in rows if r["status"] == "draft"),
            "creators": {r["c"] for r in rows}}


async def approved_change_set(conn, w) -> str:
    proposal = await tx(conn, propose_assignment, **propose_kwargs(w))
    cs = proposal["change_set"]["id"]
    await tx(conn, decide_assignment, tenant_id=w.tenant, actor=w.ceo_actor, change_set_id=cs, approve=True, reason="CEO")
    return cs


def exec_kwargs(w, cs, key="exec-1", **over):
    return {"tenant_id": w.tenant, "actor": w.svc_actor, "change_set_id": cs, "decision_ref": w.ref,
            "approval_message_id": w.message, "execution_key": key, **over}


@pg
def test_pg_approved_assignment_applies_only_the_approved_scope_and_audits_both_principals(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        other_goal = uid()
        await conn.execute("INSERT INTO goals(id,title,project,status,tenant_id) VALUES($1,'other',$2,'active',$3)",
                           uuid.UUID(other_goal), w.project, uuid.UUID(w.tenant))
        before = await snapshot(conn, w)
        assert before["md5"] == w.ids_md5 and before["assigned"] == 0 and before["drafts"] == 90
        preview = await tx(conn, preview_assignment, **propose_kwargs(w))
        assert {r: len(v) for r, v in preview["plan"].items()} == GOAL8_ROLE
        proposal = await tx(conn, propose_assignment, **propose_kwargs(w))
        cs = proposal["change_set"]["id"]
        assert proposal["required_routes"] == ["ceo"] and proposal["change_set"]["state"] == "pending"
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs))) == (409, "approval_required")
        assert await snapshot(conn, w) == before
        decided = await tx(conn, decide_assignment, tenant_id=w.tenant, actor=w.ceo_actor, change_set_id=cs,
                           approve=True, reason="CEO 권장안")
        assert decided["state"] == "approved"
        result = await tx(conn, execute_assignment, **exec_kwargs(w, cs))
        assert result["applied"] == 90 and len(result["created_assignment_ids"]) == 3
        after = await snapshot(conn, w)
        assert after["assigned"] == 90 and after["drafts"] == 90 and after["creators"] == before["creators"]
        assert after["md5"] == w.ids_md5 and all(r[2] == 2 for r in after["rows"])
        per_role = await conn.fetch(
            """SELECT a.role_key, a.session_id::text AS sid, count(*) AS n FROM work_items i
                 JOIN project_role_assignments a ON a.id=i.assignment_id WHERE i.goal_id=$1 GROUP BY 1,2""",
            uuid.UUID(w.goal))
        got = {r["role_key"]: (r["sid"], r["n"]) for r in per_role}
        goal8 = w.goal[:8]
        assert got == {f"PM@{goal8}": (w.pm, 36), f"Developer@{goal8}": (w.dev, 30), f"QA@{goal8}": (w.qa, 24)}
        assert await conn.fetchval("SELECT count(*) FROM work_items WHERE goal_id=$1", uuid.UUID(other_goal)) == 0
        assert await conn.fetchval("SELECT count(*) FROM goal_auto_approval_grants WHERE tenant_id=$1", uuid.UUID(w.tenant)) == 0
        events = {r["event_type"]: json.loads(r["payload"]) for r in await conn.fetch(
            "SELECT event_type, payload::text AS payload FROM work_item_events WHERE aggregate_id=$1", uuid.UUID(cs))}
        executed = events["canonical_assignment_executed"]
        assert executed["principal"] == {"user_id": "svc-monitor", "internal_admin": True, "owner_verified": False}
        assert executed["approver_user_id"] == w.ceo_user and executed["approver_session_id"] == w.ceo
        approved = events["change_set_route_approved"]
        assert approved["principal"]["user_id"] == w.ceo_user and approved["principal"]["owner_verified"] is True
        assert events["canonical_assignment_proposed"]["principal"]["user_id"] == w.other_user
        assert await conn.fetchval("SELECT state FROM work_item_change_sets WHERE id=$1", uuid.UUID(cs)) == "executed"
    run_pg(body)


@pg
def test_pg_idempotent_replays_and_conflicting_keys(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        cs = await approved_change_set(conn, w)
        again = await tx(conn, propose_assignment, **propose_kwargs(w))
        assert again["replayed"] and again["change_set"]["id"] == cs
        first = await tx(conn, execute_assignment, **exec_kwargs(w, cs))
        mid = await snapshot(conn, w)
        replay = await tx(conn, execute_assignment, **exec_kwargs(w, cs))
        assert replay["replayed"] and replay["state"] == "executed" and first["applied"] == 90
        assert await snapshot(conn, w) == mid
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs, key="exec-2"))) == (409, "execution_key_conflict")
        late = await tx(conn, propose_assignment, **propose_kwargs(w))
        assert late["replayed"] and late["change_set"]["id"] == cs
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, holders={**w.holders, "QA": w.admin}))) == (409, "idempotency_conflict")
        assert await conn.fetchval(
            "SELECT count(*) FROM work_item_change_sets WHERE tenant_id=$1 AND action='assign'", uuid.UUID(w.tenant)) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM project_role_assignments WHERE tenant_id=$1", uuid.UUID(w.tenant)) == 3
    run_pg(body)


@pg
def test_pg_wrong_actor_cannot_approve_propose_or_execute_as_ceo(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        borrowed_proposer = ActorScope(w.tenant, w.ceo, "CEO", "CEO", "ceo_integrated", user_id="svc-monitor", internal_admin=True)
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, actor=borrowed_proposer))) == (403, "approver_session_impersonation")
        proposal = await tx(conn, propose_assignment, **propose_kwargs(w))
        cs = proposal["change_set"]["id"]
        decide = {"tenant_id": w.tenant, "change_set_id": cs, "approve": True, "reason": "x"}
        assert (await err_of(conn, decide_assignment, actor=w.borrowed_ceo, **decide)) == (403, "ceo_owner_verification_required")
        non_ceo = ActorScope(w.tenant, w.pm, "PM", w.project, "project", user_id=w.other_user, owner_verified=True)
        assert (await err_of(conn, decide_assignment, actor=non_ceo, **decide)) == (403, "ceo_approval_required")
        plain = ActorScope(w.tenant, w.plain, "CEO", w.project, "project", user_id=w.ceo_user, owner_verified=True)
        assert (await err_of(conn, decide_assignment, actor=plain, **decide)) == (403, "ceo_approval_required")
        assert (await err_of(conn, decide_assignment, actor=w.prop_actor, **decide)) == (403, "ceo_approval_required")
        assert await conn.fetchval("SELECT state FROM work_item_change_sets WHERE id=$1", uuid.UUID(cs)) == "pending"
        await tx(conn, decide_assignment, actor=w.ceo_actor, **decide)
        borrowed_executor = ActorScope(w.tenant, w.ceo, "CEO", "CEO", "ceo_integrated", user_id="svc-monitor", internal_admin=True)
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs, actor=borrowed_executor))) == (403, "approver_session_impersonation")
        assert (await err_of(conn, rollback_assignment, tenant_id=w.tenant, actor=w.borrowed_ceo, change_set_id=cs,
                             rollback_key="rb", reason="x")) == (403, "rollback_owner_required")
        assert (await snapshot(conn, w))["assigned"] == 0
    run_pg(body)


@pg
def test_pg_empty_or_foreign_approval_references_never_count(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        for ref, message in (("", w.message), ("  ", w.message), (w.ref, ""), (w.ref, "   ")):
            assert (await err_of(conn, propose_assignment, **propose_kwargs(w, decision_ref=ref, approval_message_id=message))) == (400, "approval_ref_required")
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, decision_ref="decision:nope"))) == (403, "approval_decision_not_found")
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, approval_message_id=uid()))) == (403, "approval_ref_mismatch")
        # internal_admin alone: valid refs, but no owner-verified CEO decision exists.
        proposal = await tx(conn, propose_assignment, **propose_kwargs(w, actor=w.svc_actor))
        cs = proposal["change_set"]["id"]
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs))) == (409, "approval_required")
        await conn.execute("UPDATE work_item_change_set_approval_routes SET state='approved' WHERE change_set_id=$1", uuid.UUID(cs))
        await conn.execute("UPDATE work_item_change_sets SET state='approved' WHERE id=$1", uuid.UUID(cs))
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs))) == (409, "approval_required")
        assert (await snapshot(conn, w))["assigned"] == 0
    run_pg(body)


@pg
def test_pg_other_tenant_project_and_session_are_rejected(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        stranger = ActorScope(uid(), w.prop, "lead", w.project, "project", user_id=w.other_user, owner_verified=True)
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, tenant_id=stranger.tenant_id, actor=stranger))) == (404, "goal_not_found")
        kis_actor = ActorScope(w.tenant, w.kis, "PM", "KIS", "project", user_id=w.other_user, owner_verified=True)
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, actor=kis_actor))) == (403, "project_scope_denied")
        await conn.execute("UPDATE project_handover_entries SET project_key='KIS' WHERE id=$1", uuid.UUID(w.decision))
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w))) == (403, "approval_decision_not_found")
        await conn.execute("UPDATE project_handover_entries SET project_key=$2 WHERE id=$1", uuid.UUID(w.decision), w.project)
        await conn.execute("UPDATE project_handover_entries SET metadata=jsonb_set(metadata,'{goal_id}',to_jsonb($2::text)) WHERE id=$1", uuid.UUID(w.decision), uid())
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w))) == (403, "approval_ref_mismatch")
        await conn.execute("UPDATE project_handover_entries SET metadata=jsonb_set(metadata,'{goal_id}',to_jsonb($2::text)) WHERE id=$1", uuid.UUID(w.decision), w.goal)
        await conn.execute("UPDATE project_handover_entries SET source_session_id=$2 WHERE id=$1", uuid.UUID(w.decision), uuid.UUID(w.plain))
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w))) == (403, "approval_source_not_ceo")
        await conn.execute("UPDATE project_handover_entries SET source_session_id=$2 WHERE id=$1", uuid.UUID(w.decision), uuid.UUID(w.ceo))
        await conn.execute("UPDATE tenant_memberships SET role='admin' WHERE user_id=$1", w.ceo_user)
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w))) == (403, "approval_actor_not_owner")
        await conn.execute("UPDATE tenant_memberships SET role='owner' WHERE user_id=$1", w.ceo_user)
        await conn.execute("UPDATE chat_messages SET session_id=$2 WHERE id=$1", uuid.UUID(w.message), uuid.UUID(w.ceo_ws_other))
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w))) == (403, "approval_message_mismatch")
        await conn.execute("UPDATE chat_messages SET session_id=$2 WHERE id=$1", uuid.UUID(w.message), uuid.UUID(w.ceo))
        await conn.execute("UPDATE chat_messages SET role='assistant' WHERE id=$1", uuid.UUID(w.message))
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w))) == (403, "approval_message_mismatch")
        await conn.execute("UPDATE chat_messages SET role='user' WHERE id=$1", uuid.UUID(w.message))
        proposal = await tx(conn, propose_assignment, **propose_kwargs(w))
        assert proposal["change_set"]["state"] == "pending"
    run_pg(body)


@pg
def test_pg_holder_sessions_must_be_clear_local_role_holders(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        async def code(**holders):
            return await err_of(conn, propose_assignment, **propose_kwargs(w, holders={**w.holders, **holders}))
        assert await code(PM=w.kis) == (403, "project_scope_denied")
        assert await code(PM=w.ceo_ws_other) == (403, "project_scope_denied")
        assert await code(PM=w.ceo) == (403, "holder_session_forbidden")
        assert await code(PM=w.dev, Developer=w.pm) == (403, "holder_role_mismatch")
        assert await code(PM=w.dev, Developer=w.dev) == (400, "holder_duplicate")
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, holders={"PM": w.pm}))) == (400, "holders_incomplete")
        await conn.execute(
            "INSERT INTO project_role_assignments(tenant_id,project,role_key,session_id) VALUES($1,$2,'GoalSystemAdmin',$3)",
            uuid.UUID(w.tenant), w.project, uuid.UUID(w.admin))
        assert await code(PM=w.admin) == (409, "holder_role_conflict")
        await conn.execute(
            "INSERT INTO project_role_assignments(tenant_id,project,role_key,session_id) VALUES($1,$2,$3,$4)",
            uuid.UUID(w.tenant), w.project, f"PM@{w.goal[:8]}", uuid.UUID(w.prop))
        assert await code() == (409, "role_holder_conflict")
        assert (await snapshot(conn, w))["assigned"] == 0
    run_pg(body)


@pg
def test_pg_existing_holder_is_reused_and_never_ended_by_rollback(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        pm_assignment = str(await conn.fetchval(
            "INSERT INTO project_role_assignments(tenant_id,project,role_key,session_id) VALUES($1,$2,'PM',$3) RETURNING id",
            uuid.UUID(w.tenant), w.project, uuid.UUID(w.pm)))
        cs = await approved_change_set(conn, w)
        result = await tx(conn, execute_assignment, **exec_kwargs(w, cs))
        assert result["assignments"]["PM"] == pm_assignment and len(result["created_assignment_ids"]) == 2
        rolled = await tx(conn, rollback_assignment, tenant_id=w.tenant, actor=w.ceo_actor, change_set_id=cs,
                          rollback_key="rb-1", reason="test")
        assert rolled["restored"] == 90 and len(rolled["ended_assignment_ids"]) == 2
        assert await conn.fetchval("SELECT active FROM project_role_assignments WHERE id=$1", uuid.UUID(pm_assignment)) is True
    run_pg(body)


TAMPER_CASES = {
    "wrong_decision_ref": ({"decision_ref": "decision:other"}, None, (403, "approval_ref_mismatch")),
    "wrong_message_ref": ({"approval_message_id": "RANDOM"}, None, (403, "approval_ref_mismatch")),
    "decision_withdrawn": ({}, "UPDATE project_handover_entries SET status='superseded' WHERE id=$D", (409, "approval_withdrawn")),
    "message_deleted": ({}, "UPDATE chat_messages SET deleted_at=now() WHERE id=$M", (409, "approval_message_withdrawn")),
    "message_hidden": ({}, "UPDATE chat_messages SET is_hidden=true WHERE id=$M", (409, "approval_message_withdrawn")),
    "message_edited": ({}, "UPDATE chat_messages SET content='다른 내용' WHERE id=$M", (409, "approval_evidence_changed")),
    "decision_body_edited": ({}, "UPDATE project_handover_entries SET body='변조' WHERE id=$D", (409, "approval_evidence_changed")),
    "decision_revision_bumped": ({}, "UPDATE project_handover_entries SET revision=2 WHERE id=$D", (409, "approval_evidence_changed")),
    "holder_role_changed": ({}, "UPDATE chat_sessions SET role_key='Developer' WHERE id=$PM", (403, "holder_role_mismatch")),
    "item_version_bumped": ({}, "UPDATE work_items SET version=version+1 WHERE id=$I0", (409, "target_set_mismatch")),
    "item_left_draft": ({}, "UPDATE work_items SET status='ready' WHERE id=$I1", (409, "target_set_mismatch")),
    "item_role_changed": ({}, "UPDATE work_items SET description=regexp_replace(description,'권장 역할: [A-Za-z]+',CASE WHEN description LIKE '%권장 역할: QA%' THEN '권장 역할: PM' ELSE '권장 역할: QA' END) WHERE id=$I2", (409, "target_set_mismatch")),
    "patch_rewritten": ({}, "REWRITE", (409, "patch_hash_mismatch")),
}


@pg
@pytest.mark.parametrize("case", sorted(TAMPER_CASES))
def test_pg_withdrawal_and_tampering_block_execution_without_side_effects(monkeypatch, case):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")
    overrides, tamper, expected = TAMPER_CASES[case]

    async def body(conn, w):
        cs = await approved_change_set(conn, w)
        before = await snapshot(conn, w)
        overrides_now = {k: (uid() if v == "RANDOM" else v) for k, v in overrides.items()}
        if tamper == "REWRITE":
            await conn.execute("ALTER TABLE work_item_change_sets DISABLE TRIGGER USER")
            await conn.execute(
                "UPDATE work_item_change_sets SET patch=jsonb_set(patch,'{1,value,count}','99'::jsonb) WHERE id=$1", uuid.UUID(cs))
            await conn.execute("ALTER TABLE work_item_change_sets ENABLE TRIGGER USER")
        elif tamper:
            ids = {"$D": w.decision, "$M": w.message, "$PM": w.pm, "$I0": w.ids[0], "$I1": w.ids[1], "$I2": w.ids[2]}
            statement = tamper
            for token in sorted(ids, key=len, reverse=True):
                statement = statement.replace(token, f"'{ids[token]}'")
            await conn.execute(statement)
        before = await snapshot(conn, w)
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs, **overrides_now))) == expected
        assert await snapshot(conn, w) == before
        assert await conn.fetchval("SELECT count(*) FROM project_role_assignments WHERE tenant_id=$1", uuid.UUID(w.tenant)) == 0
        assert await conn.fetchval("SELECT state FROM work_item_change_sets WHERE id=$1", uuid.UUID(cs)) == "approved"
    run_pg(body)


@pg
def test_pg_extra_item_or_changed_role_after_approval_is_a_target_set_mismatch(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        cs = await approved_change_set(conn, w)
        milestone = await conn.fetchval("SELECT id FROM milestones WHERE goal_id=$1 LIMIT 1", uuid.UUID(w.goal))
        await conn.execute(
            """INSERT INTO work_items(tenant_id,project,goal_id,milestone_id,type,title,description,idempotency_key)
               VALUES($1,$2,$3,$4,'epic','늦게 추가','(권장 역할: PM)','late')""",
            uuid.UUID(w.tenant), w.project, uuid.UUID(w.goal), milestone)
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs))) == (409, "target_set_mismatch")
        await conn.execute("DELETE FROM work_items WHERE idempotency_key='late' AND goal_id=$1", uuid.UUID(w.goal))
        await conn.execute("UPDATE work_items SET description=replace(description,'권장 역할: PM','권장 역할: QA') WHERE id=$1 AND description LIKE '%권장 역할: PM%'",
                           uuid.UUID(w.ids[0]))
        await conn.execute("UPDATE work_items SET description=replace(description,'권장 역할: Developer','권장 역할: PM') WHERE id=$1 AND description LIKE '%권장 역할: Developer%'",
                           uuid.UUID(w.ids[0]))
        assert (await snapshot(conn, w))["assigned"] == 0
    run_pg(body)


@pg
def test_pg_approval_reference_cannot_back_a_second_live_change_set(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        first = await tx(conn, propose_assignment, **propose_kwargs(w))
        await conn.execute("UPDATE project_handover_entries SET revision=2 WHERE id=$1", uuid.UUID(w.decision))
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w))) == (409, "approval_ref_already_used")
        other_goal = uid()
        await conn.execute("INSERT INTO goals(id,title,project,status,tenant_id) VALUES($1,'o',$2,'active',$3)",
                           uuid.UUID(other_goal), w.project, uuid.UUID(w.tenant))
        assert (await err_of(conn, propose_assignment, **propose_kwargs(w, goal_id=other_goal))) == (403, "approval_ref_mismatch")
        assert first["change_set"]["state"] == "pending"
    run_pg(body)


@pg
def test_pg_concurrent_executions_apply_once_and_concurrent_proposals_create_one(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        c1, c2 = await connect(), await connect()
        try:
            proposals = await asyncio.gather(
                tx(c1, propose_assignment, **propose_kwargs(w)), tx(c2, propose_assignment, **propose_kwargs(w)))
            assert len({p["change_set"]["id"] for p in proposals}) == 1
            assert sorted(p["replayed"] for p in proposals) == [False, True]
            cs = proposals[0]["change_set"]["id"]
            await tx(conn, decide_assignment, tenant_id=w.tenant, actor=w.ceo_actor, change_set_id=cs, approve=True, reason="CEO")
            mixed = await asyncio.gather(
                tx(c1, execute_assignment, **exec_kwargs(w, cs, key="A")),
                tx(c2, execute_assignment, **exec_kwargs(w, cs, key="B")), return_exceptions=True)
            wins = [r for r in mixed if isinstance(r, dict)]
            losses = [r for r in mixed if isinstance(r, HTTPException)]
            assert len(wins) == 1 and len(losses) == 1 and code_of(losses[0]) == "execution_key_conflict"
            same = await asyncio.gather(
                tx(c1, execute_assignment, **exec_kwargs(w, cs, key=wins[0]["execution_key"])),
                tx(c2, execute_assignment, **exec_kwargs(w, cs, key=wins[0]["execution_key"])))
            assert all(r["replayed"] for r in same)
            after = await snapshot(conn, w)
            assert after["assigned"] == 90 and all(r[2] == 2 for r in after["rows"])
            assert await conn.fetchval("SELECT count(*) FROM project_role_assignments WHERE tenant_id=$1", uuid.UUID(w.tenant)) == 3
        finally:
            await c1.close()
            await c2.close()
    run_pg(body)


@pg
def test_pg_race_between_item_edit_and_execution_never_overwrites_the_edit(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        cs = await approved_change_set(conn, w)
        editor = await connect()
        try:
            async with editor.transaction():
                await editor.execute("UPDATE work_items SET version=version+1, title='편집' WHERE id=$1", uuid.UUID(w.ids[5]))
                racing = asyncio.ensure_future(tx(conn, execute_assignment, **exec_kwargs(w, cs)))
                await asyncio.sleep(0.5)
                assert not racing.done()
            with pytest.raises(HTTPException) as info:
                await racing
            assert code_of(info.value) == "target_set_mismatch"
        finally:
            await editor.close()
        assert (await snapshot(conn, w))["assigned"] == 0
        assert await conn.fetchval("SELECT title FROM work_items WHERE id=$1", uuid.UUID(w.ids[5])) == "편집"
    run_pg(body)


@pg
def test_pg_rollback_restores_ids_status_creator_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        cs = await approved_change_set(conn, w)
        rb = {"tenant_id": w.tenant, "actor": w.ceo_actor, "change_set_id": cs, "rollback_key": "rb-1", "reason": "되돌림"}
        assert (await err_of(conn, rollback_assignment, **rb)) == (409, "rollback_not_applicable")
        before = await snapshot(conn, w)
        await tx(conn, execute_assignment, **exec_kwargs(w, cs))
        assert (await err_of(conn, rollback_assignment, **{**rb, "actor": w.prop_actor})) == (403, "rollback_owner_required")
        assert (await err_of(conn, rollback_assignment, **{**rb, "reason": " "})) == (400, "rollback_key_and_reason_required")
        rolled = await tx(conn, rollback_assignment, **rb)
        assert rolled["restored"] == 90 and rolled["state"] == "revoked" and len(rolled["ended_assignment_ids"]) == 3
        after = await snapshot(conn, w)
        assert after["md5"] == before["md5"] == w.ids_md5 and after["assigned"] == 0
        assert after["drafts"] == 90 and after["creators"] == before["creators"]
        assert all(a[3] is None and a[1] == "draft" and a[4] == b[4] for a, b in zip(after["rows"], before["rows"], strict=True))
        assert all(a[2] == b[2] + 2 for a, b in zip(after["rows"], before["rows"], strict=True))
        assert await conn.fetchval(
            "SELECT count(*) FROM project_role_assignments WHERE tenant_id=$1 AND active", uuid.UUID(w.tenant)) == 0
        record = await conn.fetchrow(
            "SELECT state, target_type, action FROM work_item_change_sets WHERE id=$1", uuid.UUID(rolled["rollback_change_set_id"]))
        assert tuple(record) == ("executed", "goal", "assign")
        again = await tx(conn, rollback_assignment, **rb)
        assert again["replayed"] and again["rollback_change_set_id"] == rolled["rollback_change_set_id"]
        assert (await err_of(conn, rollback_assignment, **{**rb, "rollback_key": "rb-2"})) == (409, "execution_key_conflict")
        assert (await err_of(conn, execute_assignment, **exec_kwargs(w, cs, key="exec-2"))) == (409, "execution_key_conflict")
        late = await tx(conn, execute_assignment, **exec_kwargs(w, cs))
        assert late["replayed"] and late["state"] == "revoked"
        assert (await snapshot(conn, w))["assigned"] == 0
    run_pg(body)


@pg
def test_pg_rollback_refuses_to_rewind_an_item_that_left_draft(monkeypatch):
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")

    async def body(conn, w):
        cs = await approved_change_set(conn, w)
        await tx(conn, execute_assignment, **exec_kwargs(w, cs))
        await conn.execute("UPDATE work_items SET status='in_progress' WHERE id=$1", uuid.UUID(w.ids[3]))
        rb = {"tenant_id": w.tenant, "actor": w.ceo_actor, "change_set_id": cs, "rollback_key": "rb", "reason": "x"}
        assert (await err_of(conn, rollback_assignment, **rb)) == (409, "rollback_blocked_started")
        assert (await snapshot(conn, w))["assigned"] == 90
        await conn.execute("UPDATE work_items SET status='draft' WHERE id=$1", uuid.UUID(w.ids[3]))
        await conn.execute("UPDATE work_items SET assignment_id=NULL WHERE id=$1", uuid.UUID(w.ids[4]))
        await tx(conn, rollback_assignment, **rb)
        assert (await snapshot(conn, w))["assigned"] == 0
    run_pg(body)


def test_hash_helper_matches_stdlib():
    assert svc._sha256("a") == hashlib.sha256(b"a").hexdigest()
