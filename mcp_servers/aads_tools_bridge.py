"""
AADS Tools MCP Bridge — stdio transport (low-level MCP Server).
Claude Code CLI에서 MCP 서버로 실행되어 AADS의 55개 도구를 Claude에게 노출.

실행: docker exec -i aads-server python -m mcp_servers.aads_tools_bridge

환경변수:
  AADS_SESSION_ID: 현재 채팅 세션 ID (도구 실행 시 전달)
  DATABASE_URL: PostgreSQL 연결 문자열
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("aads_tools_bridge")

# sys.path에 /app 추가 (컨테이너 내 app 모듈 접근)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

server = Server("aads-tools")

# 지연 임포트 캐시
_execute_tool = None
_tool_definitions = None
_db_initialized = False


async def _ensure_db():
    """DB pool 초기화 (최초 1회)."""
    global _db_initialized
    if _db_initialized:
        return
    try:
        from app.core.db_pool import init_pool
        dsn = os.getenv("DATABASE_URL", "")
        if dsn:
            await init_pool(dsn)
            _db_initialized = True
            logger.info("DB pool initialized")
    except Exception as e:
        logger.warning(f"DB pool init failed (non-fatal): {e}")
        _db_initialized = True


def _get_tool_definitions():
    """TOOL_DEFINITIONS 로드 (지연)."""
    global _tool_definitions
    if _tool_definitions is None:
        from app.api.ceo_chat_tools import TOOL_DEFINITIONS
        _tool_definitions = TOOL_DEFINITIONS
    return _tool_definitions


async def _call_tool(name: str, params: dict) -> str:
    """도구 실행 래퍼."""
    global _execute_tool
    if _execute_tool is None:
        from app.api.ceo_chat_tools import execute_tool
        _execute_tool = execute_tool

    await _ensure_db()

    session_id = os.getenv("AADS_SESSION_ID", "")
    dsn = os.getenv("DATABASE_URL", "")
    try:
        result = await _execute_tool(name, params, dsn, session_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"Tool {name} error: {e}")
        return json.dumps({"error": str(e), "tool": name}, ensure_ascii=False)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """TOOL_DEFINITIONS → MCP Tool 리스트 변환."""
    defs = _get_tool_definitions()
    tools = []
    for td in defs:
        tools.append(types.Tool(
            name=td["name"],
            description=td.get("description", ""),
            inputSchema=td.get("input_schema", {"type": "object", "properties": {}}),
        ))
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """도구 호출 → execute_tool 실행 → 결과 반환."""
    logger.info(f"call_tool: {name} args={json.dumps(arguments, ensure_ascii=False)[:200]}")
    result = await _call_tool(name, arguments)
    return [types.TextContent(type="text", text=result)]


async def main():
    """stdio 모드로 MCP 서버 실행."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
