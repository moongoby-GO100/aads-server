"""목표 주도담당 확정(resolver) 규칙, 등록 경로 차단/원자성, 보정 계획 테스트."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import goal_owner_resolver as resolver
from app.services.goal_registration import register_goal, validation_errors
from tests.unit.test_goal_registration import MemoryConnection, payload

TENANT_A, TENANT_B = str(uuid4()), str(uuid4())


class FakeDB:
    """resolver 가 쓰는 네 가지 SQL 만 흉내 낸다. 쿼리 상수로 분기해 SQL 문자열 변경에 안 흔들린다."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.assignments: list[dict] = []

    def session(self, tenant, project, role="", title="t") -> str:
        sid = str(uuid4())
        self.sessions[sid] = {"tenant": tenant, "project_key": project, "role_key": role, "title": title}
        return sid

    def assign(self, tenant, project, role, sid, active=True) -> None:
        self.assignments.append(
            {"tenant": tenant, "project": project, "role": role, "sid": sid, "active": active}
        )

    def _row(self, sid):
        s = self.sessions[sid]
        return {"session_id": sid, "role_key": s["role_key"], "title": s["title"],
                "project_key": s["project_key"]}

    async def fetchrow(self, sql, *a):
        assert sql == resolver._SESSION_SQL, sql
        sid, tenant = a
        s = self.sessions.get(sid)
        return self._row(sid) if s and s["tenant"] == tenant else None

    async def fetch(self, sql, *a):
        tenant, project = a[0], a[1]
        if sql == resolver._ASSIGNMENT_SQL:
            role = a[2]
            return [
                {**self._row(x["sid"]), "role_key": x["role"]}
                for x in self.assignments
                if x["tenant"] == tenant and x["project"].upper() == project.upper() and x["active"]
                and (role is None or x["role"] == role) and x["sid"] in self.sessions
                and self.sessions[x["sid"]]["tenant"] == tenant
            ]
        assert sql == resolver._WORKSPACE_SESSIONS_SQL, sql
        return [
            self._row(sid) for sid, s in self.sessions.items()
            if s["tenant"] == tenant and s["project_key"].upper() == project.upper() and s["role_key"]
        ]


@pytest.fixture
def db():
    return FakeDB()


async def resolve(db, **kw):
    kw.setdefault("tenant_id", TENANT_A)
    kw.setdefault("project", "AADS")
    return await resolver.resolve_goal_owner(db, **kw)


@pytest.mark.asyncio
async def test_explicit_session_same_project_confirms_session_role(db):
    sid = db.session(TENANT_A, "AADS", "dev")
    res = await resolve(db, owner_session_id=sid)
    assert (res.ok, res.owner_role_key, res.owner_session_id, res.source) == (
        True, "dev", sid, "explicit_session")


@pytest.mark.asyncio
async def test_explicit_session_without_role_uses_assignment_or_explicit_role(db):
    sid = db.session(TENANT_A, "AADS", "")
    assert (await resolve(db, owner_session_id=sid)).error == resolver.OWNER_REQUIRED
    assert (await resolve(db, owner_session_id=sid, owner_role_key="qa")).owner_role_key == "qa"
    db.assign(TENANT_A, "AADS", "dev", sid)
    assert (await resolve(db, owner_session_id=sid)).owner_role_key == "dev"


@pytest.mark.asyncio
async def test_explicit_session_other_tenant_is_not_found(db):
    sid = db.session(TENANT_B, "AADS", "dev")
    res = await resolve(db, owner_session_id=sid)
    assert res.error == "owner_session_not_found" and res.owner_session_id is None


@pytest.mark.asyncio
async def test_explicit_session_other_project_denied(db):
    sid = db.session(TENANT_A, "GO100", "dev")
    assert (await resolve(db, owner_session_id=sid)).error == "cross_project_owner_denied"


@pytest.mark.asyncio
async def test_ceo_integrated_session_cannot_lead_project_goal(db):
    sid = db.session(TENANT_A, "CEO", "ceo")
    assert (await resolve(db, owner_session_id=sid)).error == "cross_project_owner_denied"
    assert (await resolve(db, creator_session_id=sid)).error == resolver.OWNER_REQUIRED


@pytest.mark.asyncio
async def test_ceo_project_goal_may_be_led_by_ceo_workspace_session(db):
    sid = db.session(TENANT_A, "CEO", "ceo")
    res = await resolve(db, project="ceo", owner_session_id=sid)
    assert res.ok and res.owner_session_id == sid


@pytest.mark.asyncio
async def test_explicit_role_conflicting_with_session_role_rejected(db):
    sid = db.session(TENANT_A, "AADS", "dev")
    assert (await resolve(db, owner_session_id=sid, owner_role_key="qa")).error == "owner_role_mismatch"


@pytest.mark.asyncio
async def test_role_only_resolves_unique_active_assignment(db):
    sid = db.session(TENANT_A, "AADS", "dev")
    db.assign(TENANT_A, "AADS", "dev", sid)
    res = await resolve(db, owner_role_key=" dev ")
    assert (res.ok, res.owner_session_id, res.source) == (True, sid, "role_assignment")


@pytest.mark.asyncio
async def test_role_only_ignores_inactive_other_project_and_other_tenant_assignments(db):
    old = db.session(TENANT_A, "AADS", "")
    db.assign(TENANT_A, "AADS", "dev", old, active=False)
    other_proj = db.session(TENANT_A, "GO100", "dev")
    db.assign(TENANT_A, "GO100", "dev", other_proj)
    other_tenant = db.session(TENANT_B, "AADS", "dev")
    db.assign(TENANT_B, "AADS", "dev", other_tenant)
    res = await resolve(db, owner_role_key="dev")
    assert res.owner_session_id is None
    assert res.error == resolver.OWNER_REQUIRED
    assert res.reason == "no_active_assignment_for_role"
    assert res.candidates == []


@pytest.mark.asyncio
async def test_role_only_prefers_creator_session_holding_that_role(db):
    creator = db.session(TENANT_A, "AADS", "dev")
    other = db.session(TENANT_A, "AADS", "dev")
    db.assign(TENANT_A, "AADS", "dev", other)
    res = await resolve(db, owner_role_key="dev", creator_session_id=creator)
    assert (res.owner_session_id, res.source) == (creator, "creator_session")


@pytest.mark.asyncio
async def test_creator_in_project_becomes_owner_with_its_role(db):
    creator = db.session(TENANT_A, "AADS", "dev")
    res = await resolve(db, creator_session_id=creator)
    assert (res.owner_role_key, res.owner_session_id, res.source) == ("dev", creator, "creator_session")


@pytest.mark.asyncio
async def test_creator_without_role_falls_back_to_assignment_then_required(db):
    creator = db.session(TENANT_A, "AADS", "")
    assert (await resolve(db, creator_session_id=creator)).reason == "creator_session_has_no_role"
    db.assign(TENANT_A, "AADS", "pm", creator)
    assert (await resolve(db, creator_session_id=creator)).owner_role_key == "pm"


@pytest.mark.asyncio
async def test_creator_from_other_tenant_is_ignored(db):
    creator = db.session(TENANT_B, "AADS", "dev")
    res = await resolve(db, creator_session_id=creator)
    assert res.error == resolver.OWNER_REQUIRED and res.owner_session_id is None


@pytest.mark.asyncio
async def test_no_hint_returns_candidates_assignments_first_and_scoped(db):
    a = db.session(TENANT_A, "AADS", "dev")
    db.assign(TENANT_A, "AADS", "dev", a)
    b = db.session(TENANT_A, "AADS", "qa")
    db.session(TENANT_A, "GO100", "dev")
    db.session(TENANT_B, "AADS", "dev")
    db.session(TENANT_A, "CEO", "ceo")
    res = await resolve(db)
    assert res.error == resolver.OWNER_REQUIRED
    assert [(c["session_id"], c["source"]) for c in res.candidates] == [
        (a, "assignment"), (b, "workspace_session")]
    err = res.as_error()
    assert err["error"] == "owner_required" and len(err["candidates"]) == 2


@pytest.mark.asyncio
async def test_ambiguous_role_assignment_is_not_guessed(db):
    class Dup(FakeDB):
        async def fetch(self, sql, *a):
            rows = await super().fetch(sql, *a)
            return rows + rows if sql == resolver._ASSIGNMENT_SQL and a[2] else rows

    d = Dup()
    sid = d.session(TENANT_A, "AADS", "dev")
    d.assign(TENANT_A, "AADS", "dev", sid)
    res = await resolve(d, owner_role_key="dev")
    assert res.error == resolver.OWNER_REQUIRED and res.reason == "ambiguous_role_assignment"


# ── register_goal: 차단 / 원자성 ────────────────────────────────────────────

class OwnerlessConnection(MemoryConnection):
    """등록 경로 + resolver SQL. creator 세션의 project/role 을 지정할 수 있다."""

    def __init__(self, project_key="AADS", role_key="dev", assignments=(), **kw):
        super().__init__(**kw)
        self.project_key, self.role_key, self.assignments = project_key, role_key, list(assignments)

    async def fetchrow(self, sql, *a):
        if sql == resolver._SESSION_SQL:
            return {"session_id": a[0], "role_key": self.role_key, "title": "t",
                    "project_key": self.project_key}
        return await super().fetchrow(sql, *a)

    async def fetch(self, sql, *a):
        if sql == resolver._ASSIGNMENT_SQL:
            return [r for r in self.assignments if a[2] is None or r["role_key"] == a[2]]
        return []


@pytest.mark.asyncio
async def test_register_blocks_when_owner_unconfirmable_and_writes_nothing():
    from tests.unit.test_goal_registration import SESSION, TENANT

    conn = OwnerlessConnection(project_key="CEO", role_key="ceo")
    res = await register_goal(conn, payload(), tenant_id=TENANT, session_id=SESSION)
    assert res["error"] == "owner_required" and "candidates" in res
    assert not conn.goals and not conn.milestones and not conn.items and not conn.links


@pytest.mark.asyncio
async def test_register_ceo_creator_with_explicit_role_uses_project_assignment():
    from tests.unit.test_goal_registration import SESSION, TENANT

    owner_sid = str(uuid4())
    conn = OwnerlessConnection(
        project_key="CEO", role_key="ceo",
        assignments=[{"session_id": owner_sid, "role_key": "dev", "title": "t", "project_key": "AADS"}],
    )
    p = payload()
    p["goal"]["owner_role_key"] = "dev"
    rejected = await register_goal(conn, p, tenant_id=TENANT, session_id=SESSION)
    assert rejected["error"] == "foreign_owner_session_requires_confirmation"
    assert not conn.goals
    res = await register_goal(
        conn, p, tenant_id=TENANT, session_id=SESSION, confirm_foreign_owner_session=True,
    )
    assert owner_sid in res["warning"]
    goal = conn.goals[res["goal_id"]]
    assert (goal["owner_role_key"], goal["owner_session_id"]) == ("dev", owner_sid)
    assert all(ms["owner_session_id"] == owner_sid for ms in conn.milestones.values())
    assert conn.links[0][3:] == ("chat_session", owner_sid)


@pytest.mark.asyncio
async def test_register_owner_is_never_null_on_success():
    from tests.unit.test_goal_registration import SESSION, TENANT

    conn = OwnerlessConnection()
    res = await register_goal(conn, payload(), tenant_id=TENANT, session_id=SESSION)
    goal = conn.goals[res["goal_id"]]
    assert goal["owner_role_key"] and goal["owner_session_id"]


@pytest.mark.asyncio
async def test_register_failure_after_owner_confirmed_leaves_no_goal_row():
    from tests.unit.test_goal_registration import SESSION, TENANT

    conn = OwnerlessConnection(fail_at=2)
    with pytest.raises(RuntimeError, match="injected"):
        await register_goal(conn, payload(), tenant_id=TENANT, session_id=SESSION)
    assert not conn.goals and not conn.milestones and not conn.links


def test_owner_role_key_must_be_string():
    p = payload()
    p["goal"]["owner_role_key"] = 7
    assert any("owner_role_key" in e for e in validation_errors(p))


# ── REST: 422 + 후보 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rest_create_goal_maps_owner_required_to_422(monkeypatch):
    from contextlib import asynccontextmanager

    from app.core import db_pool
    from app.routers import goals as router
    from app.services import goal_registration

    seen = {}

    async def fake_register(conn, inp, **kw):
        seen.update(kw, role=inp["goal"]["owner_role_key"])
        return {"error": "owner_required", "candidates": [{"session_id": "s", "role_key": "dev"}]}

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield object()

    monkeypatch.setattr(db_pool, "get_pool", lambda: Pool())
    monkeypatch.setattr(goal_registration, "register_goal", fake_register)
    monkeypatch.setattr(goal_registration, "validation_errors", lambda inp: [])
    req = router.GoalCreateRequest(
        project="AADS", title="목표", description="d", success_criteria="c",
        milestones=[], owner_role_key="dev",
    )
    ctx = {"tenant": {"id": TENANT_A}, "user": {"user_id": "u"}}
    with pytest.raises(HTTPException) as exc:
        await router.create_goal(req, ctx)
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "owner_required" and exc.value.detail["candidates"]
    assert seen["owner_session_id"] is None and seen["role"] == "dev"
    assert "session_id" not in seen


# ── 보정 계획 ───────────────────────────────────────────────────────────────

def goal_row(project="AADS", role="", session=None, tenant=TENANT_A, status="active"):
    return {"goal_id": str(uuid4()), "tenant_id": tenant, "project": project, "status": status,
            "title": "목표", "owner_role_key": role, "owner_session_id": session}


@pytest.mark.asyncio
async def test_plan_fixes_only_when_single_assignment_confirms(db):
    sid = db.session(TENANT_A, "AADS", "dev")
    db.assign(TENANT_A, "AADS", "dev", sid)
    plan = await resolver.plan_owner_fix(db, goal_row())
    assert plan["fix"] == {"owner_role_key": "dev", "owner_session_id": sid}


@pytest.mark.asyncio
async def test_plan_reports_ambiguous_when_other_candidates_exist(db):
    sid = db.session(TENANT_A, "AADS", "dev")
    db.assign(TENANT_A, "AADS", "dev", sid)
    db.session(TENANT_A, "AADS", "qa")
    plan = await resolver.plan_owner_fix(db, goal_row())
    assert plan["fix"] is None and len(plan["candidates"]) == 2


@pytest.mark.asyncio
async def test_plan_reports_ambiguous_when_no_candidates(db):
    plan = await resolver.plan_owner_fix(db, goal_row(project="FOOD"))
    assert plan["fix"] is None and plan["candidates"] == []


@pytest.mark.asyncio
async def test_plan_partial_goal_is_completed_without_overwrite(db):
    sid = db.session(TENANT_A, "AADS", "dev")
    db.assign(TENANT_A, "AADS", "dev", sid)
    no_session = await resolver.plan_owner_fix(db, goal_row(role="dev"))
    assert no_session["fix"]["owner_session_id"] == sid
    no_role = await resolver.plan_owner_fix(db, goal_row(session=sid))
    assert no_role["fix"]["owner_role_key"] == "dev"
    foreign = db.session(TENANT_A, "GO100", "dev")
    bad = await resolver.plan_owner_fix(db, goal_row(role="dev", session=foreign))
    assert bad["fix"] is None


@pytest.mark.asyncio
async def test_apply_writes_only_fixable_plans_and_guards_missing_columns():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "goal_owner_backfill", Path(__file__).parents[2] / "scripts/goal_owner_backfill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = []

    class Conn:
        def transaction(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def cm():
                yield

            return cm()

        async def execute(self, sql, *a):
            calls.append((sql, a))
            return "UPDATE 1"

    plans = [
        {"goal_id": "g1", "tenant_id": TENANT_A, "project": "AADS", "status": "active",
         "fix": {"owner_role_key": "dev", "owner_session_id": "s1"}},
        {"goal_id": "g2", "tenant_id": TENANT_A, "project": "AADS", "status": "active",
         "fix": None, "reason": "no_owner_hint", "candidates": []},
    ]
    assert await mod.apply_plans(Conn(), plans, ("active", "draft")) == 1
    assert len(calls) == 1 and calls[0][1][0] == "g1"
    assert "updated_at" not in calls[0][0]
    assert "COALESCE(owner_role_key, '') = ''" in calls[0][0]
    text = mod.render(plans, apply=False)
    assert "DRY-RUN" in text and "fixable=1" in text and "ambiguous=1" in text
