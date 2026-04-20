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


async def _heartbeat(interval: float = 30.0):
    """stderr에 주기적으로 heartbeat 출력 — 파이프 단절 조기 감지."""
    while True:
        try:
            await asyncio.sleep(interval)
            sys.stderr.write("")
            sys.stderr.flush()
        except (BrokenPipeError, OSError):
            logger.warning("Heartbeat: pipe broken detected")
            raise BrokenPipeError("heartbeat detected broken pipe")
        except asyncio.CancelledError:
            break


def _get_tool_definitions():
    """TOOL_DEFINITIONS 로드 (지연)."""
    global _tool_definitions
    if _tool_definitions is None:
        from app.api.ceo_chat_tools import TOOL_DEFINITIONS
        _tool_definitions = TOOL_DEFINITIONS
    return _tool_definitions


# MCP 도구 이름 → ToolExecutor dispatch 이름 매핑
TOOL_NAME_MAP: dict[str, str] = {
    "read_github": "read_github_file",
    "query_db": "query_database",
    "search_naver": "web_search_naver",
    "search_naver_multi": "web_search_naver",
    "search_kakao": "web_search_kakao",
    "execute_sandbox": "code_sandbox",
    "gemini_grounding_search": "gemini_search",
    "visual_qa_test": "visual_qa",
    "evaluate_alerts": "alert_evaluate",
    "send_alert_message": "alert_send",
}


async def _call_tool(name: str, params: dict) -> str:
    """도구 실행 래퍼 — ToolExecutor 우선, execute_tool 폴백.

    TOOL_NAME_MAP으로 MCP 이름 → ToolExecutor dispatch 이름 변환 후 실행.
    ToolExecutor가 unknown_tool 반환 시 execute_tool 폴백.
    """
    await _ensure_db()

    # MCP 이름 → ToolExecutor dispatch 이름 변환
    dispatch_name = TOOL_NAME_MAP.get(name, name)

    # 1순위: ToolExecutor (dispatch 이름으로 실행)
    try:
        from app.services.tool_executor import ToolExecutor
        executor = ToolExecutor()
        result = await executor.execute(dispatch_name, params)
        result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

        # unknown_tool 반환 시 execute_tool 폴백으로 전환
        if "unknown_tool" in result_str:
            logger.info(f"ToolExecutor unknown_tool: {dispatch_name} (mcp: {name}), trying execute_tool fallback")
        else:
            return result_str
    except Exception as e1:
        logger.debug(f"ToolExecutor error for {dispatch_name} (mcp: {name}): {e1}")

    # 2순위: execute_tool (레거시 폴백 — 원래 MCP 이름으로 실행)
    try:
        from app.api.ceo_chat_tools import execute_tool
        session_id = os.getenv("AADS_SESSION_ID", "")
        dsn = os.getenv("DATABASE_URL", "")
        result = await execute_tool(name, params, dsn, session_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e2:
        logger.error(f"Tool {name} error: {e2}")
        return json.dumps({"error": str(e2), "tool": name}, ensure_ascii=False)


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
    """도구 호출 → execute_tool 실행 → 결과 반환. 모든 예외를 잡아 MCP 연결 보호."""
    logger.info(f"call_tool: {name} args={json.dumps(arguments, ensure_ascii=False)[:200]}")
    try:
        result = await _call_tool(name, arguments)
    except asyncio.CancelledError:
        logger.warning(f"call_tool CANCELLED: {name}")
        result = json.dumps({"error": "cancelled", "tool": name}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"call_tool UNHANDLED: {name} error={e}")
        result = json.dumps({"error": str(e), "tool": name}, ensure_ascii=False)
    return [types.TextContent(type="text", text=result)]


async def main():
    """stdio 모드로 MCP 서버 실행. 끊김 시 자동 재시작 (최대 5회)."""
    global _db_initialized

    max_retries = 5
    retry_delay = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            logger.warning(f"MCP server starting (attempt {attempt}/{max_retries})")
            async with stdio_server() as (read_stream, write_stream):
                await _ensure_db()
                heartbeat_task = asyncio.create_task(_heartbeat(30.0))
                try:
                    await server.run(
                        read_stream,
                        write_stream,
                        server.create_initialization_options(),
                    )
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
            logger.warning("MCP server: clean shutdown")
            break
        except (BrokenPipeError, ConnectionResetError, EOFError) as e:
            logger.warning(f"MCP server pipe broken (attempt {attempt}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)
                _db_initialized = False
                continue
            logger.error("MCP server: max retries exceeded, exiting")
            sys.exit(1)
        except asyncio.CancelledError:
            logger.warning("MCP server: cancelled, shutting down gracefully")
            break
        except Exception as e:
            logger.error(f"MCP server unexpected error (attempt {attempt}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)
                _db_initialized = False
                continue
            logger.error("MCP server: max retries exceeded, exiting")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
