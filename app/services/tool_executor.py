"""
AADS-186A: 도구 실행기 — Anthropic Tool Use API 도구 실행
10초 타임아웃, 결과 2000토큰(~6000자) 제한 (기본값, 실제 25,000 토큰 허용).
신규 워크플로우 도구: inspect_service, get_all_service_status, generate_directive
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

_MAX_RESULT_CHARS = 25000  # ~8000 토큰 (지시서 기준 25,000 허용)
_TOOL_TIMEOUT = 20.0  # 워크플로우 도구(inspect_service 등)는 더 오래 걸릴 수 있음


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
            "health_check":           self._health_check,
            "dashboard_query":        self._dashboard_query,
            "task_history":           self._task_history,
            "server_status":          self._server_status,
            "directive_create":       self._directive_create,
            "read_github_file":       self._read_github_file,
            "query_database":         self._query_database,
            "read_remote_file":       self._read_remote_file,
            "list_remote_dir":        self._list_remote_dir,
            "cost_report":            self._cost_report,
            "web_search_brave":       self._web_search_brave,
            # AADS-186A 신규 워크플로우 도구
            "inspect_service":        self._inspect_service,
            "get_all_service_status": self._get_all_service_status,
            "generate_directive":     self._generate_directive,
            # AADS-186E-1 크롤링 도구
            "jina_read":              self._jina_read,
            "crawl4ai_fetch":         self._crawl4ai_fetch,
            "deep_crawl":             self._deep_crawl,
            # 하위호환: fetch_url → jina_read 내부 리다이렉트 (AADS-186E-1 제약)
            "fetch_url":              self._jina_read,
            # AADS-186E-2: 메모리 도구
            "save_note":              self._save_note,
            "recall_notes":           self._recall_notes,
            "learn_pattern":          self._learn_pattern,
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
        """원격 서버 파일 읽기 (SSH, 프로젝트별 서버 매핑)."""
        project = (inp.get("project") or "").upper()
        path = inp.get("path") or inp.get("file_path") or ""
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        if not path:
            return {"error": "path 또는 file_path 필수"}
        try:
            from app.api.ceo_chat_tools import tool_read_remote_file
            return await tool_read_remote_file(project, path)
        except Exception as e:
            return {"error": str(e)}

    async def _list_remote_dir(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 디렉터리/파일 검색 (SSH)."""
        project = (inp.get("project") or "").upper()
        path = inp.get("path", "")
        keyword = inp.get("keyword", "")
        max_depth = min(int(inp.get("max_depth", 3)), 5)
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        try:
            from app.api.ceo_chat_tools import tool_list_remote_dir
            return await tool_list_remote_dir(project, path, keyword, max_depth)
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

    # ── AADS-186A 신규 워크플로우 도구 ──────────────────────────────────────

    async def _inspect_service(self, inp: Dict[str, Any]) -> Any:
        """
        서비스 종합 점검: 프로세스/Docker/로그/헬스체크 수행.
        list_remote_dir + read_remote_file + health_check 조합.
        """
        project = (inp.get("project") or "").upper()
        checks_input = inp.get("checks", ["all"])
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}

        do_all = "all" in checks_input
        do_process = do_all or "process" in checks_input
        do_docker = do_all or "docker" in checks_input
        do_log_tail = do_all or "log_tail" in checks_input
        do_health = do_all or "health" in checks_input

        results: Dict[str, Any] = {"project": project, "checks_performed": []}

        try:
            from app.api.ceo_chat_tools import tool_list_remote_dir, tool_read_remote_file
        except ImportError:
            tool_list_remote_dir = None
            tool_read_remote_file = None

        if do_process and tool_list_remote_dir:
            try:
                proc_result = await asyncio.wait_for(
                    tool_list_remote_dir(project, "", "*.py", 2),
                    timeout=8.0,
                )
                results["process_files"] = proc_result
                results["checks_performed"].append("process")
            except Exception as e:
                results["process_error"] = str(e)

        if do_docker:
            try:
                docker_result = await self._health_check({"server": "all"})
                results["docker_status"] = docker_result
                results["checks_performed"].append("docker")
            except Exception as e:
                results["docker_error"] = str(e)

        if do_log_tail and tool_list_remote_dir:
            try:
                log_result = await asyncio.wait_for(
                    tool_list_remote_dir(project, "", "*.log", 3),
                    timeout=8.0,
                )
                results["log_files"] = log_result
                results["checks_performed"].append("log_tail")
            except Exception as e:
                results["log_error"] = str(e)

        if do_health:
            try:
                health_result = await self._health_check({})
                results["health"] = health_result
                results["checks_performed"].append("health")
            except Exception as e:
                results["health_error"] = str(e)

        return results

    async def _get_all_service_status(self, inp: Dict[str, Any]) -> Any:
        """
        6개 서비스 헬스체크 병렬 수행 → 마크다운 테이블 반환.
        """
        include_details = inp.get("include_details", False)

        # 헬스체크 URL 정의
        services = {
            "AADS": f"{_AADS_API_BASE}/api/v1/ops/health-check",
            "KIS":  "http://211.188.51.113:8082/health",
            "GO100":"http://211.188.51.113:8083/health",
            "SF":   "http://116.120.58.155:7916/health",
            "NTV2": "http://116.120.58.155:8080/health",
            "NAS":  "http://cafe24-nas-placeholder/health",
        }

        async def check_one(name: str, url: str) -> Dict[str, Any]:
            import time
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.get(url)
                    elapsed = round((time.monotonic() - start) * 1000)
                    status = "✅ UP" if r.status_code < 400 else f"⚠️ {r.status_code}"
                    result = {"service": name, "status": status, "response_ms": elapsed}
                    if include_details:
                        try:
                            result["detail"] = r.json()
                        except Exception:
                            result["detail"] = r.text[:200]
                    return result
            except Exception as e:
                elapsed = round((time.monotonic() - start) * 1000)
                return {"service": name, "status": "❌ DOWN", "response_ms": elapsed, "error": str(e)[:100]}

        tasks = [check_one(name, url) for name, url in services.items()]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        # 마크다운 테이블 생성
        lines = ["| 서비스 | 상태 | 응답(ms) |", "|--------|------|----------|"]
        for r in results:
            lines.append(f"| {r['service']} | {r['status']} | {r.get('response_ms', '-')} |")
        table = "\n".join(lines)

        if include_details:
            return {"table": table, "details": results}
        return {"table": table, "summary": results}

    async def _generate_directive(self, inp: Dict[str, Any]) -> Any:
        """
        자연어 설명 → AADS 형식 지시서 자동 생성.
        TASK_ID 자동 채번, auto_submit=true 시 API 제출.
        """
        description = inp.get("description", "")
        priority = inp.get("priority", "P1-HIGH")
        size = inp.get("size", "M")
        project = (inp.get("project") or "AADS").upper()
        auto_submit = inp.get("auto_submit", False)

        if not description:
            return {"error": "description 필수"}

        # TASK_ID 채번: DB에서 최대 번호 조회
        task_id = f"{project}-NEW"
        try:
            import asyncpg
            db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
            conn = await asyncpg.connect(db_url, timeout=8)
            try:
                row = await conn.fetchrow(
                    "SELECT task_id FROM directive_lifecycle "
                    "WHERE task_id ILIKE $1 ORDER BY created_at DESC LIMIT 1",
                    f"{project}-%",
                )
                if row:
                    last_id = row["task_id"]
                    parts = last_id.split("-")
                    if len(parts) == 2 and parts[1].isdigit():
                        next_num = int(parts[1]) + 1
                        task_id = f"{project}-{next_num}"
            finally:
                await conn.close()
        except Exception as e:
            logger.warning(f"generate_directive task_id lookup failed: {e}")

        # 모델 라우팅 (size 기반)
        size_model = {"XS": "haiku", "S": "sonnet", "M": "sonnet", "L": "opus", "XL": "opus"}
        model = size_model.get(size, "sonnet")

        # 지시서 생성
        directive_block = (
            f">>>DIRECTIVE_START\n"
            f"TASK_ID: {task_id}\n"
            f"TITLE: {description[:60]}\n"
            f"PRIORITY: {priority}\n"
            f"SIZE: {size}\n"
            f"MODEL: {model}\n"
            f"ASSIGNEE: Claude (서버 68, /root/aads)\n"
            f"\nDESCRIPTION:\n{description}\n"
            f">>>DIRECTIVE_END"
        )

        result: Dict[str, Any] = {
            "task_id": task_id,
            "directive": directive_block,
            "auto_submit": auto_submit,
        }

        if auto_submit:
            try:
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.post(
                        f"{_AADS_API_BASE}/api/v1/directives/submit",
                        json={
                            "task_id": task_id,
                            "content": directive_block,
                            "priority": priority,
                        },
                    )
                    result["submit_status"] = r.status_code
                    result["submit_response"] = r.json() if r.status_code < 400 else r.text[:200]
            except Exception as e:
                result["submit_error"] = str(e)

        return result

    # ── AADS-186E-1 크롤링 도구 ────────────────────────────────────────────

    async def _jina_read(self, inp: Dict[str, Any]) -> Any:
        """Jina Reader API로 URL → 마크다운 변환. 실패 시 crawl4ai 폴백."""
        url = inp.get("url", "")
        max_tokens = int(inp.get("max_tokens", 25000))
        if not url:
            return {"error": "url 필수"}
        from app.services.jina_reader_service import JinaReaderService
        jina = JinaReaderService()
        result = await jina.read_url(url, max_tokens=max_tokens)
        if result and result.content:
            return {
                "title": result.title,
                "content": result.content,
                "word_count": result.word_count,
                "source_url": result.source_url,
                "truncated": result.truncated,
            }
        # crawl4ai 폴백
        logger.info(f"jina_read fallback to crawl4ai: {url}")
        from app.services.crawl4ai_service import Crawl4AIService
        c4 = Crawl4AIService()
        c4_result = await c4.fetch_page(url)
        if c4_result and c4_result.content:
            return {
                "title": url,
                "content": c4_result.content,
                "word_count": c4_result.word_count,
                "source_url": url,
                "via": "crawl4ai_fallback",
            }
        return {"error": f"jina_read 및 crawl4ai 모두 실패: {url}"}

    async def _crawl4ai_fetch(self, inp: Dict[str, Any]) -> Any:
        """Crawl4AI Docker 서버로 JS 렌더링 포함 크롤링."""
        url = inp.get("url", "")
        js_render = bool(inp.get("js_render", True))
        if not url:
            return {"error": "url 필수"}
        from app.services.crawl4ai_service import Crawl4AIService
        c4 = Crawl4AIService()
        result = await c4.fetch_page(url, js_render=js_render)
        if result is None:
            return {"error": "crawl4ai 서버 미가용 — docker-compose.crawl4ai.yml로 배포 필요"}
        if result.error:
            return {"error": result.error, "url": url}
        return {
            "url": result.url,
            "content": result.content,
            "word_count": result.word_count,
            "js_rendered": result.js_rendered,
        }

    async def _deep_crawl(self, inp: Dict[str, Any]) -> Any:
        """검색 → 다중 크롤링 → 종합 요약 파이프라인."""
        query = inp.get("query", "")
        max_pages = min(int(inp.get("max_pages", 5)), 10)
        summarize = bool(inp.get("summarize", True))
        if not query:
            return {"error": "query 필수"}
        from app.services.deep_crawl_service import DeepCrawlService
        svc = DeepCrawlService()
        result = await svc.research_crawl(query, max_pages=max_pages, summarize=summarize)
        return {
            "query": result.query,
            "synthesis": result.synthesis,
            "citations": result.citations,
            "pages_crawled": result.pages_crawled,
            "pages_failed": result.pages_failed,
            "error": result.error,
        }

    # ── AADS-186E-2: 메모리 도구 ─────────────────────────────────────────────

    async def _save_note(self, inp: Dict[str, Any]) -> Any:
        """세션 노트 저장 — session_notes 테이블에 INSERT."""
        summary = inp.get("summary", "")
        if not summary:
            return {"error": "summary 필수"}
        key_decisions = inp.get("key_decisions", [])
        action_items = inp.get("action_items", [])
        unresolved_issues = inp.get("unresolved_issues", [])

        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        note = await mgr.save_session_note(
            session_id="tool_call",
            messages=[],
            summary=summary,
            key_decisions=key_decisions,
            action_items=action_items,
            unresolved_issues=unresolved_issues,
        )
        return {
            "status": "saved",
            "note_id": note.id,
            "summary": note.summary,
        }

    async def _recall_notes(self, inp: Dict[str, Any]) -> Any:
        """최근 세션 노트 검색 — session_notes 테이블 조회."""
        count = min(int(inp.get("count", 5)), 20)
        query = inp.get("query", "")

        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()

        if query:
            # 쿼리 있으면 recall (메타메모리 검색)
            memories = await mgr.recall(query=query)
            return [
                {
                    "category": m.category,
                    "key": m.key,
                    "value": m.value,
                    "confidence": m.confidence,
                }
                for m in memories[:count]
            ]
        else:
            # 최근 노트 반환
            notes = await mgr.get_recent_notes(count)
            return [
                {
                    "session_id": n.session_id,
                    "summary": n.summary,
                    "key_decisions": n.key_decisions,
                    "action_items": n.action_items,
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                for n in notes
            ]

    async def _learn_pattern(self, inp: Dict[str, Any]) -> Any:
        """패턴 학습 — ai_meta_memory UPSERT."""
        category = inp.get("category", "")
        key = inp.get("key", "")
        value = inp.get("value", {})

        if not category or not key:
            return {"error": "category, key 필수"}

        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        await mgr.learn(category, key, value)
        return {"status": "learned", "category": category, "key": key}


# ─── 하위 호환성 ─────────────────────────────────────────────────────────────

_INTENT_TOOL_MAP: Dict[str, list] = {
    "health_check":        ["health_check"],
    "dashboard":           ["dashboard_query"],
    "diagnosis":           ["dashboard_query", "health_check"],
    "search":              ["web_search_brave"],
    "memory_recall":       ["read_github_file", "query_database"],
    "directive_gen":       ["directive_create", "generate_directive"],
    "execute":             ["directive_create"],
    "workspace_switch":    ["dashboard_query"],
    "qa":                  ["read_remote_file", "list_remote_dir"],
    "execution_verify":    ["read_remote_file", "list_remote_dir"],
    "task_history":        ["task_history"],
    "cost_report":         ["cost_report"],
    "system_status":       ["health_check", "dashboard_query", "get_all_service_status"],
    "url_analyze":         ["jina_read"],
    "url_read":            ["jina_read"],
    "server_file":         ["list_remote_dir", "read_remote_file"],
    # AADS-186A 신규 인텐트
    "service_inspection":  ["inspect_service"],
    "all_service_status":  ["get_all_service_status"],
    # AADS-186E-1 크롤링 인텐트
    "deep_crawl":          ["deep_crawl"],
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
