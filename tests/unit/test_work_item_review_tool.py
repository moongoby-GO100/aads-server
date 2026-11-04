"""work_item_review 도구 — 담당 본인 인수 금지·프로젝트/테넌트 범위·증거 검사 (DB 없이 fake conn)."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
ITEM = "00000000-0000-0000-0000-000000000010"
ASSIGNEE = "00000000-0000-0000-0000-000000000002"
REVIEWER = "00000000-0000-0000-0000-000000000003"
ASSIGNMENT = "00000000-0000-0000-0000-000000000004"


class FakeConn:
    def __init__(self, *, status="in_review", project="AADS", item_tenant=TENANT,
                 assignment_session=ASSIGNEE, evidence=1, session_project="AADS",
                 session_tenant=TENANT, workspace_name="AADS", user_id="user-1",
                 fail_on_update=None, fail_on_event=None):
        self.item = {
            "id": ITEM, "tenant_id": item_tenant, "project": project, "status": status,
            "assignment_id": ASSIGNMENT, "progress": 50, "version": 1,
        }
        self.assignment_session = assignment_session
        self.evidence = evidence
        self.session_project = session_project
        self.session_tenant = session_tenant
        self.workspace_name = workspace_name
        self.user_id = user_id
        self.fail_on_update = fail_on_update
        self.fail_on_event = fail_on_event
        self.rolled_back = False
        self.db_calls = 0
        self.updates = []
        self.events = []
        self._snapshots = []

    def transaction(self):
        return self

    async def __aenter__(self):
        self._snapshots.append((dict(self.item), list(self.events), list(self.updates)))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        item, events, updates = self._snapshots.pop()
        if exc_type is not None:
            self.rolled_back = True
            self.item, self.events, self.updates = item, events, updates
        return False

    async def fetchrow(self, sql, *args):
        self.db_calls += 1
        if "FROM chat_sessions s" in sql:
            sid = args[0]
            if args[1] != self.session_tenant:
                return None
            return {"session_id": sid, "role_key": "", "project": self.session_project,
                    "workspace_name": self.workspace_name, "display_name": self.workspace_name,
                    "user_id": self.user_id}
        if "FROM chat_sessions WHERE id" in sql:
            return {"tenant_id": self.session_tenant, "user_id": self.user_id}
        if "SELECT tenant_id::text AS tenant_id FROM work_items" in sql:
            return {"tenant_id": self.item["tenant_id"]}
        if "SELECT * FROM work_items" in sql:
            return dict(self.item) if args[1] == self.item["tenant_id"] else None
        if sql.lstrip().startswith("UPDATE work_items"):
            if self.fail_on_update:
                raise self.fail_on_update
            self.updates.append(args)
            self.item["status"] = args[1]
            return dict(self.item)
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *args):
        if "FROM project_role_assignments" in sql:
            return self.assignment_session
        if "work_item_evidence" in sql:
            return self.evidence > 0
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        if "INSERT INTO work_item_events" in sql:
            if self.fail_on_event:
                raise self.fail_on_event
            self.events.append(args)
        return "OK"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOAL_WORK_HIERARCHY_ENABLED", "true")
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")
    from app.services import goal_workflow_approval as gwa

    monkeypatch.setattr(gwa, "rollup_ancestors", _noop_rollup)
    return monkeypatch


async def _noop_rollup(conn, **kwargs):
    return None


def _run(conn, inp, monkeypatch, session=REVIEWER):
    from app.core import db_pool
    from app.services import tool_executor as te

    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))
    token = te.current_chat_session_id.set(session)
    try:
        return asyncio.run(te.ToolExecutor()._work_item_review(inp))
    finally:
        te.current_chat_session_id.reset(token)


def test_assignee_session_is_rejected_and_state_unchanged(env):
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env, session=ASSIGNEE)
    assert res["error"] == "separation_of_duties"
    assert "담당" in res["message"]
    assert conn.updates == []
    assert conn.item["status"] == "in_review"


def test_other_session_with_evidence_completes(env):
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["ok"] is True
    assert res["status"] == "completed"
    assert conn.item["status"] == "completed"


def test_request_changes_with_reason_moves_to_changes_requested(env):
    conn = FakeConn(evidence=0)
    res = _run(conn, {"item_id": ITEM, "decision": "request_changes", "reason": "테스트 보강"}, env)
    assert res["ok"] is True
    assert res["status"] == "changes_requested"


def test_accept_without_evidence_is_rejected(env):
    conn = FakeConn(evidence=0)
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["error"] == "evidence_required"
    assert conn.updates == []


def test_foreign_tenant_is_rejected(env):
    conn = FakeConn(item_tenant=OTHER_TENANT)
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["error"] == "cannot_review_foreign_tenant"
    assert conn.updates == []


def test_inaccessible_project_is_rejected(env):
    conn = FakeConn(project="KIS", session_project="AADS")
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["error"] == "project_scope_denied"
    assert conn.updates == []


def test_integrated_ceo_workspace_may_review_any_project(env):
    conn = FakeConn(project="KIS", session_project="CEO", workspace_name="[CEO] 통합지시")
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["ok"] is True


def test_request_changes_requires_reason(env):
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "request_changes", "reason": "  "}, env)
    assert res["error"] == "reason_required"
    assert conn.updates == []


@pytest.mark.parametrize("status", ["ready", "in_progress", "completed", "changes_requested"])
def test_not_in_review_is_rejected(env, status):
    conn = FakeConn(status=status)
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["error"] == "invalid_review_transition"
    assert conn.updates == []


def test_invalid_decision_and_missing_session(env):
    conn = FakeConn()
    assert _run(conn, {"item_id": ITEM, "decision": "approve"}, env)["error"] == "invalid_decision"
    assert _run(conn, {"item_id": ITEM, "decision": "accept"}, env, session="")["error"] == "review_session_required"


def test_feature_flag_off_fails_closed(env):
    env.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "false")
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["error"] == "goal_workflow_approval_disabled"
    assert conn.updates == []


def test_input_session_id_cannot_impersonate_reviewer(env):
    """contextvar 가 비면 입력 session_id 만으로는 검토할 수 없다 (separation_of_duties 우회 차단)."""
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept", "session_id": REVIEWER}, env, session="")
    assert res["error"] == "review_session_required"
    assert conn.db_calls == 0
    assert conn.updates == []


def test_input_session_id_is_ignored_when_bound_session_exists(env):
    """바인딩 세션이 담당자면 입력으로 다른 세션을 넣어도 담당 본인 인수가 막힌다."""
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept", "session_id": REVIEWER}, env, session=ASSIGNEE)
    assert res["error"] == "separation_of_duties"
    assert conn.updates == []


def test_sdk_global_session_is_never_used_as_reviewer(env):
    """contextvar 가 비어 있으면 SDK 프로세스 전역 세션이 있어도 거절한다."""
    from app.services import agent_sdk_service

    env.setattr(agent_sdk_service, "_active_chat_session_id", REVIEWER)
    assert agent_sdk_service.get_active_chat_session_id() == REVIEWER
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env, session="")
    assert res["error"] == "review_session_required"
    assert conn.db_calls == 0
    assert conn.updates == [] and conn.events == []


def test_contextvar_session_wins_over_sdk_global_session(env):
    """contextvar 세션만 검토자로 쓰인다 — SDK 전역 세션이 담당자여도 영향 없다."""
    from app.services import agent_sdk_service

    env.setattr(agent_sdk_service, "_active_chat_session_id", ASSIGNEE)
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env, session=REVIEWER)
    assert res["ok"] is True
    assert conn.events[0][5] == REVIEWER


def _payload(event):
    return json.loads(event[7])


def test_accept_records_one_event_with_reviewer_and_reason(env):
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept", "reason": "증거 확인 완료"}, env)
    assert res["ok"] is True
    assert len(conn.events) == 1
    ev = conn.events[0]
    assert ev[0] == TENANT and ev[1] == "AADS" and ev[2] == "work_item" and ev[3] == ITEM
    assert ev[4] == "work_item_accepted"
    assert ev[5] == REVIEWER
    body = _payload(ev)
    assert body["decision"] == "accept"
    assert body["reason"] == "증거 확인 완료"
    assert body["from_status"] == "in_review" and body["to_status"] == "completed"
    assert body["work_item_version"] == 1


def test_request_changes_records_one_event_with_reason(env):
    conn = FakeConn(evidence=0)
    res = _run(conn, {"item_id": ITEM, "decision": "request_changes", "reason": "테스트 보강"}, env)
    assert res["ok"] is True
    assert len(conn.events) == 1
    ev = conn.events[0]
    assert ev[4] == "work_item_changes_requested"
    assert ev[5] == REVIEWER
    body = _payload(ev)
    assert body["decision"] == "request_changes"
    assert body["reason"] == "테스트 보강"
    assert body["from_status"] == "in_review" and body["to_status"] == "changes_requested"


def test_rejected_review_records_no_event(env):
    conn = FakeConn()
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env, session=ASSIGNEE)
    assert res["error"] == "separation_of_duties"
    assert conn.events == []


@pytest.mark.parametrize("decision,reason", [("accept", ""), ("request_changes", "보완 필요")])
def test_event_insert_failure_rolls_back_status(env, decision, reason):
    conn = FakeConn(fail_on_event=RuntimeError("insert into work_item_events failed"))
    res = _run(conn, {"item_id": ITEM, "decision": decision, "reason": reason}, env)
    assert res["error"] == "work_item_review_failed"
    assert conn.rolled_back is True
    assert conn.item["status"] == "in_review"
    assert conn.events == []


def test_router_accept_records_event(env):
    """라우터 /accept 경로도 review_item 안에서 같은 이벤트를 남긴다."""
    from app.core import db_pool
    from app.routers import work_items as router
    from app.services.goal_work_hierarchy import ActorScope

    conn = FakeConn()
    env.setattr(db_pool, "get_pool", lambda: FakePool(conn))

    async def fake_actor(c, context, session_id):
        return ActorScope(tenant_id=TENANT, session_id=REVIEWER, role_key="reviewer", project="AADS",
                          workspace_kind="project")

    env.setattr(router, "_actor", fake_actor)
    context = {"tenant": {"id": TENANT}, "user": {"user_id": "user-1"}}
    asyncio.run(router.post_accept(ITEM, router.ReviewRequest(reason="LGTM"), context, REVIEWER))
    assert len(conn.events) == 1
    assert conn.events[0][4] == "work_item_accepted"
    assert conn.events[0][5] == REVIEWER and conn.events[0][6] == "reviewer"
    assert _payload(conn.events[0])["reason"] == "LGTM"


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", "'; DROP TABLE work_items;--"])
def test_invalid_item_id_is_rejected_before_db(env, bad_id):
    conn = FakeConn()
    res = _run(conn, {"item_id": bad_id, "decision": "accept"}, env)
    assert res["error"] == "invalid_item_id"
    assert "UUID" in res["message"]
    assert conn.db_calls == 0


def test_db_exception_is_masked_and_rolled_back(env):
    conn = FakeConn(fail_on_update=RuntimeError('relation "work_items" SELECT secret FROM x'))
    res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
    assert res["error"] == "work_item_review_failed"
    assert "SELECT" not in res["message"] and "relation" not in res["message"]
    assert "Traceback" not in str(res)
    assert conn.rolled_back is True


def test_session_without_user_id_is_rejected(env):
    """user_id 가 NULL 인 세션은 resolve_actor_scope('') 가 소유자 검사를 통과해 버리므로 거절한다."""
    for user_id in (None, ""):
        conn = FakeConn(user_id=user_id)
        res = _run(conn, {"item_id": ITEM, "decision": "accept"}, env)
        assert res["error"] == "review_session_owner_required"
        assert conn.updates == []


def test_tool_is_registered_and_wired():
    from app.services.tool_executor import ToolExecutor
    from app.services.tool_registry import ToolRegistry

    tools = {t["name"]: t for t in ToolRegistry().get_tools("all")}
    schema = tools["work_item_review"]["input_schema"]
    assert schema["required"] == ["item_id", "decision"]
    assert "session_id" not in schema["properties"]
    assert callable(getattr(ToolExecutor, "_work_item_review"))


def test_router_accept_denies_inaccessible_project(env):
    """라우터 /accept 도 review_item 의 프로젝트 범위 검사를 탄다."""
    from app.core import db_pool
    from app.routers import work_items as router
    from app.services.goal_work_hierarchy import ActorScope

    conn = FakeConn(project="KIS")
    env.setattr(db_pool, "get_pool", lambda: FakePool(conn))

    async def fake_actor(c, context, session_id):
        return ActorScope(tenant_id=TENANT, session_id=REVIEWER, role_key="", project="AADS",
                          workspace_kind="project")

    env.setattr(router, "_actor", fake_actor)
    context = {"tenant": {"id": TENANT}, "user": {"user_id": "user-1"}}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.post_accept(ITEM, router.ReviewRequest(), context, REVIEWER))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "project_scope_denied"
    assert conn.updates == []
