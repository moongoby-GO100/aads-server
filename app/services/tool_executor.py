"""
AADS-185: 도구 실행기 — Anthropic Tool Use API 도구 실행
10초 타임아웃, 결과 2000토큰(~6000자) 제한.
기존 chat_tools.py의 함수를 래핑.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
_AADS_API_BASE = os.getenv("AADS_API_BASE", "http://localhost:8080")

_MAX_RESULT_CHARS = 6000  # ~2000 토큰
_TOOL_TIMEOUT = 10.0


class ToolExecutor:
    """단일 도구 실행 + 타임아웃 + 결과 제한."""

    async def execute(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """
        도구 실행. 10초 타임아웃, 결과 6000자 제한.
        실패 시 JSON error 반환.
        """
        try:
            result = await asyncio.wait_for(
                self._dispatch(tool_name, tool_input),
                timeout=_TOOL_TIMEOUT,
            )
            result_str = (
                json.dumps(result, ensure_ascii=False, indent=2)
                if not isinstance(result, str)
                else result
            )
            if len(result_str) > _MAX_RESULT_CHARS:
                result_str = result_str[:_MAX_RESULT_CHARS] + "\n...[결과 일부 생략]"
            return result_str
        except asyncio.TimeoutError:
            logger.warning(f"tool_executor timeout: tool={tool_name}")
            return json.dumps({"error": "timeout", "tool": tool_name})
        except Exception as e:
            logger.error(f"tool_executor error: tool={tool_name} error={e}")
            return json.dumps({"error": str(e), "tool": tool_name})

    async def _dispatch(self, tool_name: str, tool_input: Dict[str, Any]) -> Any:
        """도구 이름 → 실제 함수 매핑."""
        dispatch = {
            "health_check":     self._health_check,
            "dashboard_query":  self._dashboard_query,
            "task_history":     self._task_history,
            "server_status":    self._server_status,
            "directive_create": self._directive_create,
            "read_github_file": self._read_github_file,
            "query_database":   self._query_database,
            "read_remote_file": self._read_remote_file,
            "cost_report":      self._cost_report,
            "web_search_brave": self._web_search_brave,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return {"error": f"unknown_tool: {tool_name}"}
        return await fn(tool_input)

    # ── system 도구 ─────────────────────────────────────────────────────────

    async def _health_check(self, inp: Dict[str, Any]) -> Any:
        try:
            from app.services.chat_tools import health_check
            return await health_check("", "")
        except ImportError:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(f"{_AADS_API_BASE}/api/v1/ops/full-health")
                return r.json() if r.status_code == 200 else {"error": f"status {r.status_code}"}

    async def _dashboard_query(self, inp: Dict[str, Any]) -> Any:
        try:
            from app.services.chat_tools import dashboard_query
            return await dashboard_query("", "")
        except ImportError:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(f"{_AADS_API_BASE}/api/v1/ops/pipeline-status")
                return r.json() if r.status_code == 200 else {"error": f"status {r.status_code}"}

    async def _task_history(self, inp: Dict[str, Any]) -> Any:
        limit = min(inp.get("limit", 10), 50)
        project = inp.get("project", "")
        try:
            import asyncpg
            db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
            conn = await asyncpg.connect(db_url, timeout=8)
            try:
                if project:
                    rows = await conn.fetch(
                        "SELECT task_id, title, status, completed_at FROM directive_lifecycle "
                        "WHERE task_id ILIKE $1 ORDER BY completed_at DESC LIMIT $2",
                        f"{project}%", limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT task_id, title, status, completed_at FROM directive_lifecycle "
                        "ORDER BY completed_at DESC LIMIT $1",
                        limit,
                    )
                return [dict(r) for r in rows]
            finally:
                await conn.close()
        except Exception as e:
            return {"error": str(e)}

    async def _server_status(self, inp: Dict[str, Any]) -> Any:
        try:
            from app.services.chat_tools import health_check
            return await health_check("", "")
        except Exception as e:
            return {"error": str(e)}

    # ── action 도구 ─────────────────────────────────────────────────────────

    async def _directive_create(self, inp: Dict[str, Any]) -> str:
        task_id = inp.get("task_id", "AADS-XXX")
        title = inp.get("title", "")
        priority = inp.get("priority", "P2-MEDIUM")
        size = inp.get("size", "M")
        model = inp.get("model", "sonnet")
        description = inp.get("description", "")
        depends_on = inp.get("depends_on", "none")
        return (
            f">>>DIRECTIVE_START\n"
            f"TASK_ID: {task_id}\n"
            f"TITLE: {title}\n"
            f"PRIORITY: {priority}\n"
            f"SIZE: {size}\n"
            f"MODEL: {model}\n"
            f"DEPENDS_ON: {depends_on}\n"
            f"ASSIGNEE: Claude (서버 68, /root/aads)\n"
            f"\nDESCRIPTION: {description}\n"
            f">>>DIRECTIVE_END"
        )

    async def _read_github_file(self, inp: Dict[str, Any]) -> Any:
        try:
            from app.services.chat_tools import read_github_file
            query = f"repo={inp.get('repo', '')} path={inp.get('path', '')} branch={inp.get('branch', 'main')}"
            return await read_github_file(query, "")
        except ImportError:
            repo = inp.get("repo", "moongoby-GO100/aads-docs")
            path = inp.get("path", "HANDOVER.md")
            branch = inp.get("branch", "main")
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(url)
                if r.status_code == 200:
                    return r.text[:3000]
                return {"error": f"github {r.status_code}: {url}"}

    async def _query_database(self, inp: Dict[str, Any]) -> Any:
        query = inp.get("query", "")
        limit = min(inp.get("limit", 20), 100)
        if not query.strip().upper().startswith("SELECT"):
            return {"error": "SELECT 쿼리만 허용됩니다"}
        try:
            import asyncpg
            db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
            conn = await asyncpg.connect(db_url, timeout=8)
            try:
                if "LIMIT" not in query.upper():
                    query = query.rstrip(";") + f" LIMIT {limit}"
                rows = await conn.fetch(query)
                return [dict(r) for r in rows]
            finally:
                await conn.close()
        except Exception as e:
            return {"error": str(e)}

    async def _read_remote_file(self, inp: Dict[str, Any]) -> Any:
        try:
            from app.services.chat_tools import read_remote_file
            return await read_remote_file(inp.get("path", ""), "")
        except Exception as e:
            return {"error": str(e)}

    async def _cost_report(self, inp: Dict[str, Any]) -> Any:
        days = inp.get("days", 7)
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(
                    f"{LITELLM_BASE_URL}/api/spend/logs",
                    headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
                    params={"days": days},
                )
                return r.json() if r.status_code == 200 else {"error": f"status {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    async def _web_search_brave(self, inp: Dict[str, Any]) -> Any:
        from app.services.brave_search_service import BraveSearchService
        svc = BraveSearchService()
        result = await svc.search(inp.get("query", ""), count=inp.get("count", 5))
        return {"text": result.text, "citations": result.citations}


# ─── 하위 호환성 ─────────────────────────────────────────────────────────────

_INTENT_TOOL_MAP: Dict[str, list] = {
    "health_check":     ["health_check"],
    "dashboard":        ["dashboard_query"],
    "diagnosis":        ["dashboard_query", "health_check"],
    "search":           ["web_search_brave"],
    "memory_recall":    ["read_github_file", "query_database"],
    "directive_gen":    ["directive_create"],
    "execute":          ["directive_create"],
    "workspace_switch": ["dashboard_query"],
    "qa":               ["read_remote_file"],
    "execution_verify": ["read_remote_file"],
    "task_history":     ["task_history"],
    "cost_report":      ["cost_report"],
    "system_status":    ["health_check", "dashboard_query"],
    "url_analyze":      ["read_github_file"],
}


async def execute_tools(intent: str, message: str, workspace_id: str) -> str:
    tool_names = _INTENT_TOOL_MAP.get(intent, [])
    if not tool_names:
        return ""
    executor = ToolExecutor()
    parts = []
    for name in tool_names:
        result = await executor.execute(name, {"message": message, "workspace_id": workspace_id})
        parts.append(f"[{name}]\n{result}")
    return "\n\n".join(parts)


def build_tool_injection(tool_result: str) -> str:
    if not tool_result:
        return ""
    return "[시스템 도구 조회 결과 — 아래 데이터를 기반으로 정확하게 답변하세요]\n\n" + tool_result


def has_tools_for_intent(intent: str) -> bool:
    return bool(_INTENT_TOOL_MAP.get(intent))
