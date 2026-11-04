"""work_item_change_set_execute 도구 — 현재 턴 lease 로 execute_change_set 호출 (DB 없이 fake conn)."""
from __future__ import annotations

import asyncio

import pytest

TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
CHANGE_SET = "00000000-0000-0000-0000-000000000020"
TARGET = "00000000-0000-0000-0000-000000000010"
SESSION = "00000000-0000-0000-0000-000000000003"
EXECUTION = "00000000-0000-0000-0000-000000000099"


class FakeConn:
    def __init__(self, *, state="approved", cs_tenant=TENANT, session_tenant=TENANT,
                 user_id="user-1", lease_valid=True, project="AADS", turns=None, execution_key=None):
        self.state = state
        self.cs_tenant = cs_tenant
        self.session_tenant = session_tenant
        self.user_id = user_id
        self.lease_valid = lease_valid
        self.project = project
        self.turns = turns
        self.execution_key = execution_key
        self.lease_args = None
        self.writes = []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetchrow(self, sql, *args):
        if "FROM chat_sessions WHERE id" in sql:
            return {"tenant_id": self.session_tenant, "user_id": self.user_id}
        if "FROM work_item_change_sets WHERE id" in sql:
            return {"tenant_id": self.cs_tenant, "state": self.state}
        if "FROM work_item_change_sets c JOIN work_items w" in sql:
            return {
                "id": CHANGE_SET, "tenant_id": self.cs_tenant, "project": self.project,
                "target_project": self.project, "state": self.state, "execution_key": self.execution_key,
                "target_id": TARGET,
            }
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *args):
        if "FROM chat_turn_executions" in sql:
            self.lease_args = args
            self.lease_sql = sql
            if self.turns is None:
                return self.lease_valid
            session_id, instance, epoch = args[:3]
            bound_id = args[3] if "id=$4" in sql else None
            return any(
                t["session_id"] == session_id and t["owner_instance"] == instance
                and t["owner_epoch"] == epoch and t["status"] in ("running", "retrying")
                and (bound_id is None or t["id"] == bound_id)
                for t in self.turns
            )
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        self.writes.append(sql)
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
    from app.services import goal_work_hierarchy as gwh

    async def fake_actor(conn, *, tenant_id, user_id, actor_session_id, internal_admin=False):
        return gwh.ActorScope(
            tenant_id=tenant_id, session_id=actor_session_id, role_key="", project="AADS",
            workspace_kind="project", user_id=user_id, owner_verified=True,
        )

    monkeypatch.setattr(gwh, "resolve_actor_scope", fake_actor)
    return monkeypatch


def _run(conn, inp, monkeypatch, *, session=SESSION, epoch=7, execution=EXECUTION):
    from app.core import db_pool
    from app.services import chat_service as cs
    from app.services import tool_executor as te

    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))
    tokens = [
        (te.current_chat_session_id, te.current_chat_session_id.set(session)),
        (cs._current_execution_id, cs._current_execution_id.set(execution)),
        (cs._current_execution_owner_epoch, cs._current_execution_owner_epoch.set(epoch)),
    ]
    try:
        return asyncio.run(te.ToolExecutor()._work_item_change_set_execute(inp))
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _spy_execute(env):
    from app.services import goal_workflow_approval as gwa

    calls = []

    async def fake_execute(conn, **kwargs):
        calls.append(kwargs)
        return {"change_set_id": kwargs["change_set_id"], "state": "executed",
                "execution_key": kwargs["execution_key"]}

    env.setattr(gwa, "execute_change_set", fake_execute)
    return calls


def test_approved_change_set_is_executed_with_turn_lease(env):
    from app.services import chat_service as cs

    calls = _spy_execute(env)
    conn = FakeConn()
    res = _run(conn, {"change_set_id": CHANGE_SET}, env, epoch=7)
    assert res["ok"] is True
    assert res["state"] == "executed"
    assert len(calls) == 1
    call = calls[0]
    assert call["tenant_id"] == TENANT
    assert call["change_set_id"] == CHANGE_SET
    assert call["owner_instance"] == cs._EXECUTION_OWNER_INSTANCE
    assert call["owner_epoch"] == 7
    assert call["actor"].session_id == SESSION
    assert call["execution_key"] == res["execution_key"]


@pytest.mark.parametrize("state", ["pending", "pending_approval", "rejected", "executed", "revoked", "superseded"])
def test_non_approved_change_set_is_rejected_without_calling_execute(env, state):
    calls = _spy_execute(env)
    conn = FakeConn(state=state)
    res = _run(conn, {"change_set_id": CHANGE_SET}, env)
    assert res["error"] == "change_set_not_approved"
    assert res["state"] == state
    assert calls == []


def test_foreign_tenant_change_set_is_rejected_without_calling_execute(env):
    calls = _spy_execute(env)
    conn = FakeConn(cs_tenant=OTHER_TENANT)
    res = _run(conn, {"change_set_id": CHANGE_SET}, env)
    assert res["error"] == "cannot_execute_foreign_tenant"
    assert calls == []


def test_missing_lease_error_is_passed_through_from_execute_change_set(env):
    """실제 execute_change_set 의 lease 검사 결과를 숨기지 않고 그대로 반환한다."""
    conn = FakeConn(lease_valid=False)
    res = _run(conn, {"change_set_id": CHANGE_SET}, env, epoch=3)
    assert res["error"] == "execution_owner_lease_invalid"
    assert "ok" not in res
    assert conn.lease_args[0] == SESSION and conn.lease_args[2] == 3
    assert conn.writes == []


def test_no_turn_context_never_matches_a_lease(env):
    """턴 컨텍스트(epoch)가 없으면 어떤 lease 와도 맞지 않는 epoch 로 호출되고 오류가 전달된다."""
    from app.services import chat_service as cs

    conn = FakeConn(lease_valid=False)
    res = _run(conn, {"change_set_id": CHANGE_SET}, env, epoch=None, execution=None)
    assert res["error"] == "execution_owner_lease_invalid"
    assert conn.lease_args[1] == cs._EXECUTION_OWNER_INSTANCE
    assert conn.lease_args[2] == -1
    assert conn.writes == []


def _two_turns(instance):
    """같은 세션·같은 owner_instance·같은 epoch=1 인 종료된 턴 A 와 실행 중인 턴 B."""
    turn_a = "00000000-0000-0000-0000-0000000000aa"
    turn_b = "00000000-0000-0000-0000-0000000000bb"
    turns = [
        {"id": turn_a, "session_id": SESSION, "owner_instance": instance, "owner_epoch": 1, "status": "done"},
        {"id": turn_b, "session_id": SESSION, "owner_instance": instance, "owner_epoch": 1, "status": "running"},
    ]
    return turn_a, turn_b, turns


def test_finished_turn_id_cannot_borrow_running_turn_lease(env):
    """종료된 턴 A 의 execution_id 는 실행 중인 턴 B 의 lease 로 통과할 수 없고 쓰기가 0건이다."""
    from app.services import chat_service as cs

    turn_a, _turn_b, turns = _two_turns(cs._EXECUTION_OWNER_INSTANCE)
    conn = FakeConn(turns=turns)
    res = _run(conn, {"change_set_id": CHANGE_SET}, env, epoch=1, execution=turn_a)
    assert res["error"] == "execution_owner_lease_invalid"
    assert conn.lease_args[3] == turn_a
    assert conn.writes == []


def test_running_turn_id_passes_lease_check(env):
    from app.services import chat_service as cs

    _turn_a, turn_b, turns = _two_turns(cs._EXECUTION_OWNER_INSTANCE)
    conn = FakeConn(turns=turns, execution_key=f"chat-tool:{CHANGE_SET}")
    res = _run(conn, {"change_set_id": CHANGE_SET}, env, epoch=1, execution=turn_b)
    assert res.get("error") is None
    assert res["ok"] is True
    assert conn.lease_args[3] == turn_b


def test_execution_id_is_forwarded_to_execute_change_set(env):
    calls = _spy_execute(env)
    res = _run(FakeConn(), {"change_set_id": CHANGE_SET}, env, epoch=7)
    assert res["ok"] is True
    assert calls[0]["execution_id"] == EXECUTION


def test_non_uuid_execution_id_fails_closed(env):
    conn = FakeConn(lease_valid=False)
    res = _run(conn, {"change_set_id": CHANGE_SET}, env, epoch=1, execution="not-a-uuid")
    assert res["error"] == "execution_owner_lease_invalid"
    assert len(conn.lease_args) == 3 and conn.lease_args[2] == -1


def test_execute_change_set_without_execution_id_keeps_legacy_lease_check(env):
    """execution_id 생략(REST 경로) 시 기존 SQL·인자 그대로 epoch 기준으로만 검사한다."""
    from app.services import chat_service as cs
    from app.services import goal_workflow_approval as gwa
    from app.services.goal_work_hierarchy import ActorScope

    turn_a, _turn_b, turns = _two_turns(cs._EXECUTION_OWNER_INSTANCE)
    conn = FakeConn(turns=turns, execution_key="k1")
    actor = ActorScope(
        tenant_id=TENANT, session_id=SESSION, role_key="", project="AADS",
        workspace_kind="project", user_id="user-1", owner_verified=True,
    )
    res = asyncio.run(gwa.execute_change_set(
        conn, tenant_id=TENANT, change_set_id=CHANGE_SET, execution_key="k1",
        owner_instance=cs._EXECUTION_OWNER_INSTANCE, owner_epoch=1, actor=actor,
    ))
    assert res["execution_key"] == "k1"
    assert len(conn.lease_args) == 3
    assert "id=$4" not in conn.lease_sql


def test_http_error_code_from_execute_is_surfaced(env):
    from app.services import goal_workflow_approval as gwa

    async def boom(conn, **kwargs):
        raise gwa._error(409, "patch_hash_mismatch")

    env.setattr(gwa, "execute_change_set", boom)
    res = _run(FakeConn(), {"change_set_id": CHANGE_SET}, env)
    assert res["error"] == "patch_hash_mismatch"


def test_unexpected_exception_is_masked(env):
    from app.services import goal_workflow_approval as gwa

    async def boom(conn, **kwargs):
        raise RuntimeError('relation "work_items" SELECT secret')

    env.setattr(gwa, "execute_change_set", boom)
    res = _run(FakeConn(), {"change_set_id": CHANGE_SET}, env)
    assert res["error"] == "work_item_change_set_execute_failed"
    assert "SELECT" not in res["message"] and "relation" not in res["message"]


def test_input_validation_and_session_binding(env):
    calls = _spy_execute(env)
    conn = FakeConn()
    assert _run(conn, {"change_set_id": CHANGE_SET}, env, session="")["error"] == "execute_session_required"
    assert _run(conn, {"change_set_id": ""}, env)["error"] == "change_set_id_required"
    assert _run(conn, {"change_set_id": "not-a-uuid"}, env)["error"] == "invalid_change_set_id"
    assert _run(FakeConn(user_id=""), {"change_set_id": CHANGE_SET}, env)["error"] == "execute_session_owner_required"
    assert calls == []


def test_input_session_id_is_ignored(env):
    calls = _spy_execute(env)
    res = _run(FakeConn(), {"change_set_id": CHANGE_SET, "session_id": "x"}, env, session="")
    assert res["error"] == "execute_session_required"
    assert calls == []


def test_feature_flag_off_fails_closed(env):
    calls = _spy_execute(env)
    env.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "false")
    res = _run(FakeConn(), {"change_set_id": CHANGE_SET}, env)
    assert res["error"] == "goal_workflow_approval_disabled"
    assert calls == []


def test_tool_is_registered_and_wired():
    from app.services.tool_executor import ToolExecutor
    from app.services.tool_registry import ToolRegistry

    tools = {t["name"]: t for t in ToolRegistry().get_tools("all")}
    schema = tools["work_item_change_set_execute"]["input_schema"]
    assert schema["required"] == ["change_set_id"]
    assert set(schema["properties"]) == {"change_set_id"}
    assert callable(getattr(ToolExecutor, "_work_item_change_set_execute"))


@pytest.mark.parametrize("intent", ["execute", "directive", "code_task"])
def test_tool_exposed_for_general_chat_intents(intent):
    from app.services.tool_registry import ToolRegistry

    tools = ToolRegistry().get_tools_for_intent(intent)
    by_name = {t["name"]: t for t in tools}
    assert "work_item_change_set_execute" in by_name
    assert "defer_loading" not in by_name["work_item_change_set_execute"]
    assert "input_examples" not in by_name["work_item_change_set_execute"]
