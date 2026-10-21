"""채팅 '계획만'(response_mode='plan') 모드 — 서버 실행 차단과 전송 경로 보존.

프롬프트가 아니라 도구 실행 직전의 코드 게이트가 막는지, 그리고 plan 이 전송·재시도·
재생성·재개 경로에서 quality 로 바뀌지 않는지 검사한다. 유료 모델과 실제 파괴 도구는
쓰지 않는다 — 실행기는 호출 횟수를 세는 스텁이다.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import chat_service, plan_mode_guard
from app.services.plan_mode_guard import PLAN_MODE_BLOCK_MESSAGE

EXPECTED_MESSAGE = "계획만 모드이므로 실행하지 않았습니다. 실행하려면 상단 모드를 변경하십시오"


@pytest.fixture
def plan_mode():
    token = plan_mode_guard.set_response_mode("plan")
    yield
    plan_mode_guard.reset_response_mode(token)


# ── 1. 정규화·프롬프트 ────────────────────────────────────────────────────────

def test_block_message_is_exact():
    assert PLAN_MODE_BLOCK_MESSAGE == EXPECTED_MESSAGE


def test_plan_is_a_first_class_mode():
    assert chat_service._normalize_response_mode("plan") == "plan"
    assert chat_service._normalize_response_mode(" PLAN ") == "plan"
    assert chat_service._normalize_response_mode("fast") == "fast"
    assert chat_service._normalize_response_mode("quality") == "quality"
    assert chat_service._normalize_response_mode(None) == "quality"
    assert chat_service._normalize_response_mode("planning") == "quality"


def test_plan_prompt_block_forbids_execution_and_differs_from_other_modes():
    block = chat_service._response_mode_prompt_block("plan")
    assert "응답 모드: plan" in block
    assert "실행하지 않는다" in block
    assert "응답 모드: quality" not in block
    assert "응답 모드: fast" not in block
    assert "중간 보고로 끝내지 말고 가능한 확인, 조치, 검증을 이어서 수행한다" not in block


def test_plan_does_not_enforce_completion_contract_or_auto_continue():
    assert chat_service._should_enforce_completion_contract("plan") is False
    assert chat_service._should_enforce_completion_contract("fast") is False
    assert chat_service._should_enforce_completion_contract("quality") is True


def test_fast_to_quality_promotion_never_touches_plan():
    src = inspect.getsource(chat_service.send_message_stream)
    assert "response_mode == _RESPONSE_MODE_FAST and _requires_quality_response_mode" in src
    assert 'response_mode = _RESPONSE_MODE_QUALITY' in src
    # 승격 조건은 모두 fast 한정이므로 plan 은 어느 경로에서도 quality 로 바뀌지 않는다.
    assert src.count("response_mode = _RESPONSE_MODE_QUALITY") == src.count(
        "response_mode == _RESPONSE_MODE_FAST and _requires_quality_response_mode"
    )


def test_plan_skips_runner_prompt_and_execution_branches():
    src = inspect.getsource(chat_service.send_message_stream)
    assert 'if response_mode == _RESPONSE_MODE_PLAN else _runner_fast_path_prompt_block()' in src
    assert src.count("response_mode != _RESPONSE_MODE_PLAN") >= 4


# ── 2. 읽기 전용 허용 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", [
    "health_check", "web_search", "handover_search", "canonical_document_lookup",
    "task_history", "pipeline_runner_status", "check_task_status", "search_logs",
    "semantic_code_search", "aag_brief", "read_task_logs",
])
def test_read_only_tools_pass_in_plan_mode(plan_mode, tool):
    assert plan_mode_guard.check(tool, {}) is None


# ── 3. 차단 ───────────────────────────────────────────────────────────────────

BLOCKED_TOOLS = [
    "write_remote_file", "patch_remote_file", "todo_write", "goal_register", "handover_write",
    "canonical_document_register", "directive_create", "deploy_safe", "db_safe_write",
    "notify_channel", "send_telegram", "git_remote_commit", "git_remote_push",
    "git_remote_add", "git_remote_create_branch",
    "pipeline_runner_submit", "pipeline_runner_submit_batch", "pipeline_runner_approve",
    "pipeline_c_start", "ask_session", "delegate_to_agent", "spawn_subagent",
    "spawn_parallel_subagents", "schedule_task", "unschedule_task", "export_data",
    "execute_sandbox", "code_sandbox", "browser_navigate", "smart_browser",
    "generate_image", "pc_execute", "device_command", "device_execute",
    "propose_next_steps", "save_note", "learn_pattern", "credential_register",
    "google_sheets_write_records", "terminate_task", "mockup_review_submit",
]


@pytest.mark.parametrize("tool", BLOCKED_TOOLS)
def test_mutating_tools_are_blocked(plan_mode, tool):
    out = plan_mode_guard.check(tool, {"path": "/x", "content": "y"})
    assert out is not None
    payload = json.loads(out)
    assert payload["error"] == "plan_mode_blocked"
    assert payload["blocked"] is True
    assert payload["message"] == EXPECTED_MESSAGE
    assert payload["tool"] == tool


def test_unknown_tool_is_blocked_by_default(plan_mode):
    out = plan_mode_guard.check("brand_new_tool_added_next_month", {})
    assert json.loads(out)["reason_code"] == "tool_not_allowlisted"


SQL_TOOLS = ("query_database", "query_db", "query_project_database")
SQL_PAYLOADS = [
    "SELECT 1",
    "select id from chat_sessions limit 5;",
    "WITH a AS (SELECT 1) SELECT * FROM a",
    "EXPLAIN SELECT * FROM chat_messages",
    "SHOW server_version",
    "SELECT pg_logical_emit_message(true, 'p', 'x')",
    "SELECT pg_create_restore_point('x')",
    "SELECT public.some_user_defined_writer()",
    "SELECT pg_terminate_backend(1)",
    "DELETE FROM chat_messages",
    "",
]


@pytest.mark.parametrize("tool", SQL_TOOLS)
@pytest.mark.parametrize("field", ["sql", "query"])
@pytest.mark.parametrize("sql", SQL_PAYLOADS)
def test_sql_tools_are_blocked_whatever_the_statement(plan_mode, tool, field, sql):
    """SELECT 로 시작해도 쓰기 함수가 가능하므로 SQL 도구는 문장 검사 없이 통째로 차단한다."""
    out = plan_mode_guard.check(tool, {field: sql, "project": "KIS"})
    assert out is not None
    assert json.loads(out)["reason_code"] == "tool_not_allowlisted"


@pytest.mark.parametrize("command", [
    "rm -rf /tmp/x", "ls -la /root/aads", "cat app/main.py", "git status", "date +%s", "",
])
def test_shell_tool_is_blocked_even_for_read_looking_commands(plan_mode, command):
    out = plan_mode_guard.check("run_remote_command", {"project": "AADS", "command": command})
    assert out is not None
    assert json.loads(out)["reason_code"] == "tool_not_allowlisted"


@pytest.mark.parametrize("tool", [
    "read_remote_file", "list_remote_dir", "git_remote_status", "inspect_service",
    "analyze_changes", "code_explorer", "search_all_projects", "read_github_file",
    "read_uploaded_file", "fetch_url", "jina_read", "crawl4ai_fetch", "deep_crawl",
    "deep_research", "fact_check", "search_crawl_match", "google_sheets_read",
    "list_project_databases",
])
def test_url_path_and_composite_lookups_are_excluded(plan_mode, tool):
    out = plan_mode_guard.check(tool, {"project": "AADS", "path": "app/main.py", "url": "http://x"})
    assert json.loads(out)["reason_code"] == "tool_not_allowlisted"


def test_policy_has_no_conditional_read_tools():
    assert not hasattr(plan_mode_guard, "_SQL_READ_TOOLS")
    assert not hasattr(plan_mode_guard, "_SHELL_READ_TOOLS")
    banned = set(SQL_TOOLS) | {"run_remote_command", "read_remote_file", "list_remote_dir",
                               "git_remote_status", "inspect_service", "analyze_changes",
                               "code_explorer", "search_all_projects"}
    assert not banned & plan_mode_guard._READ_ONLY_TOOLS


def test_guard_error_fails_closed(plan_mode, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(plan_mode_guard, "classify", boom)
    out = plan_mode_guard.check("health_check", {})
    assert json.loads(out)["reason_code"] == "plan_guard_error"


def test_non_plan_modes_are_never_blocked():
    for mode in ("quality", "fast", ""):
        token = plan_mode_guard.set_response_mode(mode)
        try:
            assert plan_mode_guard.check("write_remote_file", {}) is None
            assert plan_mode_guard.check("anything_unknown", {}) is None
        finally:
            plan_mode_guard.reset_response_mode(token)


def test_offered_tool_list_keeps_only_read_only_candidates():
    tools = [{"name": "health_check"}, {"name": "write_remote_file"}, {"name": "run_remote_command"},
             {"name": "pipeline_runner_submit"}, {"name": "query_database"},
             {"name": "query_db"}, {"name": "query_project_database"}, {"name": "read_remote_file"}]
    kept = [t["name"] for t in plan_mode_guard.filter_offered_tools(tools)]
    assert kept == ["health_check"]
    assert plan_mode_guard.filter_offered_tools(None) is None


# ── 4. 공통 실행기: 기존 프로젝트 승인이 있어도 차단, 부작용 0회 ─────────────

def _executor_with_side_effect_counters(monkeypatch):
    from app.services import live_trading_guard
    from app.services.tool_executor import ToolExecutor

    calls = {"handler": 0, "gate": 0, "grant": 0}

    async def handler(self, *_a, **_k):
        calls["handler"] += 1
        return "executed"

    def gate(*_a, **_k):
        calls["gate"] += 1
        return None

    def grant(*_a, **_k):
        calls["grant"] += 1
        return True

    for name in ("_write_remote_file", "_patch_remote_file", "_run_remote_command",
                 "_pipeline_runner_submit", "_handover_write", "_goal_register"):
        if hasattr(ToolExecutor, name):
            monkeypatch.setattr(ToolExecutor, name, handler)
    monkeypatch.setattr(live_trading_guard, "check", gate)
    for name in ("goal_policy_allows", "is_approved"):
        if hasattr(live_trading_guard, name):
            monkeypatch.setattr(live_trading_guard, name, grant)
    return ToolExecutor(), calls


@pytest.mark.parametrize("tool,args", [
    ("write_remote_file", {"project": "AADS", "path": "a.py", "content": "x"}),
    ("patch_remote_file", {"project": "AADS", "path": "a.py", "old": "a", "new": "b"}),
    ("run_remote_command", {"project": "AADS", "command": "rm -rf /tmp/x"}),
    ("pipeline_runner_submit", {"project": "AADS", "instruction": "do"}),
    ("handover_write", {"content": "x"}),
    ("goal_register", {"title": "x"}),
    ("query_database", {"sql": "SELECT pg_logical_emit_message(true, 'p', 'x')"}),
    ("query_project_database", {"project": "KIS", "query": "SELECT pg_create_restore_point('x')"}),
    ("totally_unknown_tool", {}),
])
async def test_executor_blocks_before_grants_and_gates(plan_mode, monkeypatch, tool, args):
    executor, calls = _executor_with_side_effect_counters(monkeypatch)

    out = await executor.execute(tool, args)

    payload = json.loads(out)
    assert payload["error"] == "plan_mode_blocked"
    assert payload["message"] == EXPECTED_MESSAGE
    assert calls == {"handler": 0, "gate": 0, "grant": 0}


async def test_dispatch_itself_is_guarded(plan_mode, monkeypatch):
    executor, calls = _executor_with_side_effect_counters(monkeypatch)
    out = await executor._dispatch("write_remote_file", {"path": "a", "content": "x"})
    assert json.loads(out)["error"] == "plan_mode_blocked"
    assert calls["handler"] == 0


async def test_dispatch_runs_read_only_tool(plan_mode, monkeypatch):
    from app.services.tool_executor import ToolExecutor

    stub = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(ToolExecutor, "_health_check", stub)
    result = await ToolExecutor()._dispatch("health_check", {})
    assert result == {"status": "ok"}
    stub.assert_awaited_once()


async def test_legacy_execute_tool_is_guarded(plan_mode):
    from app.api import ceo_chat_tools

    out = await ceo_chat_tools.execute_tool(
        "write_remote_file", {"project": "AADS", "path": "a", "content": "x"}, "", "sid"
    )
    assert json.loads(out)["message"] == EXPECTED_MESSAGE


async def test_same_calls_run_normally_outside_plan_mode(monkeypatch):
    from app.services.tool_executor import ToolExecutor

    stub = AsyncMock(return_value="ok")
    monkeypatch.setattr(ToolExecutor, "_health_check", stub)
    assert await ToolExecutor()._dispatch("health_check", {}) == "ok"
    token = plan_mode_guard.set_response_mode("quality")
    try:
        assert plan_mode_guard.check("write_remote_file", {}) is None
    finally:
        plan_mode_guard.reset_response_mode(token)


def test_guard_runs_first_in_execute():
    from app.services.tool_executor import ToolExecutor

    src = inspect.getsource(ToolExecutor.execute)
    guard_at = src.find("plan_mode_guard.check(")
    for later in ("assert_no_untrusted_page_data", "_sideeffect_duplicate(", "_lt_check(", "_dir_check(",
                  "resolve_bound_tenant_id("):
        at = src.find(later)
        assert at > 0, later
        assert 0 < guard_at < at, f"계획만 게이트가 {later} 보다 먼저 와야 한다"


# ── 5. 요청 단위 격리 ─────────────────────────────────────────────────────────

async def test_concurrent_quality_and_plan_do_not_contaminate():
    seen = {}

    async def turn(name, mode, delay):
        token = plan_mode_guard.set_response_mode(mode)
        try:
            await asyncio.sleep(delay)
            seen[name] = (plan_mode_guard.plan_mode_active(), plan_mode_guard.check("write_remote_file", {}) is not None)
            await asyncio.sleep(delay)
            seen[name + "_late"] = plan_mode_guard.plan_mode_active()
        finally:
            plan_mode_guard.reset_response_mode(token)

    await asyncio.gather(
        asyncio.create_task(turn("plan", "plan", 0.01)),
        asyncio.create_task(turn("quality", "quality", 0.005)),
        asyncio.create_task(turn("fast", "fast", 0.002)),
    )

    assert seen["plan"] == (True, True)
    assert seen["quality"] == (False, False)
    assert seen["fast"] == (False, False)
    assert seen["plan_late"] is True
    assert seen["quality_late"] is False
    assert plan_mode_guard.plan_mode_active() is False


async def test_child_task_inherits_plan_and_does_not_leak_back():
    token = plan_mode_guard.set_response_mode("plan")
    try:
        child = await asyncio.create_task(asyncio.sleep(0, result=plan_mode_guard.plan_mode_active()))
        assert child is True
    finally:
        plan_mode_guard.reset_response_mode(token)
    assert plan_mode_guard.plan_mode_active() is False


async def test_explicit_mode_overrides_stale_plan_value():
    stale = plan_mode_guard.set_response_mode("plan")
    try:
        inner = plan_mode_guard.set_response_mode("quality")
        assert plan_mode_guard.plan_mode_active() is False
        plan_mode_guard.reset_response_mode(inner)
        assert plan_mode_guard.plan_mode_active() is True
    finally:
        plan_mode_guard.reset_response_mode(stale)


def test_reset_from_foreign_context_clears_instead_of_leaking():
    token = plan_mode_guard.set_response_mode("plan")
    import contextvars

    contextvars.copy_context().run(plan_mode_guard.reset_response_mode, token)
    plan_mode_guard.reset_response_mode(token)
    assert plan_mode_guard.plan_mode_active() is False


def test_stream_sets_mode_for_every_turn_and_resets_in_finally():
    src = inspect.getsource(chat_service.send_message_stream)
    set_at = src.find("_plan_mode_guard.set_response_mode(response_mode)")
    try_at = src.find("    try:\n        from app.services.tool_executor import current_chat_session_id")
    reset_at = src.find("_plan_mode_guard.reset_response_mode(_plan_mode_token)")
    assert 0 < set_at < try_at < reset_at


# ── 6. 전송 경로: JSON · multipart · 재생성 ──────────────────────────────────

SESSION_ID = str(uuid.uuid4())
CONTEXT = {"tenant": {"id": str(uuid.uuid4())}, "user": {"user_id": str(uuid.uuid4())}}


class _FakeRequest:
    def __init__(self, body=None, *, content_type="application/json", form=None):
        self.headers = {"content-type": content_type}
        self._body = body
        self._form = form

    async def json(self):
        return self._body

    async def form(self):
        return self._form


class _FakeForm(dict):
    def getlist(self, name):
        return []


@pytest.fixture
def router_svc(monkeypatch):
    from app.routers import chat as chat_router

    async def _stream(**kwargs):
        yield "data: {}\n\n"

    m = MagicMock()
    m.get_session = AsyncMock(return_value={"id": SESSION_ID})
    m.get_html_edit_context_state = AsyncMock(return_value={})
    m.send_message_stream = MagicMock(side_effect=lambda **kw: _stream(**kw))
    m.with_background_completion = MagicMock(side_effect=lambda stream, **kw: stream)
    m._save_message = AsyncMock()
    m._normalize_response_mode = chat_service._normalize_response_mode
    m._strip_internal_continuation_context = lambda text: text
    monkeypatch.setattr(chat_router, "svc", m)
    monkeypatch.setattr(chat_router, "is_streaming", lambda sid: False)
    monkeypatch.setattr(
        "app.services.tenant_usage_limits.check_tenant_usage_limit", AsyncMock(return_value=None)
    )
    return chat_router, m


@pytest.mark.parametrize("mode", ["plan", "quality", "fast"])
async def test_json_delivery_preserves_mode(router_svc, mode):
    chat_router, m = router_svc
    body = {"session_id": SESSION_ID, "content": "hello", "response_mode": mode}
    await chat_router.send_message(_FakeRequest(body), contract_version=None, context=CONTEXT)
    assert m.send_message_stream.call_args.kwargs["response_mode"] == mode


@pytest.mark.parametrize("mode", ["plan", "quality", "fast"])
async def test_multipart_delivery_preserves_mode(router_svc, mode):
    chat_router, m = router_svc
    form = _FakeForm(session_id=SESSION_ID, content="hello", response_mode=mode)
    await chat_router.send_message(
        _FakeRequest(content_type="multipart/form-data; boundary=x", form=form),
        contract_version=None, context=CONTEXT,
    )
    assert m.send_message_stream.call_args.kwargs["response_mode"] == mode


async def test_default_mode_stays_quality_when_omitted(router_svc):
    chat_router, m = router_svc
    await chat_router.send_message(
        _FakeRequest({"session_id": SESSION_ID, "content": "hello"}), contract_version=None, context=CONTEXT
    )
    assert m.send_message_stream.call_args.kwargs["response_mode"] == "quality"
    form = _FakeForm(session_id=SESSION_ID, content="hello")
    await chat_router.send_message(
        _FakeRequest(content_type="multipart/form-data; boundary=x", form=form),
        contract_version=None, context=CONTEXT,
    )
    assert m.send_message_stream.call_args.kwargs["response_mode"] == "quality"


def _fake_regenerate_pool(monkeypatch, origin_mode, *, lookup_error=False):
    ai_id = uuid.uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": ai_id, "session_id": uuid.UUID(SESSION_ID), "role": "assistant",
         "created_at": "t2", "content": "answer"},
        {"id": uuid.uuid4(), "content": "do it", "attachments": None},
    ])
    if lookup_error:
        conn.fetchval = AsyncMock(
            side_effect=RuntimeError("db down") if lookup_error is True else lookup_error
        )
    else:
        conn.fetchval = AsyncMock(return_value=origin_mode)
    conn.execute = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    return ai_id


async def _regenerate(chat_router, ai_id, **kw):
    return await chat_router.regenerate_message(
        ai_id, MagicMock(headers={}), contract_version=None, context=CONTEXT, **kw
    )


@pytest.mark.parametrize("mode", ["regenerate", "continue"])
async def test_regenerate_and_continue_inherit_plan(router_svc, monkeypatch, mode):
    chat_router, m = router_svc
    ai_id = _fake_regenerate_pool(monkeypatch, "plan")
    await _regenerate(chat_router, ai_id, mode=mode, response_mode=None)
    assert m.send_message_stream.call_args.kwargs["response_mode"] == "plan"


async def test_regenerate_keeps_existing_quality_behavior(router_svc, monkeypatch):
    chat_router, m = router_svc
    for origin in (None, "quality", "fast"):
        ai_id = _fake_regenerate_pool(monkeypatch, origin)
        await _regenerate(chat_router, ai_id, mode="regenerate", response_mode=None)
        assert m.send_message_stream.call_args.kwargs["response_mode"] == "quality"


async def test_regenerate_lookup_failure_fails_closed_to_plan(router_svc, monkeypatch):
    chat_router, m = router_svc
    ai_id = _fake_regenerate_pool(monkeypatch, None, lookup_error=True)
    await _regenerate(chat_router, ai_id, mode="regenerate", response_mode=None)
    assert m.send_message_stream.call_args.kwargs["response_mode"] == "plan"


async def test_regenerate_missing_column_is_legacy_quality(router_svc, monkeypatch):
    import asyncpg

    chat_router, m = router_svc
    ai_id = _fake_regenerate_pool(monkeypatch, None, lookup_error=asyncpg.exceptions.UndefinedColumnError("response_mode"))
    await _regenerate(chat_router, ai_id, mode="regenerate", response_mode=None)
    assert m.send_message_stream.call_args.kwargs["response_mode"] == "quality"


async def test_regenerate_explicit_mode_wins(router_svc, monkeypatch):
    chat_router, m = router_svc
    ai_id = _fake_regenerate_pool(monkeypatch, "plan")
    await _regenerate(chat_router, ai_id, mode="regenerate", response_mode="quality")
    assert m.send_message_stream.call_args.kwargs["response_mode"] == "quality"


# ── 7. 실행 단위 저장·재개 ────────────────────────────────────────────────────

def _pool_returning(value=None, error=None):
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=error) if error else AsyncMock(return_value=value)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


async def test_load_execution_mode_returns_stored_plan(monkeypatch):
    monkeypatch.setattr(chat_service, "get_pool", lambda: _pool_returning("plan"))
    assert await chat_service._load_execution_response_mode(str(uuid.uuid4())) == "plan"


@pytest.mark.parametrize("stored", [None, "garbage"])
async def test_load_execution_mode_ignores_missing_or_invalid(monkeypatch, stored):
    monkeypatch.setattr(chat_service, "get_pool", lambda: _pool_returning(stored))
    assert await chat_service._load_execution_response_mode(str(uuid.uuid4())) is None


async def test_load_execution_mode_db_error_fails_closed_to_plan(monkeypatch):
    monkeypatch.setattr(chat_service, "get_pool", lambda: _pool_returning(error=RuntimeError("x")))
    assert await chat_service._load_execution_response_mode(str(uuid.uuid4())) == "plan"


async def test_load_execution_mode_missing_column_is_not_plan(monkeypatch):
    import asyncpg

    err = asyncpg.exceptions.UndefinedColumnError("response_mode")
    monkeypatch.setattr(chat_service, "get_pool", lambda: _pool_returning(error=err))
    assert await chat_service._load_execution_response_mode(str(uuid.uuid4())) is None


async def test_load_execution_mode_bad_id_is_none():
    assert await chat_service._load_execution_response_mode("not-a-uuid") is None
    assert await chat_service._load_execution_response_mode(None) is None


def _tx_conn(execute):
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=tx)
    conn.execute = execute
    return conn


async def test_persist_execution_mode_writes():
    conn = _tx_conn(AsyncMock())
    exec_id = uuid.uuid4()
    await chat_service._persist_execution_response_mode(conn, exec_id, "plan")
    sql, eid, mode = conn.execute.await_args.args
    assert "response_mode" in sql and eid == exec_id and mode == "plan"


async def test_persist_plan_failure_propagates_so_turn_does_not_start():
    conn = _tx_conn(AsyncMock(side_effect=RuntimeError("no such column")))
    with pytest.raises(RuntimeError):
        await chat_service._persist_execution_response_mode(conn, uuid.uuid4(), "plan")


@pytest.mark.parametrize("mode", ["quality", "fast"])
async def test_persist_non_plan_failure_keeps_turn_running(mode):
    conn = _tx_conn(AsyncMock(side_effect=RuntimeError("no such column")))
    await chat_service._persist_execution_response_mode(conn, uuid.uuid4(), mode)


@pytest.mark.parametrize("requested,epoch,expect_persist", [
    ("plan", 3, True),
    ("plan", None, False),
    ("quality", 3, False),
    ("fast", 3, False),
    (None, 3, False),
])
async def test_reused_execution_only_upgrades_to_plan_when_this_request_owns_it(
    monkeypatch, requested, epoch, expect_persist
):
    existing = uuid.uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=existing)
    conn.execute = AsyncMock()
    monkeypatch.setattr(chat_service, "_claim_execution_lease", AsyncMock(return_value=epoch))
    persist = AsyncMock()
    monkeypatch.setattr(chat_service, "_persist_execution_response_mode", persist)
    out = await chat_service._get_or_create_turn_execution(
        conn, uuid.uuid4(), user_message_id=uuid.uuid4(), requested_model=None, response_mode=requested,
    )
    assert out == str(existing)
    assert persist.await_count == (1 if expect_persist else 0)
    if expect_persist:
        assert persist.await_args.args[2] == "plan"


def test_resume_restores_plan_and_resets_afterwards():
    src = inspect.getsource(chat_service._resume_single_stream)
    assert "_load_execution_response_mode(execution_id)" in src
    assert "_plan_mode_guard.set_response_mode(_resume_response_mode)" in src
    assert "_plan_mode_guard.filter_offered_tools(tools_for_api)" in src
    assert "_response_mode_prompt_block(_RESPONSE_MODE_PLAN)" in src
    finally_at = src.rfind("finally:")
    assert "_plan_mode_guard.reset_response_mode(_resume_plan_token)" in src[finally_at:]


def test_bridge_and_relay_carry_plan_mode():
    from mcp_servers import aads_tools_bridge
    from app.services import model_selector

    assert "AADS_RESPONSE_MODE" in inspect.getsource(aads_tools_bridge._session_in_plan_mode)
    assert "plan_mode_guard.set_response_mode" in inspect.getsource(aads_tools_bridge._call_tool)
    assert 'req_body["plan_mode"] = True' in inspect.getsource(model_selector._stream_codex_relay_once)
    assert "plan_mode_supported" in inspect.getsource(model_selector._stream_codex_relay_once)
    relay_src = open("scripts/claude_relay_server.py", encoding="utf-8").read()
    assert '"read-only" if plan_mode else _CODEX_SANDBOX_MODE' in relay_src
    assert '"plan_mode_supported": True' in relay_src


async def test_bridge_blocks_write_tool_in_plan_session(monkeypatch):
    from mcp_servers import aads_tools_bridge

    monkeypatch.setenv("AADS_RESPONSE_MODE", "plan")
    monkeypatch.setenv("AADS_SESSION_ID", SESSION_ID)
    monkeypatch.setattr(aads_tools_bridge, "_ensure_db", AsyncMock())
    out = await aads_tools_bridge._call_tool("write_remote_file", {"project": "AADS", "path": "a", "content": "x"})
    assert json.loads(out)["message"] == EXPECTED_MESSAGE
    assert plan_mode_guard.plan_mode_active() is False


async def test_bridge_reads_plan_from_execution_row_when_env_absent(monkeypatch):
    from mcp_servers import aads_tools_bridge

    monkeypatch.delenv("AADS_RESPONSE_MODE", raising=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _pool_returning("plan"))
    assert await aads_tools_bridge._session_in_plan_mode(SESSION_ID) is True
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _pool_returning("quality"))
    assert await aads_tools_bridge._session_in_plan_mode(SESSION_ID) is False
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _pool_returning(error=RuntimeError("x")))
    assert await aads_tools_bridge._session_in_plan_mode(SESSION_ID) is True


async def test_bridge_missing_column_is_not_plan(monkeypatch):
    import asyncpg
    from mcp_servers import aads_tools_bridge

    monkeypatch.delenv("AADS_RESPONSE_MODE", raising=False)
    err = asyncpg.exceptions.UndefinedColumnError("response_mode")
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _pool_returning(error=err))
    assert await aads_tools_bridge._session_in_plan_mode(SESSION_ID) is False


async def test_bridge_lookup_failure_blocks_writes_but_allows_reads(monkeypatch):
    from mcp_servers import aads_tools_bridge

    monkeypatch.delenv("AADS_RESPONSE_MODE", raising=False)
    monkeypatch.setenv("AADS_SESSION_ID", SESSION_ID)
    monkeypatch.setattr(aads_tools_bridge, "_ensure_db", AsyncMock())
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _pool_returning(error=RuntimeError("down")))
    out = await aads_tools_bridge._call_tool("write_remote_file", {"project": "AADS", "path": "a", "content": "x"})
    assert json.loads(out)["error"] == "plan_mode_blocked"


# ── 9. 복합 읽기 도구의 셸 실행 우회 (plan_mode.composite_read_tool_shell_injection) ─────────
#
# code_explorer / search_all_projects 는 모델 인자를 `_ssh_run` → create_subprocess_shell 의
# 셸 문자열에 그대로 끼운다. 읽기처럼 보이는 이름만으로 allowlist 에 올리면 `; 명령 #` 이 실행된다.

SENTINEL = "PLAN_MODE_WRITE_SENTINEL"
_COMPOSITE_INJECTIONS = [
    ("code_explorer", {"project": "KIS", "entry_point": f"app/main.py; {SENTINEL} #"}),
    ("code_explorer", {"project": "AADS", "entry_point": "app/main.py"}),
    ("search_all_projects", {"query": f"needle'; {SENTINEL} #"}),
    ("search_all_projects", {"query": "needle"}),
]


@pytest.fixture
def shell_spies(monkeypatch):
    """셸/SSH 로 내려가는 모든 지점을 스파이로 바꿔 실제 전달 인자를 기록한다. 실제 실행은 없다."""
    from app.services import code_explorer_service as ces
    from app.services.ckp_manager import CKPManager

    seen = {"ssh": [], "shell": [], "handler": 0}

    async def fake_ssh(host, cmd, timeout=30.0):
        seen["ssh"].append((host, cmd))
        return ""

    async def fake_shell(cmd, *_a, **_k):
        seen["shell"].append(cmd)
        raise RuntimeError("shell must not run in tests")

    monkeypatch.setattr(ces, "_ssh_run", fake_ssh)
    monkeypatch.setattr(ces.asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(CKPManager, "get_ckp_summary", AsyncMock(return_value=""))

    from app.services.tool_executor import ToolExecutor

    for name in ("_code_explorer", "_search_all_projects"):
        original = getattr(ToolExecutor, name)

        async def counting(self, inp, _orig=original):
            seen["handler"] += 1
            return await _orig(self, inp)

        monkeypatch.setattr(ToolExecutor, name, counting)
    return seen


@pytest.mark.parametrize("tool,args", _COMPOSITE_INJECTIONS)
def test_composite_tools_are_not_allowlisted_even_with_clean_args(plan_mode, tool, args):
    allowed, reason = plan_mode_guard.classify(tool, args)
    assert (allowed, reason) == (False, "tool_not_allowlisted")
    assert json.loads(plan_mode_guard.check(tool, args))["error"] == "plan_mode_blocked"


def test_composite_tools_are_not_offered_in_plan_mode():
    tools = [{"name": n} for n in ("code_explorer", "search_all_projects", "analyze_changes", "read_remote_file")]
    assert plan_mode_guard.filter_offered_tools(tools) == []


@pytest.mark.parametrize("tool,args", _COMPOSITE_INJECTIONS)
async def test_executor_execute_blocks_composite_before_any_shell(plan_mode, shell_spies, tool, args):
    from app.services.tool_executor import ToolExecutor

    out = await ToolExecutor().execute(tool, args)

    assert json.loads(out)["message"] == EXPECTED_MESSAGE
    assert shell_spies == {"ssh": [], "shell": [], "handler": 0}


@pytest.mark.parametrize("tool,args", _COMPOSITE_INJECTIONS)
async def test_executor_dispatch_blocks_composite_before_any_shell(plan_mode, shell_spies, tool, args):
    from app.services.tool_executor import ToolExecutor

    out = await ToolExecutor()._dispatch(tool, args)

    assert json.loads(out)["error"] == "plan_mode_blocked"
    assert shell_spies == {"ssh": [], "shell": [], "handler": 0}


@pytest.mark.parametrize("tool,args", _COMPOSITE_INJECTIONS)
async def test_legacy_execute_tool_blocks_composite_before_any_shell(plan_mode, shell_spies, tool, args):
    from app.api import ceo_chat_tools

    out = await ceo_chat_tools.execute_tool(tool, dict(args), "", "sid")

    assert json.loads(out)["message"] == EXPECTED_MESSAGE
    assert shell_spies == {"ssh": [], "shell": [], "handler": 0}


@pytest.mark.parametrize("tool,args", _COMPOSITE_INJECTIONS)
async def test_mcp_bridge_blocks_composite_before_any_shell(monkeypatch, shell_spies, tool, args):
    from mcp_servers import aads_tools_bridge

    monkeypatch.setenv("AADS_RESPONSE_MODE", "plan")
    monkeypatch.setenv("AADS_SESSION_ID", SESSION_ID)
    monkeypatch.setattr(aads_tools_bridge, "_ensure_db", AsyncMock())

    out = await aads_tools_bridge._call_tool(tool, dict(args))

    assert json.loads(out)["message"] == EXPECTED_MESSAGE
    assert shell_spies == {"ssh": [], "shell": [], "handler": 0}
    assert plan_mode_guard.plan_mode_active() is False


async def test_mcp_bridge_lookup_failure_still_blocks_composite(monkeypatch, shell_spies):
    from mcp_servers import aads_tools_bridge

    monkeypatch.delenv("AADS_RESPONSE_MODE", raising=False)
    monkeypatch.setenv("AADS_SESSION_ID", SESSION_ID)
    monkeypatch.setattr(aads_tools_bridge, "_ensure_db", AsyncMock())
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _pool_returning(error=RuntimeError("down")))

    out = await aads_tools_bridge._call_tool("search_all_projects", {"query": f"x'; {SENTINEL} #"})

    assert json.loads(out)["error"] == "plan_mode_blocked"
    assert shell_spies == {"ssh": [], "shell": [], "handler": 0}


async def test_mcp_bridge_list_tools_hides_composite_in_plan_env(monkeypatch):
    from mcp_servers import aads_tools_bridge

    monkeypatch.setenv("AADS_RESPONSE_MODE", "plan")
    monkeypatch.delenv("AADS_TOOL_RESIDENT_MODE", raising=False)
    names = {t.name for t in await aads_tools_bridge.list_tools()}
    assert "code_explorer" not in names and "search_all_projects" not in names
    assert "write_remote_file" not in names
    assert "health_check" in names

    monkeypatch.delenv("AADS_RESPONSE_MODE")
    names = {t.name for t in await aads_tools_bridge.list_tools()}
    assert {"code_explorer", "search_all_projects", "write_remote_file"} <= names


async def test_control_outside_plan_the_spy_sees_the_injected_shell_string(shell_spies):
    """대조군: plan 이 아니면 주입 문자열이 그대로 셸로 내려간다 — 위 테스트가 헛돌지 않는다는 증거."""
    from app.services.tool_executor import ToolExecutor

    executor = ToolExecutor()
    await executor._dispatch("code_explorer", dict(_COMPOSITE_INJECTIONS[0][1]))
    await executor._dispatch("search_all_projects", dict(_COMPOSITE_INJECTIONS[2][1]))

    assert shell_spies["handler"] == 2
    cmds = [cmd for _host, cmd in shell_spies["ssh"]]
    assert any(f"; {SENTINEL} #" in c and c.startswith("cat ") for c in cmds)
    assert any(f"'; {SENTINEL} #" in c and "grep" in c for c in cmds)


@pytest.mark.parametrize("args", [
    {"project": "KIS", "days": "7'; PLAN_MODE_WRITE_SENTINEL #"},
    {"project": "KIS; PLAN_MODE_WRITE_SENTINEL", "days": 7},
])
async def test_analyze_changes_is_blocked_before_any_shell(plan_mode, shell_spies, args):
    from app.services.tool_executor import ToolExecutor

    out = await ToolExecutor()._dispatch("analyze_changes", dict(args))

    assert json.loads(out)["error"] == "plan_mode_blocked"
    assert shell_spies == {"ssh": [], "shell": [], "handler": 0}


async def test_pipeline_runner_status_job_id_cannot_escape_jobs_path(plan_mode, monkeypatch):
    """남은 allowlist 감사: job_id 의 `../` 로 내부 API 의 다른 GET 경로에 닿지 못한다."""
    import httpx
    from app.services.tool_executor import ToolExecutor

    urls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    async def fake_get(self, url, **_k):
        urls.append(url)
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await ToolExecutor()._dispatch("pipeline_runner_status", {"job_id": "../../admin/x"})

    assert len(urls) == 1
    assert "/../" not in urls[0] and "%2F" in urls[0]


# ── 10. SQL·셸 도구: 모든 진입점에서 handler/DB helper/셸 호출 0회 ───────────────────────
#
# 실제 쓰기 UDF 나 SSH 는 실행하지 않는다 — 호출 횟수를 세는 스파이만 쓴다.

_SQL_ENTRY_CASES = [
    ("query_database", {"sql": "SELECT pg_logical_emit_message(true, 'p', 'x')"}),
    ("query_db", {"query": "SELECT pg_create_restore_point('x')"}),
    ("query_project_database", {"project": "KIS", "query": "SELECT public.writer_udf()"}),
    ("query_project_database", {"project": "KIS", "query": "SELECT 1"}),
    ("run_remote_command", {"project": "AADS", "command": "ls"}),
    ("run_remote_command", {"project": "AADS", "command": "ls; touch /tmp/PLAN_MODE_WRITE_SENTINEL"}),
]


@pytest.fixture
def sql_shell_spies(monkeypatch):
    from app.api import ceo_chat_tools, ceo_chat_tools_db
    from app.core import db_pool
    from app.services.tool_executor import ToolExecutor

    seen = {"handler": 0, "db_helper": 0, "pool": 0, "remote_cmd": 0, "shell": 0}

    def counter(key, ret=None):
        async def _spy(*_a, **_k):
            seen[key] += 1
            return ret
        return _spy

    def pool_spy(*_a, **_k):
        seen["pool"] += 1
        raise RuntimeError("DB must not be touched in plan mode")

    for name in ("_query_database", "_query_project_database", "_run_remote_command"):
        monkeypatch.setattr(ToolExecutor, name, counter("handler"))
    monkeypatch.setattr(ceo_chat_tools_db, "query_project_database", counter("db_helper"))
    monkeypatch.setattr(ceo_chat_tools, "tool_run_remote_command", counter("remote_cmd"))
    monkeypatch.setattr(db_pool, "get_pool", pool_spy)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", counter("shell"))
    return seen


_ZERO = {"handler": 0, "db_helper": 0, "pool": 0, "remote_cmd": 0, "shell": 0}


@pytest.mark.parametrize("tool,args", _SQL_ENTRY_CASES)
async def test_executor_execute_never_reaches_sql_or_shell_helpers(plan_mode, sql_shell_spies, tool, args):
    from app.services.tool_executor import ToolExecutor

    out = await ToolExecutor().execute(tool, dict(args))

    assert json.loads(out)["message"] == EXPECTED_MESSAGE
    assert sql_shell_spies == _ZERO


@pytest.mark.parametrize("tool,args", _SQL_ENTRY_CASES)
async def test_executor_dispatch_never_reaches_sql_or_shell_helpers(plan_mode, sql_shell_spies, tool, args):
    from app.services.tool_executor import ToolExecutor

    out = await ToolExecutor()._dispatch(tool, dict(args))

    assert json.loads(out)["error"] == "plan_mode_blocked"
    assert sql_shell_spies == _ZERO


@pytest.mark.parametrize("tool,args", _SQL_ENTRY_CASES)
async def test_legacy_execute_tool_never_reaches_sql_or_shell_helpers(plan_mode, sql_shell_spies, tool, args):
    from app.api import ceo_chat_tools

    out = await ceo_chat_tools.execute_tool(tool, dict(args), "", "sid")

    assert json.loads(out)["message"] == EXPECTED_MESSAGE
    assert sql_shell_spies == _ZERO


@pytest.mark.parametrize("tool,args", _SQL_ENTRY_CASES)
async def test_mcp_bridge_never_reaches_sql_or_shell_helpers(monkeypatch, sql_shell_spies, tool, args):
    from mcp_servers import aads_tools_bridge

    monkeypatch.setenv("AADS_RESPONSE_MODE", "plan")
    monkeypatch.setenv("AADS_SESSION_ID", SESSION_ID)
    monkeypatch.setattr(aads_tools_bridge, "_ensure_db", AsyncMock())

    out = await aads_tools_bridge._call_tool(tool, dict(args))

    assert json.loads(out)["message"] == EXPECTED_MESSAGE
    assert sql_shell_spies == _ZERO
    assert plan_mode_guard.plan_mode_active() is False


async def test_mcp_bridge_lookup_failure_still_blocks_sql(monkeypatch, sql_shell_spies):
    from mcp_servers import aads_tools_bridge

    monkeypatch.delenv("AADS_RESPONSE_MODE", raising=False)
    monkeypatch.setenv("AADS_SESSION_ID", SESSION_ID)
    monkeypatch.setattr(aads_tools_bridge, "_ensure_db", AsyncMock())
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _pool_returning(error=RuntimeError("down")))

    out = await aads_tools_bridge._call_tool("query_database", {"sql": "SELECT pg_logical_emit_message(true,'p','x')"})

    assert json.loads(out)["error"] == "plan_mode_blocked"
    assert {k: v for k, v in sql_shell_spies.items() if k != "pool"} == {k: v for k, v in _ZERO.items() if k != "pool"}


async def test_mcp_bridge_list_tools_hides_sql_and_shell_in_plan_env(monkeypatch):
    from mcp_servers import aads_tools_bridge

    monkeypatch.setenv("AADS_RESPONSE_MODE", "plan")
    monkeypatch.delenv("AADS_TOOL_RESIDENT_MODE", raising=False)
    names = {t.name for t in await aads_tools_bridge.list_tools()}
    assert not names & {"query_database", "query_db", "query_project_database", "run_remote_command",
                        "read_remote_file", "list_remote_dir", "analyze_changes"}


async def test_control_outside_plan_sql_handler_still_reached(monkeypatch, sql_shell_spies):
    """대조군: plan 이 아니면 같은 호출이 handler 에 도달한다 — 위 0회 검증이 헛돌지 않는다는 증거."""
    from app.services.tool_executor import ToolExecutor

    await ToolExecutor()._dispatch("query_database", {"sql": "SELECT 1"})
    await ToolExecutor()._dispatch("run_remote_command", {"project": "AADS", "command": "ls"})

    assert sql_shell_spies["handler"] == 2
