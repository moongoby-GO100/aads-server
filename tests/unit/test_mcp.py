"""MCP 클라이언트 단위 테스트"""
import pytest
import sys
import os
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def test_mcp_config_always_on():
    """상시 가동 4개 서버 설정 확인."""
    from app.mcp.config import get_always_on_connections, ALWAYS_ON_SERVERS
    connections = get_always_on_connections()
    assert set(connections.keys()) == set(ALWAYS_ON_SERVERS)
    assert len(connections) == 4
    for name, conn in connections.items():
        assert conn["transport"] == "sse"
        assert "url" in conn
        assert "localhost" in conn["url"] or "127.0.0.1" in conn["url"]


def test_mcp_config_on_demand():
    """온디맨드 3개 서버 설정 확인."""
    from app.mcp.config import get_on_demand_connections, ON_DEMAND_SERVERS
    connections = get_on_demand_connections()
    assert set(connections.keys()) == set(ON_DEMAND_SERVERS)
    assert len(connections) == 3


def test_mcp_config_all_servers():
    """전체 7개 서버 설정 확인."""
    from app.mcp.config import get_mcp_connections
    connections = get_mcp_connections()
    assert len(connections) == 7
    expected = {"filesystem", "git", "memory", "postgres", "github", "brave_search", "fetch"}
    assert set(connections.keys()) == expected


def test_mcp_config_ports_unique():
    """각 서버 포트가 고유함을 확인."""
    from app.mcp.config import MCP_PORTS
    ports = list(MCP_PORTS.values())
    assert len(ports) == len(set(ports)), "Duplicate ports found"


@pytest.mark.asyncio
async def test_mcp_manager_initialize():
    """MCPClientManager 초기화 테스트."""
    from app.mcp.client import MCPClientManager

    mock_client = MagicMock()

    with patch("app.mcp.client.MultiServerMCPClient", return_value=mock_client):
        manager = MCPClientManager()
        await manager.initialize()

    assert manager.is_initialized


@pytest.mark.asyncio
async def test_mcp_manager_get_tools_no_client():
    """MCP 클라이언트 없을 때 빈 목록 반환 (graceful degradation)."""
    from app.mcp.client import MCPClientManager

    manager = MCPClientManager()
    manager._client = None  # 클라이언트 없는 상태
    manager._initialized = True

    tools = await manager.get_tools("filesystem")
    assert tools == []


@pytest.mark.asyncio
async def test_mcp_manager_get_tools_with_client():
    """MCP 클라이언트 있을 때 도구 목록 반환."""
    from app.mcp.client import MCPClientManager

    mock_tool = MagicMock()
    mock_tool.name = "read_file"

    mock_client = AsyncMock()
    mock_client.get_tools = AsyncMock(return_value=[mock_tool])

    with patch("app.mcp.client.MultiServerMCPClient", return_value=mock_client):
        manager = MCPClientManager()
        manager._client = mock_client
        manager._available_servers = {"filesystem"}
        manager._initialized = True

        tools = await manager.get_tools("filesystem")

    assert len(tools) == 1
    assert tools[0].name == "read_file"


@pytest.mark.asyncio
async def test_mcp_manager_get_tools_server_not_available():
    """가용하지 않은 서버 요청 시 빈 목록 반환."""
    from app.mcp.client import MCPClientManager

    mock_client = MagicMock()

    with patch("app.mcp.client.MultiServerMCPClient", return_value=mock_client):
        manager = MCPClientManager()
        manager._client = mock_client
        manager._available_servers = {"filesystem"}  # git 없음
        manager._initialized = True

        tools = await manager.get_tools("git")

    assert tools == []


@pytest.mark.asyncio
async def test_mcp_manager_get_tools_connection_error():
    """연결 오류 시 graceful degradation."""
    from app.mcp.client import MCPClientManager

    mock_client = AsyncMock()
    mock_client.get_tools = AsyncMock(side_effect=ConnectionError("Connection refused"))

    with patch("app.mcp.client.MultiServerMCPClient", return_value=mock_client):
        manager = MCPClientManager()
        manager._client = mock_client
        manager._available_servers = {"filesystem"}
        manager._initialized = True

        # 연결 오류 시 빈 목록 반환 (예외 미전파)
        tools = await manager.get_tools("filesystem")

    assert tools == []


@pytest.mark.asyncio
async def test_mcp_manager_on_demand_lazy():
    """온디맨드 서버 lazy 연결 테스트."""
    from app.mcp.client import MCPClientManager

    mock_tool = MagicMock()
    mock_tool.name = "web_search"

    mock_client = AsyncMock()
    mock_client.get_tools = AsyncMock(return_value=[mock_tool])

    with patch("app.mcp.client.MultiServerMCPClient", return_value=mock_client):
        manager = MCPClientManager()
        tools = await manager.get_on_demand_tools("brave_search")

    assert len(tools) == 1


@pytest.mark.asyncio
async def test_mcp_manager_on_demand_invalid_server():
    """온디맨드 아닌 서버 이름으로 요청 시 빈 목록."""
    from app.mcp.client import MCPClientManager

    manager = MCPClientManager()
    tools = await manager.get_on_demand_tools("filesystem")  # filesystem은 always-on

    assert tools == []


def test_mcp_manager_is_server_available():
    """서버 가용 여부 확인."""
    from app.mcp.client import MCPClientManager

    manager = MCPClientManager()
    manager._available_servers = {"filesystem", "git"}

    assert manager.is_server_available("filesystem") is True
    assert manager.is_server_available("github") is False


@pytest.mark.asyncio
async def test_mcp_manager_shutdown():
    """MCPClientManager 종료 테스트."""
    from app.mcp.client import MCPClientManager

    mock_client = MagicMock()

    with patch("app.mcp.client.MultiServerMCPClient", return_value=mock_client):
        manager = MCPClientManager()
        manager._client = mock_client
        manager._available_servers = {"filesystem"}
        manager._initialized = True

        await manager.shutdown()

    assert manager._client is None
    assert manager._available_servers == set()
    assert not manager.is_initialized
