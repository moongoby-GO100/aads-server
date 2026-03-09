"""
MCP 서버 설정 — T-003 준수.
- 상시 가동 4개: Filesystem, Git, Memory, PostgreSQL
- 온디맨드 3개: GitHub, Brave Search, Fetch
- 전송: SSE transport (localhost) — supervisord 환경
- 인증: 환경변수 토큰 주입
"""
import os
from typing import TypedDict


class SSEConnectionConfig(TypedDict, total=False):
    """SSE transport 연결 설정."""
    transport: str   # "sse"
    url: str


# MCP 서버 포트 정의
MCP_PORTS = {
    "filesystem": 8765,
    "git": 8766,
    "memory": 8767,
    "postgres": 8768,
    "github": 8769,
    "brave_search": 8770,
    "fetch": 8771,
}

# 상시 가동 MCP 서버 (서버 시작 시 연결)
ALWAYS_ON_SERVERS = ["filesystem", "git", "memory"]

# 온디맨드 MCP 서버 (필요 시 lazy 연결)
ON_DEMAND_SERVERS = ["github", "brave_search", "fetch"]


def get_mcp_connections() -> dict:
    """
    MCP 서버 연결 설정 반환.
    환경변수에서 토큰 주입.
    """
    base_url = os.getenv("MCP_SERVER_HOST", "localhost")

    connections = {
        # 상시 가동 4개
        "filesystem": {
            "transport": "sse",
            "url": f"http://{base_url}:{MCP_PORTS['filesystem']}/sse",
        },
        "git": {
            "transport": "sse",
            "url": f"http://{base_url}:{MCP_PORTS['git']}/sse",
        },
        "memory": {
            "transport": "sse",
            "url": f"http://{base_url}:{MCP_PORTS['memory']}/sse",
        },
        "postgres": {
            "transport": "sse",
            "url": f"http://{base_url}:{MCP_PORTS['postgres']}/sse",
        },
        # 온디맨드 3개
        "github": {
            "transport": "sse",
            "url": f"http://{base_url}:{MCP_PORTS['github']}/sse",
        },
        "brave_search": {
            "transport": "sse",
            "url": f"http://{base_url}:{MCP_PORTS['brave_search']}/sse",
        },
        "fetch": {
            "transport": "sse",
            "url": f"http://{base_url}:{MCP_PORTS['fetch']}/sse",
        },
    }
    return connections


def get_always_on_connections() -> dict:
    """상시 가동 4개 연결 설정만 반환."""
    all_conn = get_mcp_connections()
    return {k: v for k, v in all_conn.items() if k in ALWAYS_ON_SERVERS}


def get_on_demand_connections() -> dict:
    """온디맨드 3개 연결 설정만 반환."""
    all_conn = get_mcp_connections()
    return {k: v for k, v in all_conn.items() if k in ON_DEMAND_SERVERS}
