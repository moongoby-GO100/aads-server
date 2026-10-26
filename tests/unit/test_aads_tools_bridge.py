"""aads_tools_bridge 단위 테스트."""
import os
import sys
import json
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.mark.asyncio
async def test_ensure_db_uses_env_backed_init_pool():
    """DATABASE_URL가 있으면 init_pool()을 인자 없이 호출한다."""
    import mcp_servers.aads_tools_bridge as bridge

    original = bridge._db_initialized
    bridge._db_initialized = False
    try:
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/test"}, clear=False):
            with patch("app.core.db_pool.init_pool", new=AsyncMock()) as mock_init:
                await bridge._ensure_db()

        mock_init.assert_awaited_once_with()
        assert bridge._db_initialized is True
    finally:
        bridge._db_initialized = original


@pytest.mark.asyncio
async def test_mcp_bridge_binds_aads_session_for_runner_submit():
    """MCP 경로에서도 러너 제출은 AADS_SESSION_ID를 현재 채팅으로 사용한다."""
    import mcp_servers.aads_tools_bridge as bridge

    captured = {}

    class FakeExecutor:
        async def execute(self, name, params):
            from app.services.tool_executor import current_chat_session_id

            captured["name"] = name
            captured["params"] = params
            captured["context_session_id"] = current_chat_session_id.get("")
            return json.dumps({"ok": True})

    session_id = "11111111-1111-1111-1111-111111111111"
    wrong_session = "22222222-2222-2222-2222-222222222222"

    with patch.dict(os.environ, {"AADS_SESSION_ID": session_id}, clear=False), patch(
        "mcp_servers.aads_tools_bridge._ensure_db", new=AsyncMock()
    ), patch(
        "mcp_servers.aads_tools_bridge._session_in_plan_mode", new=AsyncMock(return_value=False)
    ), patch("app.services.tool_executor.ToolExecutor", return_value=FakeExecutor()):
        result = await bridge._call_tool(
            "pipeline_runner_submit",
            {"project": "AADS", "instruction": "test", "session_id": wrong_session},
        )

    assert json.loads(result)["ok"] is True
    assert captured["name"] == "pipeline_runner_submit"
    assert captured["params"]["session_id"] == session_id
    assert captured["context_session_id"] == session_id


@pytest.mark.asyncio
async def test_mcp_bridge_preserves_global_scope_without_session_filter():
    """scope=all 상태 조회는 세션 필터를 주입하지 않는다."""
    import mcp_servers.aads_tools_bridge as bridge

    captured = {}

    class FakeExecutor:
        async def execute(self, name, params):
            captured["name"] = name
            captured["params"] = params
            return json.dumps({"ok": True})

    with patch.dict(
        os.environ,
        {"AADS_SESSION_ID": "11111111-1111-1111-1111-111111111111"},
        clear=False,
    ), patch("mcp_servers.aads_tools_bridge._ensure_db", new=AsyncMock()), patch(
        "app.services.tool_executor.ToolExecutor", return_value=FakeExecutor()
    ):
        await bridge._call_tool("pipeline_runner_status", {"scope": "all"})

    assert captured["name"] == "pipeline_runner_status"
    assert captured["params"] == {"scope": "all"}


@pytest.mark.asyncio
async def test_mcp_bridge_db_tool_returns_before_client_disconnect(monkeypatch):
    """DB MCP 호출은 상위 30초 제한 전에 구조화된 timeout을 반환한다."""
    import mcp_servers.aads_tools_bridge as bridge

    async def slow_call_tool(name, params):
        await bridge.asyncio.sleep(0.03)
        return json.dumps({"ok": True})

    monkeypatch.setattr(bridge, "_MCP_DB_TOOL_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(bridge, "_call_tool", slow_call_tool)

    result = await bridge.call_tool("query_database", {"query": "SELECT 1"})

    payload = json.loads(result[0].text)
    assert payload["error_code"] == "mcp_db_tool_deadline"
    assert payload["tool"] == "query_database"


@pytest.mark.asyncio
async def test_bridge_status_without_api_guard(monkeypatch):
    """API 이미지에 plan guard가 없어도 브리지의 상태 조회는 실행한다."""
    import builtins
    import mcp_servers.aads_tools_bridge as bridge
    original_import = builtins.__import__

    def without_api_guard(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.services" and "plan_mode_guard" in (fromlist or ()):
            raise ImportError("cannot import name 'plan_mode_guard' from 'app.services'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_api_guard)
    monkeypatch.setattr(bridge, "_ensure_db", AsyncMock())
    monkeypatch.setattr(bridge, "_session_in_plan_mode", AsyncMock(return_value=True))
    execute = AsyncMock(return_value='{"status":"ok"}')
    monkeypatch.setattr(bridge, "_call_tool_unguarded", execute)
    result = await bridge.call_tool("pipeline_runner_status", {})
    assert json.loads(result[0].text) == {"status": "ok"}
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_plan_blocks_before_dispatch_and_resets(monkeypatch):
    import mcp_servers.aads_tools_bridge as bridge
    from mcp_servers import plan_mode_guard as guard
    monkeypatch.setattr(bridge, "_ensure_db", AsyncMock())
    monkeypatch.setattr(bridge, "_session_in_plan_mode", AsyncMock(return_value=True))
    execute = AsyncMock()
    monkeypatch.setattr(bridge, "_call_tool_unguarded", execute)
    for name in ("pipeline_runner_submit", "run_remote_command", "new_unknown_tool"):
        payload = json.loads(await bridge._call_tool(name, {}))
        assert payload["error"] == "plan_mode_blocked"
        assert not guard.plan_mode_active()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_plan_catalog_without_api_guard(monkeypatch):
    import mcp_servers.aads_tools_bridge as bridge
    monkeypatch.setenv("AADS_RESPONSE_MODE", "plan")
    monkeypatch.setattr(bridge, "_get_tool_definitions", lambda: [
        {"name": "pipeline_runner_status"}, {"name": "run_remote_command"},
    ])
    assert [tool.name for tool in await bridge.list_tools()] == ["pipeline_runner_status", "plan_read_source", "plan_database_status"]


@pytest.mark.asyncio
async def test_bridge_lookup_failure_stays_fail_closed(monkeypatch):
    import mcp_servers.aads_tools_bridge as bridge
    monkeypatch.delenv("AADS_RESPONSE_MODE", raising=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: (_ for _ in ()).throw(RuntimeError("DB unavailable")))
    assert await bridge._session_in_plan_mode("11111111-1111-1111-1111-111111111111") is True
