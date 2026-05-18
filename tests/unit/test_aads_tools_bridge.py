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
