"""MCP 브리지 경유 호출에서 핸드오버 도구의 세션/테넌트 바인딩 회귀 테스트.

2026-09-14: `handover_write`/`handover_search`/`handover_export` 가
`missing_tenant_id` 로 전부 실패했다. 원인은 MCP 브리지 호출이 채팅 턴과 다른
프로세스 컨텍스트라 `current_chat_session_id`/`current_tenant_id` contextvar 가
비고, `execute_tool` 의 `chat_session_id` 인자도 전달되지 않아
`resolve_bound_tenant_id` 의 3단 폴백이 통째로 무력화된 것이다.

여기서 고정하는 계약:
  1. 스키마는 `session_id` 만 노출한다. `tenant_id` 노출은 임의 테넌트 주입 경로다.
  2. contextvar 가 없어도 명시 `session_id` 로 tenant 가 해석된다.
  3. 아무 근거도 없으면 조용히 성공하지 말고 `hint` 가 붙은 오류를 돌려준다.
  4. contextvar 가 있으면 그것이 우선하고 DB 조회를 하지 않는다.
  5. 모르는 세션을 주면 다른 테넌트로 넘어가지 않는다.
  6. `AADS_MCP_CHAT_SESSION_ID` 는 설정됐을 때만 폴백으로 쓰인다.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_MISSING_SESSION_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_TENANT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONTEXT_TENANT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_SESSION_DESCRIPTION = (
    "MCP 브리지 등 채팅 컨텍스트가 없는 호출에서만 명시. "
    "생략 시 현재 채팅 세션을 사용."
)
_HANDOVER_TOOLS = ("handover_write", "handover_search", "handover_export")


@pytest.fixture(autouse=True)
def _clear_tenant_binding_context():
    """테스트 간 contextvar 누수를 막는다."""
    from app.services.tool_executor import current_chat_session_id, current_tenant_id

    session_token = current_chat_session_id.set("")
    tenant_token = current_tenant_id.set("")
    try:
        yield
    finally:
        current_tenant_id.reset(tenant_token)
        current_chat_session_id.reset(session_token)


def test_handover_schemas_expose_only_optional_session_binding():
    """계약 1 — session_id 는 선택 파라미터로 노출하고 tenant_id 는 노출하지 않는다."""
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry()
    for tool_name in _HANDOVER_TOOLS:
        schema = registry.get_tool(tool_name)["input_schema"]
        assert schema["properties"]["session_id"] == {
            "type": "string",
            "description": _SESSION_DESCRIPTION,
        }, tool_name
        assert "session_id" not in schema.get("required", []), tool_name
        assert "tenant_id" not in schema["properties"], tool_name


@pytest.mark.asyncio
async def test_session_id_resolves_tenant_without_contextvars():
    """계약 2 — contextvar 가 비어도 명시 session_id 로 tenant 를 해석한다."""
    from app.services.tool_executor import ToolExecutor

    resolve_tenant = AsyncMock(return_value=_TENANT_ID)
    list_entries = AsyncMock(return_value=([], 0))
    with patch(
        "app.services.agent_sdk_service.get_active_chat_session_id",
        return_value="",
    ), patch(
        "app.services.tenant_usage_limits.resolve_tenant_id_for_session",
        resolve_tenant,
    ), patch(
        "app.services.handover_store.list_handover_entries",
        list_entries,
    ):
        result = await ToolExecutor()._handover_search(
            {"project": "AADS", "session_id": _SESSION_ID}
        )

    resolve_tenant.assert_awaited_once_with(_SESSION_ID)
    assert "error" not in result
    assert list_entries.await_args.kwargs["tenant_id"] == _TENANT_ID


@pytest.mark.asyncio
async def test_missing_session_and_contextvars_returns_actionable_hint():
    """계약 3 — 근거가 전혀 없으면 조치 가능한 hint 와 함께 실패한다."""
    from app.services.tool_executor import ToolExecutor

    with patch(
        "app.services.agent_sdk_service.get_active_chat_session_id",
        return_value="",
    ):
        result = await ToolExecutor()._handover_write(
            {"project": "AADS", "title": "제목", "body": "본문"}
        )

    assert result["error"] == "missing_tenant_id"
    assert "handover_write" in result["message"]
    assert "session_id" in result["hint"]
    assert "contextvar" in result["hint"]


@pytest.mark.asyncio
async def test_tenant_contextvar_wins_without_database_lookup():
    """계약 4 — contextvar 가 있으면 우선하고 DB 조회를 하지 않는다."""
    from app.services.tool_executor import ToolExecutor, current_tenant_id

    resolve_tenant = AsyncMock(return_value=_TENANT_ID)
    list_entries = AsyncMock(return_value=([], 0))
    token = current_tenant_id.set(_CONTEXT_TENANT_ID)
    try:
        with patch(
            "app.services.tenant_usage_limits.resolve_tenant_id_for_session",
            resolve_tenant,
        ), patch(
            "app.services.handover_store.list_handover_entries",
            list_entries,
        ):
            await ToolExecutor()._handover_search(
                {"project": "AADS", "session_id": _SESSION_ID}
            )
    finally:
        current_tenant_id.reset(token)

    resolve_tenant.assert_not_awaited()
    assert list_entries.await_args.kwargs["tenant_id"] == _CONTEXT_TENANT_ID


@pytest.mark.asyncio
async def test_unknown_session_does_not_fall_back_to_another_active_session():
    """계약 5 — 모르는 세션은 다른 테넌트로 조용히 넘어가지 않는다."""
    from app.services.tool_executor import ToolExecutor

    resolve_tenant = AsyncMock(return_value=None)
    list_entries = AsyncMock(return_value=([], 0))
    with patch(
        "app.services.agent_sdk_service.get_active_chat_session_id",
        return_value="",
    ), patch(
        "app.services.tenant_usage_limits.resolve_tenant_id_for_session",
        resolve_tenant,
    ), patch(
        "app.services.handover_store.list_handover_entries",
        list_entries,
    ):
        result = await ToolExecutor()._handover_search(
            {"project": "AADS", "session_id": _MISSING_SESSION_ID}
        )

    resolve_tenant.assert_awaited_once_with(_MISSING_SESSION_ID)
    list_entries.assert_not_awaited()
    assert result["error"] == "missing_tenant_id"


@pytest.mark.asyncio
async def test_handover_export_also_returns_hint_when_unbound():
    """계약 3 보강 — export 도 같은 오류 계약을 따른다."""
    from app.services.tool_executor import ToolExecutor

    with patch(
        "app.services.agent_sdk_service.get_active_chat_session_id",
        return_value="",
    ):
        result = await ToolExecutor()._handover_export({"project": "AADS"})

    assert result["error"] == "missing_tenant_id"
    assert "handover_export" in result["message"]
    assert "hint" in result


@pytest.mark.asyncio
async def test_execute_tool_uses_mcp_session_environment_fallback(monkeypatch):
    """계약 6 — 환경변수가 설정되면 chat_session_id 폴백으로 쓰인다."""
    from app.api import ceo_chat_tools

    monkeypatch.setenv("AADS_MCP_CHAT_SESSION_ID", _SESSION_ID)
    resolve_bound = AsyncMock(return_value=_TENANT_ID)
    with patch("app.services.tool_executor.resolve_bound_tenant_id", resolve_bound):
        await ceo_chat_tools.execute_tool("__nonexistent_tool_for_test__", {}, "")

    resolve_bound.assert_awaited_once()
    assert resolve_bound.await_args.args[1] == _SESSION_ID


@pytest.mark.asyncio
async def test_execute_tool_without_env_keeps_existing_behaviour(monkeypatch):
    """계약 6 보강 — 환경변수가 없으면 임의 세션을 고르지 않고 빈 값 그대로 둔다."""
    from app.api import ceo_chat_tools

    monkeypatch.delenv("AADS_MCP_CHAT_SESSION_ID", raising=False)
    resolve_bound = AsyncMock(return_value="")
    with patch("app.services.tool_executor.resolve_bound_tenant_id", resolve_bound):
        await ceo_chat_tools.execute_tool("__nonexistent_tool_for_test__", {}, "")

    resolve_bound.assert_awaited_once()
    assert resolve_bound.await_args.args[1] == ""


@pytest.mark.asyncio
async def test_explicit_session_argument_beats_environment_fallback(monkeypatch):
    """계약 6 보강 — 호출자가 넘긴 chat_session_id 가 환경변수보다 우선한다."""
    from app.api import ceo_chat_tools

    monkeypatch.setenv("AADS_MCP_CHAT_SESSION_ID", _MISSING_SESSION_ID)
    resolve_bound = AsyncMock(return_value=_TENANT_ID)
    with patch("app.services.tool_executor.resolve_bound_tenant_id", resolve_bound):
        await ceo_chat_tools.execute_tool(
            "__nonexistent_tool_for_test__", {}, "", chat_session_id=_SESSION_ID
        )

    assert resolve_bound.await_args.args[1] == _SESSION_ID
