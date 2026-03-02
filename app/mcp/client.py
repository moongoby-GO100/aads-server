"""
MCP 클라이언트 매니저.
- MultiServerMCPClient (langchain-mcp-adapters) 래퍼
- 상시 가동 4개: 서버 시작 시 연결
- 온디맨드 3개: 요청 시 lazy 연결
- 연결 실패 시 graceful degradation
"""
import structlog
from typing import Optional

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.mcp.config import (
    get_mcp_connections,
    get_always_on_connections,
    get_on_demand_connections,
    ALWAYS_ON_SERVERS,
    ON_DEMAND_SERVERS,
)

logger = structlog.get_logger()

# 전역 매니저 인스턴스 (main.py lifespan에서 초기화)
_mcp_manager: Optional["MCPClientManager"] = None


class MCPClientManager:
    """
    MCP 서버 연결 매니저.
    Phase 1 구현: 연결 설정 보유 + graceful degradation.
    실제 MCP 서버는 supervisord로 별도 기동.
    """

    def __init__(self):
        self._connections = get_mcp_connections()
        self._available_servers: set[str] = set()
        self._client: Optional[MultiServerMCPClient] = None
        self._initialized = False

    async def initialize(self) -> None:
        """
        서버 시작 시 호출.
        상시 가동 4개 서버 연결 시도 + graceful degradation.
        """
        always_on_connections = get_always_on_connections()
        available = {}

        for server_name, conn_config in always_on_connections.items():
            try:
                # 연결 가능 여부 확인 (실제 연결 없이 설정 검증)
                if conn_config.get("url"):
                    available[server_name] = conn_config
                    logger.info("mcp_server_configured", server=server_name, url=conn_config["url"])
            except Exception as e:
                logger.warning("mcp_server_config_failed", server=server_name, error=str(e))

        if available:
            try:
                self._client = MultiServerMCPClient(available)
                self._available_servers = set(available.keys())
                self._initialized = True
                logger.info(
                    "mcp_client_initialized",
                    servers=list(self._available_servers),
                    always_on_count=len(available),
                )
            except Exception as e:
                logger.error("mcp_client_init_failed", error=str(e))
                self._client = None
                self._initialized = False
        else:
            logger.warning("mcp_no_servers_available")
            self._initialized = True  # graceful degradation: 서버 없어도 동작

    async def get_tools(self, server_name: Optional[str] = None) -> list:
        """
        MCP 서버 도구 목록 반환.
        서버가 없거나 연결 실패 시 빈 목록 반환 (graceful degradation).
        """
        if not self._client:
            logger.debug("mcp_no_client_graceful_degradation")
            return []

        if server_name and server_name not in self._available_servers:
            logger.debug("mcp_server_not_available", server=server_name)
            return []

        try:
            tools = await self._client.get_tools(server_name=server_name)
            logger.debug("mcp_tools_loaded", server=server_name, count=len(tools))
            return tools
        except Exception as e:
            logger.warning("mcp_get_tools_failed", server=server_name, error=str(e))
            return []  # graceful degradation

    async def get_on_demand_tools(self, server_name: str) -> list:
        """
        온디맨드 서버 도구 lazy 연결 + 반환.
        연결 실패 시 빈 목록 반환 (graceful degradation).
        """
        if server_name not in ON_DEMAND_SERVERS:
            logger.warning("mcp_not_on_demand_server", server=server_name)
            return []

        on_demand_connections = get_on_demand_connections()
        conn_config = on_demand_connections.get(server_name)
        if not conn_config:
            return []

        try:
            # 온디맨드: 임시 클라이언트 생성
            temp_client = MultiServerMCPClient({server_name: conn_config})
            tools = await temp_client.get_tools(server_name=server_name)
            logger.info("mcp_on_demand_tools_loaded", server=server_name, count=len(tools))
            return tools
        except Exception as e:
            logger.warning("mcp_on_demand_failed", server=server_name, error=str(e))
            return []  # graceful degradation

    def is_server_available(self, server_name: str) -> bool:
        """서버 가용 여부 확인."""
        return server_name in self._available_servers

    @property
    def available_servers(self) -> list[str]:
        """현재 가용 서버 목록."""
        return list(self._available_servers)

    @property
    def is_initialized(self) -> bool:
        """초기화 완료 여부."""
        return self._initialized

    async def shutdown(self) -> None:
        """종료 시 리소스 정리."""
        self._client = None
        self._available_servers = set()
        self._initialized = False
        logger.info("mcp_client_shutdown")


def get_mcp_manager() -> Optional["MCPClientManager"]:
    """전역 MCPClientManager 반환."""
    return _mcp_manager


def set_mcp_manager(manager: "MCPClientManager") -> None:
    """전역 MCPClientManager 설정 (main.py lifespan에서 호출)."""
    global _mcp_manager
    _mcp_manager = manager
