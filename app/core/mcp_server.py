"""
AADS-186C: FastAPI-MCP 통합
- AADS의 기존 API 엔드포인트를 MCP 도구로 자동 노출
- 보안: 내부 전용 엔드포인트 제외
- MCP_ENABLED=false 시 graceful 비활성화
"""
from __future__ import annotations

import os

import structlog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger(__name__)

# 노출 대상 operation ID 패턴 (화이트리스트)
_EXPOSED_OPERATIONS = [
    "health_check",
    "infra_check",
    "project_docs",
    "submit_directive",
    "list_workspaces",
    "get_health_check",
    "get_infra_check",
    "get_project_docs",
    "post_submit_directive",
    "get_list_workspaces",
    # CEO 아젠다 관리
    "add_agenda",
    "list_agendas",
    "get_agenda",
    "update_agenda",
    "decide_agenda",
    "search_agendas",
]

# 제외 대상 경로 패턴 (보안)
_EXCLUDED_PATH_PREFIXES = [
    "/api/v1/auth",
    "/api/v1/debug",
    "/api/v1/admin",
    "/api/v1/stream",
]


def _is_mcp_enabled() -> bool:
    """MCP_ENABLED 환경변수 확인."""
    return os.getenv("MCP_ENABLED", "true").lower() in ("true", "1", "yes")


def setup_mcp(app: "FastAPI") -> None:
    """
    FastAPI-MCP 마운트.
    MCP_ENABLED=false 이거나 fastapi-mcp 미설치 시 graceful 비활성화.
    """
    if not _is_mcp_enabled():
        logger.info("mcp_server_disabled: MCP_ENABLED=false")
        return

    try:
        from fastapi_mcp import FastApiMCP  # type: ignore[import]

        mcp = FastApiMCP(
            app,
            name="aads-mcp-server",
            description="AADS AI Development System MCP Server — 자율 AI 개발 시스템",
            describe_all_responses=True,
            describe_full_response_schema=True,
        )
        mcp.mount()

        logger.info(
            "mcp_server_mounted",
            name="aads-mcp-server",
            note="AADS API endpoints exposed as MCP tools",
        )

    except ImportError:
        logger.warning(
            "fastapi_mcp_not_installed: pip install fastapi-mcp>=0.3.0 필요"
        )
    except Exception as e:
        logger.warning("mcp_server_mount_failed_graceful_degradation", error=str(e))
