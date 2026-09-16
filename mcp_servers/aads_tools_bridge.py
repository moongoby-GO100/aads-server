"""
AADS Tools MCP Bridge — stdio transport (low-level MCP Server).
Claude Code CLI에서 MCP 서버로 실행되어 AADS의 55개 도구를 Claude에게 노출.

실행:
  - docker exec -i aads-server python3 -m mcp_servers.aads_tools_bridge
  - python3.11 -m mcp_servers.aads_tools_bridge

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
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("aads_tools_bridge")

# structlog가 stdout에 쓰면 MCP JSON-RPC 전송이 깨진다 — stderr로 강제 지정
import structlog
structlog.configure(
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

# 컨테이너(`/app`)와 호스트 직접 실행(`python3.11 -m ...`)을 모두 지원한다.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (Path("/app"), _REPO_ROOT):
    _candidate_str = str(_candidate)
    if _candidate.exists() and _candidate_str not in sys.path:
        sys.path.insert(0, _candidate_str)

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
            # init_pool() reads DATABASE_URL from the environment.
            await init_pool()
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
    """TOOL_DEFINITIONS 로드 (지연).

    Legacy ``ceo_chat_tools.TOOL_DEFINITIONS`` is not the canonical full
    catalog anymore. Merge ToolRegistry so MCP clients can discover newer
    tools such as search_crawl_match/search_searxng while preserving legacy
    aliases and schemas.
    """
    global _tool_definitions
    if _tool_definitions is None:
        from app.api.ceo_chat_tools import TOOL_DEFINITIONS
        merged: dict[str, dict] = {
            str(td.get("name")): dict(td)
            for td in TOOL_DEFINITIONS
            if isinstance(td, dict) and td.get("name")
        }
        try:
            from app.services.tool_registry import ToolRegistry

            registry = ToolRegistry()
            exclude_keys = {"input_examples", "defer_loading", "allowed_callers"}
            for name in registry.list_all():
                td = registry.get_tool(name)
                if not isinstance(td, dict) or not td.get("name"):
                    continue
                tool_type = str(td.get("type", "") or "")
                if tool_type and tool_type != "tool":
                    continue
                normalized = {k: v for k, v in td.items() if k not in exclude_keys}
                merged.setdefault(str(normalized["name"]), normalized)
        except Exception as exc:
            logger.warning("tool_registry_merge_failed: %s", exc)
        _tool_definitions = list(merged.values())
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


_SESSION_BOUND_TOOLS = {
    "pipeline_runner_submit",
    "pipeline_runner_submit_batch",
    "pipeline_c_start",
    "pipeline_runner_status",
    "check_task_status",
    "check_directive_status",
    # 담당끼리 묻는 길. 원세션을 잃으면 질문 자체가 나가지 못한다 —
    # env·contextvar 말고도 도구 입력으로 한 번 더 실어 보낸다.
    "ask_session",
}

_DB_TOOLS = {
    "query_database",
    "query_db",
    "query_project_database",
    "list_project_databases",
}
_MCP_DB_TOOL_DEADLINE_SECONDS = float(os.getenv("AADS_MCP_DB_TOOL_DEADLINE_SECONDS", "28"))


def _bind_aads_chat_session(name: str, params: dict) -> tuple[dict, str]:
    """Bind the MCP subprocess to the AADS chat tab that launched it."""
    bound = dict(params or {})
    session_id = str(os.getenv("AADS_SESSION_ID", "") or "").strip()
    if not session_id or name not in _SESSION_BOUND_TOOLS:
        return bound, session_id

    scope = str(bound.get("scope") or "").strip().lower()
    if scope in {"all", "global", "*"}:
        return bound, session_id

    supplied_session = str(bound.get("session_id") or "").strip()
    if name in {"pipeline_runner_submit", "pipeline_runner_submit_batch", "pipeline_c_start"}:
        if supplied_session and supplied_session != session_id:
            logger.warning(
                "mcp_runner_session_override: tool=%s supplied=%s current=%s",
                name,
                supplied_session[:8],
                session_id[:8],
            )
        bound["session_id"] = session_id
    elif not supplied_session:
        bound["session_id"] = session_id
    return bound, session_id


async def _call_tool(name: str, params: dict) -> str:
    """도구 실행 래퍼 — ToolExecutor 우선, execute_tool 폴백.

    TOOL_NAME_MAP으로 MCP 이름 → ToolExecutor dispatch 이름 변환 후 실행.
    ToolExecutor가 unknown_tool 반환 시 execute_tool 폴백.
    """
    await _ensure_db()

    # MCP 이름 → ToolExecutor dispatch 이름 변환
    dispatch_name = TOOL_NAME_MAP.get(name, name)
    bound_params, session_id = _bind_aads_chat_session(name, params)

    # 1순위: ToolExecutor (dispatch 이름으로 실행)
    token = None
    try:
        from app.services.tool_executor import ToolExecutor, current_chat_session_id

        if session_id:
            token = current_chat_session_id.set(session_id)
        executor = ToolExecutor()
        result = await executor.execute(dispatch_name, bound_params)
        result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

        # unknown_tool 반환 시 execute_tool 폴백으로 전환
        if "unknown_tool" in result_str:
            logger.info(f"ToolExecutor unknown_tool: {dispatch_name} (mcp: {name}), trying execute_tool fallback")
        else:
            return result_str
    except Exception as e1:
        logger.debug(f"ToolExecutor error for {dispatch_name} (mcp: {name}): {e1}")
    finally:
        if token is not None:
            try:
                current_chat_session_id.reset(token)
            except Exception:
                pass

    # 2순위: execute_tool (레거시 폴백 — 원래 MCP 이름으로 실행)
    try:
        from app.api.ceo_chat_tools import execute_tool
        dsn = os.getenv("DATABASE_URL", "")
        result = await execute_tool(name, bound_params, dsn, session_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e2:
        logger.error(f"Tool {name} error: {e2}")
        return json.dumps({"error": str(e2), "tool": name}, ensure_ascii=False)


# 상주 도구 — 최근 7일 호출 통계 상위 10개. 합쳐서 호출의 91% 를 덮는다.
# 사람이 고른 것이 아니라 통계가 정한 것이고, 주 1회 재계산 대상이다.
# 설계: aads-docs/docs/PRD-TOOL-RESIDENT-SET-v1.0.md
_DEFAULT_RESIDENT_TOOLS = (
    "run_remote_command",      # 57.1%
    "query_database",          #  7.6%
    "query_project_database",  #  7.1%
    "read_remote_file",        #  6.5%
    "pipeline_runner_status",  #  3.4%
    "todo_write",              #  3.1%
    "patch_remote_file",       #  2.3%
    "handover_write",          #  1.0%
    "write_remote_file",       #  0.9%
    "read_task_logs",          #  0.6%
)


def _resident_tool_names() -> set[str]:
    raw = os.getenv("AADS_TOOL_RESIDENT_LIST", "").strip()
    if raw:
        return {n.strip() for n in raw.split(",") if n.strip()}
    return set(_DEFAULT_RESIDENT_TOOLS)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """TOOL_DEFINITIONS → MCP Tool 리스트 변환.

    기본은 전량 노출이다. AADS_TOOL_RESIDENT_MODE=1 이면 상주 집합만 스키마를
    내보내고, 나머지는 **이름만** 한 줄로 알려준다.

    왜 이렇게 하나 — 도구 142개 스키마가 76,245자(약 25,000토큰)이고, 질문
    내용과 무관하게 매 턴 실린다. 그런데 최근 7일 41,875회 호출에서 상위 8개가
    90.1% 를 차지했고 47개는 한 번도 쓰이지 않았다.

    이름 목록을 함께 주는 이유는 모델이 "그런 도구가 있는지" 를 검색 없이 알게
    하기 위해서다. 이름만이면 2,330자로 싸다. 설명까지 붙이면 16,800자가 되어
    상주 집합(7,443자)보다 비싸지므로 넣지 않는다.

    끄는 법: AADS_TOOL_RESIDENT_MODE=0 (기본값). 기존 동작 그대로다.
    """
    defs = _get_tool_definitions()
    tools = []
    for td in defs:
        tools.append(types.Tool(
            name=td["name"],
            description=td.get("description", ""),
            inputSchema=td.get("input_schema", {"type": "object", "properties": {}}),
        ))

    if os.getenv("AADS_TOOL_RESIDENT_MODE", "0").strip() != "1":
        return tools

    resident = _resident_tool_names()
    kept = [t for t in tools if t.name in resident]
    deferred = sorted(t.name for t in tools if t.name not in resident)
    if not kept:
        # 상주 목록이 잘못 설정돼 하나도 안 남으면 전량으로 되돌린다.
        # 도구가 통째로 사라지는 것보다 토큰을 더 쓰는 편이 낫다.
        logger.warning("resident_mode_empty_match — 전량 노출로 폴백")
        return tools

    if deferred:
        kept.append(types.Tool(
            name="list_deferred_tools",
            description=(
                "이 세션에 스키마가 실리지 않은 도구 이름 목록. 상주 도구로 처리할 수 "
                "없으면 먼저 이 목록에서 이름을 찾고, ToolSearch 로 스키마를 불러와 "
                "호출해라. '도구가 없다' 고 답하기 전에 반드시 여기를 본다.\n"
                "사용 가능한 이름: " + ", ".join(deferred)
            ),
            inputSchema={"type": "object", "properties": {}},
        ))
    logger.info(
        "resident_mode_on: 상주 %d개 · 이름만 %d개 (전체 %d개)",
        len(kept) - (1 if deferred else 0), len(deferred), len(tools),
    )
    return kept


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """도구 호출 → execute_tool 실행 → 결과 반환. 모든 예외를 잡아 MCP 연결 보호."""
    logger.info(f"call_tool: {name} args={json.dumps(arguments, ensure_ascii=False)[:200]}")
    try:
        if name in _DB_TOOLS:
            result = await asyncio.wait_for(
                _call_tool(name, arguments),
                timeout=max(0.001, _MCP_DB_TOOL_DEADLINE_SECONDS),
            )
        else:
            result = await _call_tool(name, arguments)
    except asyncio.TimeoutError:
        logger.warning(
            "call_tool DB deadline exceeded: tool=%s timeout_seconds=%.1f",
            name,
            _MCP_DB_TOOL_DEADLINE_SECONDS,
        )
        result = json.dumps(
            {
                "error": "DB MCP tool deadline exceeded before client disconnect",
                "error_code": "mcp_db_tool_deadline",
                "tool": name,
                "timeout_seconds": _MCP_DB_TOOL_DEADLINE_SECONDS,
                "hint": "쿼리 범위를 줄이거나 query_database timeout_policy를 확인하십시오.",
            },
            ensure_ascii=False,
        )
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
