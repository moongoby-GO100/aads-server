"""
AADS-186A: 도구 실행기 — Anthropic Tool Use API 도구 실행
10초 타임아웃, 결과 2000토큰(~6000자) 제한 (기본값, 실제 25,000 토큰 허용).
신규 워크플로우 도구: inspect_service, get_all_service_status, generate_directive
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

# Pipeline C 등에서 현재 채팅 세션 ID를 도구에 전달하기 위한 컨텍스트 변수
current_chat_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_chat_session_id", default=""
)

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
_AADS_API_BASE = os.getenv("AADS_API_BASE", "http://localhost:8080")

_MAX_RESULT_CHARS = 25000  # ~8000 토큰 (지시서 기준 25,000 허용)
_TOOL_TIMEOUT = 20.0  # 일반 도구 타임아웃
_LONG_TOOL_TIMEOUT = 120.0  # 서브에이전트/딥리서치 등 장시간 도구
_LONG_TOOLS = frozenset({"spawn_subagent", "spawn_parallel_subagents", "deep_research", "delegate_to_agent", "delegate_to_research"})


class ToolExecutor:
    """단일 도구 실행 + 타임아웃 + 결과 제한."""

    async def execute(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """
        도구 실행. 10초 타임아웃, 결과 6000자 제한.
        실패 시 JSON error 반환.
        """
        try:
            _timeout = _LONG_TOOL_TIMEOUT if tool_name in _LONG_TOOLS else _TOOL_TIMEOUT
            result = await asyncio.wait_for(
                self._dispatch(tool_name, tool_input),
                timeout=_timeout,
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
            "query_project_database": self._query_project_database,
            "list_project_databases": self._list_project_databases,
            "read_remote_file":       self._read_remote_file,
            "write_remote_file":      self._write_remote_file,
            "patch_remote_file":      self._patch_remote_file,
            "run_remote_command":     self._run_remote_command,
            "git_remote_add":         self._git_remote_add,
            "git_remote_commit":      self._git_remote_commit,
            "git_remote_push":        self._git_remote_push,
            "git_remote_status":      self._git_remote_status,
            "git_remote_create_branch": self._git_remote_create_branch,
            "list_remote_dir":        self._list_remote_dir,
            "cost_report":            self._cost_report,
            "web_search_brave":       self._web_search,
            "web_search":             self._web_search,
            "web_search_naver":       self._web_search_naver,
            "web_search_kakao":       self._web_search_kakao,
            "web_search_google":      self._web_search_google,
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
            # AADS-186E-2/186E-3: 메모리 도구
            "save_note":              self._save_note,
            "recall_notes":           self._recall_notes,
            "delete_note":            self._delete_note,
            "learn_pattern":          self._learn_pattern,
            "observe":                self._observe,
            # AADS-186E-3: 딥리서치 + 코드탐색 도구
            "deep_research":          self._deep_research,
            "code_explorer":          self._code_explorer,
            "analyze_changes":        self._analyze_changes,
            "search_all_projects":    self._search_all_projects,
            # AADS-188B: 시맨틱 코드 검색
            "semantic_code_search":   self._semantic_code_search,
            # AADS-188C Phase 2: 메타 도구 (Orchestrator)
            "check_directive_status": self._check_directive_status,
            "delegate_to_agent":      self._delegate_to_agent,
            "delegate_to_research":   self._delegate_to_research,
            # AADS-159: 브라우저 도구 (Playwright 기반)
            "browser_navigate":       self._browser_navigate,
            "browser_snapshot":       self._browser_snapshot,
            "browser_screenshot":     self._browser_screenshot,
            "browser_click":          self._browser_click,
            "browser_fill":           self._browser_fill,
            "browser_tab_list":       self._browser_tab_list,
            # AADS-190 Phase2-A: 서브에이전트
            "spawn_subagent":         self._spawn_subagent,
            "spawn_parallel_subagents": self._spawn_parallel_subagents,
            # AADS-190: 내보내기 + 스케줄러
            "export_data":            self._export_data,
            "schedule_task":          self._schedule_task,
            "unschedule_task":        self._unschedule_task,
            "list_scheduled_tasks":   self._list_scheduled_tasks,
            # Pipeline C: 자율 작업 파이프라인
            "pipeline_c_start":       self._pipeline_c_start,
            "pipeline_c_status":      self._pipeline_c_status,
            "pipeline_c_approve":     self._pipeline_c_approve,
            # 첨부파일 재읽기
            "read_uploaded_file":     self._read_uploaded_file,
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
        repo = (inp.get("repo") or "").strip()
        path = (inp.get("path") or "").strip()
        branch = (inp.get("branch") or "main").strip()

        if not repo or not path:
            return {"error": "repo와 path 필수 (예: repo='moongoby-GO100/aads-docs', path='HANDOVER.md')"}

        # repo가 owner/name 형식이 아니면 기본 owner 추가
        if "/" not in repo:
            repo = f"moongoby-GO100/{repo}"

        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        headers: Dict[str, str] = {}
        pat = os.getenv("GITHUB_PAT", os.getenv("GITHUB_TOKEN", ""))
        if pat:
            headers["Authorization"] = f"token {pat}"

        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 404:
                return {"error": f"파일 없음: {url}", "repo": repo, "path": path}
            r.raise_for_status()
            content = r.text
            if len(content) > 25000:
                content = content[:25000] + "\n...(내용 잘림)"
            return {"repo": repo, "path": path, "branch": branch, "content": content}

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

    async def _query_project_database(self, inp: Dict[str, Any]) -> Any:
        """프로젝트별 원격 DB SELECT 쿼리 (KIS/GO100/SF/NTV2). Yellow 등급."""
        try:
            from app.api.ceo_chat_tools_db import query_project_database
            return await query_project_database(
                project=inp.get("project", ""),
                query=inp.get("query", ""),
                limit=inp.get("limit", 100),
                db_name=inp.get("db_name"),
            )
        except Exception as e:
            return {"error": str(e)}

    async def _list_project_databases(self, inp: Dict[str, Any]) -> Any:
        """설정된 프로젝트 DB 목록 및 연결 상태."""
        try:
            from app.api.ceo_chat_tools_db import list_project_databases
            return await list_project_databases()
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

    async def _write_remote_file(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 파일 쓰기 (SSH, 자동 백업). Yellow 등급."""
        project = (inp.get("project") or "").upper()
        file_path = inp.get("file_path") or inp.get("path") or ""
        content = inp.get("content") or ""
        backup = inp.get("backup", True)
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        if not file_path:
            return {"error": "file_path 필수"}
        if not content:
            return {"error": "content 필수"}
        try:
            from app.api.ceo_chat_tools import tool_write_remote_file
            return await tool_write_remote_file(project, file_path, content, backup)
        except Exception as e:
            return {"error": str(e)}

    async def _patch_remote_file(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 파일 부분 수정 (old→new 교체). Yellow 등급."""
        project = (inp.get("project") or "").upper()
        file_path = inp.get("file_path") or inp.get("path") or ""
        old_string = inp.get("old_string") or ""
        new_string = inp.get("new_string") or ""
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        if not file_path:
            return {"error": "file_path 필수"}
        if not old_string:
            return {"error": "old_string 필수"}
        try:
            from app.api.ceo_chat_tools import tool_patch_remote_file
            return await tool_patch_remote_file(project, file_path, old_string, new_string)
        except Exception as e:
            return {"error": str(e)}

    async def _run_remote_command(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 명령 실행 (화이트리스트 기반). Yellow 등급."""
        project = (inp.get("project") or "").upper()
        command = inp.get("command") or ""
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        if not command:
            return {"error": "command 필수"}
        try:
            from app.api.ceo_chat_tools import tool_run_remote_command
            return await tool_run_remote_command(project, command)
        except Exception as e:
            return {"error": str(e)}

    async def _git_remote_add(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 git add."""
        project = (inp.get("project") or "").upper()
        files = inp.get("files") or "."
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        try:
            from app.api.ceo_chat_tools import tool_git_remote_add
            return await tool_git_remote_add(project, files)
        except Exception as e:
            return {"error": str(e)}

    async def _git_remote_commit(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 git commit."""
        project = (inp.get("project") or "").upper()
        message = inp.get("message") or ""
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        if not message:
            return {"error": "commit message 필수"}
        try:
            from app.api.ceo_chat_tools import tool_git_remote_commit
            return await tool_git_remote_commit(project, message)
        except Exception as e:
            return {"error": str(e)}

    async def _git_remote_push(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 git push (force push 차단)."""
        project = (inp.get("project") or "").upper()
        branch = inp.get("branch") or ""
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        try:
            from app.api.ceo_chat_tools import tool_git_remote_push
            return await tool_git_remote_push(project, branch)
        except Exception as e:
            return {"error": str(e)}

    async def _git_remote_status(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 git status."""
        project = (inp.get("project") or "").upper()
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        try:
            from app.api.ceo_chat_tools import tool_git_remote_status
            return await tool_git_remote_status(project)
        except Exception as e:
            return {"error": str(e)}

    async def _git_remote_create_branch(self, inp: Dict[str, Any]) -> Any:
        """원격 서버 새 브랜치 생성."""
        project = (inp.get("project") or "").upper()
        branch_name = inp.get("branch_name") or ""
        if not project or project not in ("KIS", "GO100", "SF", "NTV2"):
            return {"error": "project 필수: KIS, GO100, SF, NTV2 중 하나"}
        if not branch_name:
            return {"error": "branch_name 필수"}
        try:
            from app.api.ceo_chat_tools import tool_git_remote_create_branch
            return await tool_git_remote_create_branch(project, branch_name)
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

    async def _web_search(self, inp: Dict[str, Any]) -> Any:
        """통합 웹 검색 — Gemini Google → Naver → Kakao 순 폴백 체인."""
        query = inp.get("query", "")
        count = inp.get("count", 5)
        engine = inp.get("engine", "auto")  # auto|google|naver|kakao|all

        if not query:
            return {"error": "query 필수"}

        if engine == "all":
            return await self._web_search_all(query, count)

        if engine in ("google", "auto"):
            try:
                result = await self._web_search_google(inp)
                if not result.get("error"):
                    return result
            except Exception:
                pass
            if engine == "google":
                return result

        if engine in ("naver", "auto"):
            try:
                result = await self._web_search_naver(inp)
                if not result.get("error"):
                    return result
            except Exception:
                pass
            if engine == "naver":
                return result

        if engine in ("kakao", "auto"):
            try:
                result = await self._web_search_kakao(inp)
                if not result.get("error"):
                    return result
            except Exception:
                pass
            if engine == "kakao":
                return result

        return {"error": "모든 검색 엔진 실패", "query": query}

    async def _web_search_all(self, query: str, count: int = 5) -> Any:
        """3개 검색 엔진 병렬 실행 → 통합 결과."""
        inp = {"query": query, "count": count}
        results = await asyncio.gather(
            self._web_search_google(inp),
            self._web_search_naver(inp),
            self._web_search_kakao(inp),
            return_exceptions=True,
        )
        merged_text = []
        merged_citations = []
        engines_ok = []
        for name, r in zip(["google", "naver", "kakao"], results):
            if isinstance(r, Exception) or (isinstance(r, dict) and r.get("error")):
                continue
            engines_ok.append(name)
            if isinstance(r, dict):
                merged_text.append(f"【{name.upper()}】\n{r.get('text', '')}")
                merged_citations.extend(r.get("citations", []))
        return {
            "text": "\n\n".join(merged_text) if merged_text else f"모든 검색 엔진 실패: {query}",
            "citations": merged_citations,
            "engines_used": engines_ok,
        }

    async def _web_search_google(self, inp: Dict[str, Any]) -> Any:
        """Gemini Google Search Grounding."""
        query = inp.get("query", "")
        from app.services.gemini_search_service import GeminiSearchService
        svc = GeminiSearchService()
        if not svc._api_key:
            return {"error": "GEMINI_API_KEY 미설정", "engine": "google"}
        result = await svc.search_grounded(query)
        if result.error:
            return {"error": result.error, "engine": "google"}
        return {"text": result.text, "citations": result.citations, "engine": "google"}

    async def _web_search_naver(self, inp: Dict[str, Any]) -> Any:
        """Naver 웹 검색."""
        query = inp.get("query", "")
        count = inp.get("count", 5)
        search_type = inp.get("search_type", "webkr")
        from app.services.naver_search_service import NaverSearchService
        svc = NaverSearchService()
        if not svc.is_available():
            return {"error": "NAVER API 키 미설정", "engine": "naver"}
        result = await svc.search(query, search_type=search_type, count=count)
        if result.error:
            return {"error": result.error, "engine": "naver"}
        return {"text": result.text, "citations": result.citations, "engine": "naver"}

    async def _web_search_kakao(self, inp: Dict[str, Any]) -> Any:
        """Kakao (Daum) 웹 검색."""
        query = inp.get("query", "")
        count = inp.get("count", 5)
        from app.services.kakao_search_service import KakaoSearchService
        svc = KakaoSearchService()
        if not svc.is_available():
            return {"error": "KAKAO API 키 미설정", "engine": "kakao"}
        result = await svc.search(query, count=count)
        if result.error:
            return {"error": result.error, "engine": "kakao"}
        return {"text": result.text, "citations": result.citations, "engine": "kakao"}

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

    # ── AADS-186E-2/186E-3: 메모리 도구 ─────────────────────────────────────

    async def _save_note(self, inp: Dict[str, Any]) -> Any:
        """노트 저장 — title/content 기반 (AADS-186E-3 업데이트)."""
        title = inp.get("title", "") or inp.get("summary", "")  # 하위호환
        content = inp.get("content", "")
        category = inp.get("category", "general")

        if not title:
            return {"error": "title 필수"}

        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()

        # content가 없으면 title을 content로 사용 (하위호환)
        if not content:
            content = title

        result = await mgr.save_note(title=title, content=content, category=category)
        return {"status": "saved", "message": result, "title": title, "category": category}

    async def _recall_notes(self, inp: Dict[str, Any]) -> Any:
        """노트 검색 — keyword 기반 (AADS-186E-3 업데이트)."""
        query = inp.get("query", "")
        limit = min(int(inp.get("limit", inp.get("count", 5))), 20)

        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()

        if query:
            notes = await mgr.recall_notes(query=query, limit=limit)
        else:
            notes = await mgr.get_recent_notes(limit)

        def _note_to_dict(n):
            d = {
                "session_id": n.session_id,
                "summary": n.summary,
                "key_decisions": n.key_decisions,
                "action_items": n.action_items,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            if n.content:
                d["content"] = n.content[:2000]
            return d

        return [_note_to_dict(n) for n in notes]

    async def _delete_note(self, inp: Dict[str, Any]) -> Any:
        """노트 삭제 — id 또는 keyword 기반."""
        note_id = int(inp.get("note_id", inp.get("id", 0)) or 0)
        keyword = (inp.get("keyword", "") or "").strip()

        if not note_id and not keyword:
            return {"error": "note_id 또는 keyword 중 하나 필수"}

        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        result = await mgr.delete_note(note_id=note_id, keyword=keyword)
        return {"status": "deleted" if "완료" in result else "not_found", "message": result}

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

    async def _observe(self, inp: Dict[str, Any]) -> Any:
        """자동 관찰 기록 — ai_observations UPSERT (AADS-186E-3)."""
        category = inp.get("category", "")
        key = inp.get("key", "")
        value = inp.get("value", "")
        confidence = float(inp.get("confidence", 0.5))

        if not category or not key or not value:
            return {"error": "category, key, value 필수"}

        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        await mgr.observe(category=category, key=key, value=value, confidence=confidence)
        return {"status": "observed", "category": category, "key": key, "confidence": confidence}


    # ── AADS-186E-3: 딥리서치 + 코드탐색 도구 ────────────────────────────────

    async def _deep_research(self, inp: Dict[str, Any]) -> Any:
        """Gemini Deep Research API — 수십 개 소스 종합 보고서 (AADS-188A: Langfuse span + context/format)."""
        query = inp.get("query", "")
        if not query:
            return {"error": "query 필수"}
        context = inp.get("context")
        format_param = inp.get("format")
        format_instructions = inp.get("format_instructions")
        from app.services.deep_research_service import DeepResearchService
        svc = DeepResearchService()
        if not svc.is_available():
            return {"error": "GEMINI_API_KEY 미설정 — Deep Research 비활성"}

        # Langfuse span (AADS-188A)
        lf_span = None
        try:
            from app.core.langfuse_config import get_langfuse, is_enabled
            if is_enabled():
                lf = get_langfuse()
                if lf:
                    trace = lf.trace(name="tool_deep_research", input=query, user_id="CEO")
                    lf_span = trace.span(
                        name="deep_research_tool",
                        input={"query": query, "context": context, "format": format_param},
                    )
        except Exception:
            pass

        result = await asyncio.wait_for(
            svc.research(
                query,
                context=context,
                format=format_param,
                format_instructions=format_instructions,
            ),
            timeout=600.0,  # 10분 타임아웃
        )

        if lf_span:
            try:
                lf_span.end(
                    output=result.report[:300],
                    metadata={
                        "sources_count": len(result.citations),
                        "cost_usd": result.cost_usd,
                        "elapsed_sec": result.elapsed_sec,
                        "status": result.status,
                    },
                )
            except Exception:
                pass

        return {
            "report": result.report,
            "interaction_id": result.interaction_id,
            "citations": result.citations,
            "status": result.status,
            "cost_usd": result.cost_usd,
            "elapsed_sec": result.elapsed_sec,
        }

    async def _code_explorer(self, inp: Dict[str, Any]) -> Any:
        """함수 호출 체인 추적."""
        project = inp.get("project", "")
        entry_point = inp.get("entry_point", "")
        depth = min(int(inp.get("depth", 3)), 3)
        if not project or not entry_point:
            return {"error": "project, entry_point 필수"}
        from app.services.code_explorer_service import CodeExplorerService
        svc = CodeExplorerService()
        result = await asyncio.wait_for(
            svc.trace_function_chain(project, entry_point, depth),
            timeout=180.0,  # 3분
        )
        return {
            "project": result.project,
            "entry_point": result.entry_point,
            "diagram": result.diagram,
            "chain_depth": len(result.chain),
            "error": result.error,
        }

    async def _analyze_changes(self, inp: Dict[str, Any]) -> Any:
        """최근 Git 변경 분석 + 위험도 평가."""
        project = inp.get("project", "")
        days = min(int(inp.get("days", 7)), 30)
        if not project:
            return {"error": "project 필수"}
        from app.services.code_explorer_service import CodeExplorerService
        svc = CodeExplorerService()
        result = await asyncio.wait_for(
            svc.analyze_recent_changes(project, days),
            timeout=60.0,
        )
        return {
            "project": result.project,
            "days": result.days,
            "commits": result.commits[:10],
            "changed_files": result.changed_files[:10],
            "categories": result.categories,
            "risk_level": result.risk_level,
            "affected_services": result.affected_services,
            "summary": result.summary,
            "error": result.error,
        }

    async def _search_all_projects(self, inp: Dict[str, Any]) -> Any:
        """6개 프로젝트 코드베이스 동시 검색."""
        query = inp.get("query", "")
        if not query:
            return {"error": "query 필수"}
        from app.services.code_explorer_service import CodeExplorerService
        svc = CodeExplorerService()
        result = await asyncio.wait_for(
            svc.search_all_projects(query),
            timeout=180.0,  # 3분
        )
        return {
            "query": result.query,
            "matches": result.matches[:30],
            "duplicate_patterns": result.duplicate_patterns,
            "shared_modules": result.shared_modules,
            "projects_searched": result.projects_searched,
            "projects_failed": result.projects_failed,
            "total_matches": len(result.matches),
        }

    # ── AADS-188C Phase 2: 메타 도구 (Orchestrator) ─────────────────────────

    async def _check_directive_status(self, inp: Dict[str, Any]) -> Any:
        """
        지시사항 진행 상태 종합 확인.
        task_history + get_all_service_status 통합 메타 도구.
        """
        project = inp.get("project", "")
        limit = min(inp.get("limit", 10), 50)

        results: Dict[str, Any] = {}

        # 1) task_history 조회
        try:
            task_result = await self._task_history({"project": project, "limit": limit})
            results["task_history"] = task_result
        except Exception as e:
            results["task_history_error"] = str(e)

        # 2) 전체 서비스 상태
        try:
            status_result = await self._get_all_service_status({"include_details": False})
            results["service_status"] = status_result
        except Exception as e:
            results["service_status_error"] = str(e)

        # 3) 요약 생성
        task_count = len(task_result) if isinstance(task_result, list) else 0
        results["summary"] = (
            f"최근 작업 {task_count}건 조회 완료. "
            f"서비스 상태 확인 완료."
        )

        return results

    async def _delegate_to_agent(self, inp: Dict[str, Any]) -> Any:
        """
        복잡한 다단계 작업을 AutonomousExecutor에 위임.
        실제 실행 후 결과를 directive_lifecycle에 저장.
        """
        import asyncio
        import uuid
        from datetime import datetime

        task = inp.get("task", "")
        project = inp.get("project", "AADS")

        if not task:
            return {"error": "task 필수 — 위임할 작업 설명을 입력하세요"}

        task_id = f"agent-{uuid.uuid4().hex[:8]}"

        # 1) directive_lifecycle에 작업 등록
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO directive_lifecycle
                        (task_id, project, title, content, executor, status, priority, created_at, queued_at)
                    VALUES ($1, $2, $3, $4, 'autonomous_executor', 'in_progress', 'P2-NORMAL', NOW(), NOW())
                    """,
                    task_id, project,
                    f"[Agent] {task[:100]}",
                    task,
                )
        except Exception as e:
            logger.error(f"delegate_to_agent DB insert failed: {e}")
            return {"error": f"DB 등록 실패: {e}"}

        # 2) AutonomousExecutor로 실제 실행 (백그라운드 task)
        # ContextVar를 명시적으로 캡처 (background task 생성 전)
        _captured_session_id = current_chat_session_id.get("")
        logger.info(f"delegate_to_agent: task_id={task_id} captured_session_id={_captured_session_id[:8] if _captured_session_id else '(empty)'}")

        async def _run_agent_task():
            import json as _json
            result_text = ""
            error_text = ""
            try:
                from app.services.autonomous_executor import AutonomousExecutor
                from app.services.tool_registry import ToolRegistry

                executor = AutonomousExecutor(max_iterations=15, cost_limit=1.5)
                registry = ToolRegistry()
                tools = registry.get_tools("all")
                messages = [{"role": "user", "content": task}]
                system_prompt = (
                    f"당신은 AADS 자율 에이전트입니다. 프로젝트: {project}.\n"
                    f"주어진 작업을 도구를 활용하여 완료하세요. 완료 후 결과를 명확하게 요약하세요."
                )

                async for sse_event in executor.execute_task(
                    task_description=task,
                    tools=tools,
                    messages=messages,
                    model="claude-sonnet",
                    system_prompt=system_prompt,
                ):
                    try:
                        data = _json.loads(sse_event.replace("data: ", "").strip())
                        if data.get("type") == "complete":
                            result_text = data.get("content", "")[:5000]
                        elif data.get("type") == "error":
                            error_text = data.get("content", "")
                        elif data.get("type") == "delta":
                            result_text += data.get("content", "")
                    except (_json.JSONDecodeError, AttributeError):
                        pass

                if len(result_text) > 5000:
                    result_text = result_text[-5000:]

            except Exception as e:
                error_text = str(e)
                logger.error(f"delegate_to_agent execution failed task={task_id}: {e}")

            # 3) DB 결과 업데이트
            try:
                from app.core.db_pool import get_pool
                pool = get_pool()
                async with pool.acquire() as conn:
                    if error_text:
                        await conn.execute(
                            """
                            UPDATE directive_lifecycle
                            SET status = 'failed', error_detail = $2,
                                completed_at = NOW(), started_at = COALESCE(started_at, NOW())
                            WHERE task_id = $1 AND project = $3
                            """,
                            task_id, error_text[:2000], project,
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE directive_lifecycle
                            SET status = 'completed',
                                validation_result = $2::jsonb,
                                completed_at = NOW(), started_at = COALESCE(started_at, NOW())
                            WHERE task_id = $1 AND project = $3
                            """,
                            task_id,
                            _json.dumps({"result": result_text[:3000], "task_id": task_id}, ensure_ascii=False),
                            project,
                        )
            except Exception as db_err:
                logger.error(f"delegate_to_agent DB update failed task={task_id}: {db_err}")

            # 4) 채팅방에 결과 보고 (캡처된 session_id 사용)
            try:
                session_id = _captured_session_id  # ContextVar 대신 명시적 캡처값 사용
                if session_id:
                    from app.core.db_pool import get_pool
                    pool = get_pool()
                    status_emoji = "❌" if error_text else "✅"
                    msg = (
                        f"{status_emoji} **[Agent 작업 완료]** `{task_id}`\n"
                        f"프로젝트: **{project}**\n"
                        f"작업: {task[:200]}\n\n"
                    )
                    if error_text:
                        msg += f"**오류:** {error_text[:500]}"
                    else:
                        msg += f"**결과:**\n{result_text[:1500]}"

                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute(
                                """
                                INSERT INTO chat_messages
                                    (session_id, role, content, intent, cost,
                                     tokens_in, tokens_out, attachments, sources, tools_called)
                                VALUES ($1::uuid, 'assistant', $2, 'agent_result', 0,
                                        0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                                """,
                                session_id, msg,
                            )
                            await conn.execute(
                                "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                                session_id,
                            )
            except Exception as chat_err:
                logger.warning(f"delegate_to_agent chat post failed: {chat_err}")

        # 백그라운드 실행
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_run_agent_task())
        except RuntimeError:
            logger.error("delegate_to_agent: no running event loop")
            return {"error": "이벤트 루프 없음"}

        return {
            "status": "started",
            "task_id": task_id,
            "project": project,
            "message": f"Agent 작업이 시작되었습니다. task_id: {task_id}. 완료 후 채팅방에 결과가 보고됩니다.",
        }

    async def _delegate_to_research(self, inp: Dict[str, Any]) -> Any:
        """
        심층 리서치를 Deep Research 에이전트에게 위임.
        시장 분석, 기술 트렌드, 경쟁 분석 등.
        내부적으로 deep_research 도구를 호출.
        """
        query = inp.get("query", "")
        context = inp.get("context", "")
        format_type = inp.get("format", "detailed")

        if not query:
            return {"error": "query 필수 — 리서치 주제를 입력하세요"}

        # deep_research 도구로 위임
        research_input = {
            "query": query,
            "format": format_type,
        }
        if context:
            research_input["context"] = context

        try:
            result = await self._deep_research(research_input)
            result["delegated_from"] = "delegate_to_research"
            return result
        except Exception as e:
            return {
                "error": f"Deep Research 위임 실패: {e}",
                "alternative": "web_search_brave로 간단한 검색을 시도하거나, deep_crawl로 크롤링 기반 분석을 할 수 있습니다.",
            }

    # ── AADS-190 Phase2-A: 서브에이전트 ──────────────────────────────────────

    async def _spawn_subagent(self, inp: Dict[str, Any]) -> Any:
        """독립적 서브에이전트 실행 — 복잡한 작업을 분할 위임."""
        task = inp.get("task", "")
        if not task:
            return {"error": "task 필수 — 서브에이전트에게 위임할 작업 설명을 입력하세요"}

        from app.services.subagent_service import spawn_subagent
        return await spawn_subagent(
            task=task,
            model=inp.get("model", "sonnet"),
            system_prompt=inp.get("system_prompt"),
            context=inp.get("context"),
            enable_tools=inp.get("enable_tools", True),
        )

    async def _spawn_parallel_subagents(self, inp: Dict[str, Any]) -> Any:
        """여러 서브에이전트를 병렬 실행 후 결과 취합."""
        tasks = inp.get("tasks", [])
        if not tasks:
            return {"error": "tasks 필수 — [{task, model?, context?}, ...] 형태 리스트"}

        from app.services.subagent_service import spawn_parallel_subagents
        results = await spawn_parallel_subagents(
            tasks=tasks,
            max_concurrent=inp.get("max_concurrent", 5),
        )
        return {
            "total": len(results),
            "completed": sum(1 for r in results if r["status"] == "completed"),
            "results": results,
        }

    # ── AADS-190: 내보내기 + 스케줄러 도구 ───────────────────────────────────

    async def _export_data(self, inp: Dict[str, Any]) -> Any:
        """데이터를 CSV/Excel/PDF로 내보내기. 쿼리 결과 또는 직접 데이터."""
        try:
            data = inp.get("data")
            fmt = inp.get("format", "xlsx")
            title = inp.get("title")
            filename = inp.get("filename")

            # data가 없으면 project+query로 자동 조회
            if not data and inp.get("project") and inp.get("query"):
                from app.api.ceo_chat_tools_db import query_project_database
                result = await query_project_database(
                    inp["project"], inp["query"], limit=inp.get("limit", 1000)
                )
                if result.get("error"):
                    return result
                data = result.get("rows", [])

            if not data:
                return {"error": "data 또는 project+query 필수"}

            from app.api.ceo_chat_tools_export import export_data
            return await export_data(data, fmt, filename, title)
        except Exception as e:
            return {"error": str(e)}

    async def _schedule_task(self, inp: Dict[str, Any]) -> Any:
        """예약 작업 등록. Yellow 등급."""
        try:
            from app.api.ceo_chat_tools_scheduler import schedule_task
            return await schedule_task(
                name=inp.get("name", ""),
                schedule_type=inp.get("schedule_type", ""),
                action_type=inp.get("action_type", ""),
                action_config=inp.get("action_config", {}),
                schedule_config=inp.get("schedule_config"),
            )
        except Exception as e:
            return {"error": str(e)}

    async def _unschedule_task(self, inp: Dict[str, Any]) -> Any:
        """예약 작업 삭제. Yellow 등급."""
        try:
            from app.api.ceo_chat_tools_scheduler import unschedule_task
            return await unschedule_task(name=inp.get("name", ""))
        except Exception as e:
            return {"error": str(e)}

    async def _list_scheduled_tasks(self, inp: Dict[str, Any]) -> Any:
        """등록된 예약 작업 목록."""
        try:
            from app.api.ceo_chat_tools_scheduler import list_scheduled_tasks
            return await list_scheduled_tasks()
        except Exception as e:
            return {"error": str(e)}

    # ── Pipeline C: 자율 작업 파이프라인 ──────────────────────────────────────

    async def _pipeline_c_start(self, inp: Dict[str, Any]) -> Any:
        """Pipeline C 시작 — Claude Code 자율 작업."""
        from app.api.ceo_chat_tools import tool_pipeline_c_start
        # 현재 채팅 세션 ID를 컨텍스트에서 가져와서 전달
        _session_id = current_chat_session_id.get("")
        if not _session_id:
            logger.warning("_pipeline_c_start: chat_session_id 없음 — 채팅방 보고가 비활성됩니다")
        return await tool_pipeline_c_start(
            project=inp.get("project", ""),
            instruction=inp.get("instruction", ""),
            max_cycles=inp.get("max_cycles", 3),
            dsn="",
            chat_session_id=_session_id,
        )

    async def _pipeline_c_status(self, inp: Dict[str, Any]) -> Any:
        """Pipeline C 상태 조회."""
        from app.api.ceo_chat_tools import tool_pipeline_c_status
        return await tool_pipeline_c_status(job_id=inp.get("job_id", ""))

    async def _pipeline_c_approve(self, inp: Dict[str, Any]) -> Any:
        """Pipeline C 승인/거부."""
        from app.api.ceo_chat_tools import tool_pipeline_c_approve
        return await tool_pipeline_c_approve(
            job_id=inp.get("job_id", ""),
            approved=inp.get("approved", False),
            reason=inp.get("reason", ""),
        )

    # ── AADS-159: 브라우저 도구 (Playwright — ceo_chat_tools 래퍼) ────────────

    async def _browser_navigate(self, inp: Dict[str, Any]) -> Any:
        """브라우저로 URL 이동."""
        url = inp.get("url", "")
        if not url:
            return {"error": "url 필수"}
        from app.api.ceo_chat_tools import tool_browser_navigate
        return await tool_browser_navigate(url)

    async def _browser_snapshot(self, inp: Dict[str, Any]) -> Any:
        """현재 페이지 접근성 트리 추출 (텍스트 기반 UI 분석)."""
        from app.api.ceo_chat_tools import tool_browser_snapshot
        return await tool_browser_snapshot()

    async def _browser_screenshot(self, inp: Dict[str, Any]) -> Any:
        """현재 페이지 PNG 스크린샷 촬영."""
        from app.api.ceo_chat_tools import tool_browser_screenshot
        return await tool_browser_screenshot()

    async def _browser_click(self, inp: Dict[str, Any]) -> Any:
        """CSS selector로 요소 클릭."""
        selector = inp.get("selector", "")
        if not selector:
            return {"error": "selector 필수"}
        from app.api.ceo_chat_tools import tool_browser_click
        return await tool_browser_click(selector)

    async def _browser_fill(self, inp: Dict[str, Any]) -> Any:
        """입력 필드에 텍스트 채우기."""
        selector = inp.get("selector", "")
        value = inp.get("value", "")
        if not selector:
            return {"error": "selector 필수"}
        from app.api.ceo_chat_tools import tool_browser_fill
        return await tool_browser_fill(selector, value)

    async def _browser_tab_list(self, inp: Dict[str, Any]) -> Any:
        """열린 탭 목록 반환."""
        from app.api.ceo_chat_tools import tool_browser_tab_list
        return await tool_browser_tab_list()

    async def _semantic_code_search(self, inp: Dict[str, Any]) -> Any:
        """AADS-188B: ChromaDB 벡터 기반 시맨틱 코드 검색."""
        query = inp.get("query", "")
        if not query:
            return {"error": "query 필수"}
        project = inp.get("project") or None
        top_k = min(int(inp.get("top_k", 5)), 20)

        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()

        if not svc._is_available():
            return {
                "error": "ChromaDB 미초기화",
                "hint": "먼저 index_project를 실행하세요 (예: code_indexer.index_project('AADS'))",
            }

        try:
            results = await asyncio.wait_for(
                svc.search(query, project=project, top_k=top_k),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            return {"error": "timeout — 30초 초과"}

        return {
            "query": query,
            "project_filter": project,
            "results": results,
            "total": len(results),
        }

    # ── 첨부파일 재읽기 도구 ─────────────────────────────────────────────────

    async def _read_uploaded_file(self, inp: Dict[str, Any]) -> Any:
        """워크스페이스에 업로드된 파일을 읽거나 목록 반환."""
        import asyncpg
        filename = inp.get("filename", "").strip()
        workspace_id = inp.get("workspace_id", "").strip()
        max_chars = int(inp.get("max_chars", 100000))

        from app.core.db_pool import get_pool
        pool = get_pool()
        conn: asyncpg.Connection = await pool.acquire()
        try:
            if workspace_id:
                import uuid as _uuid
                ws_filter = _uuid.UUID(workspace_id)
            else:
                ws_filter = None

            # 파일명 검색 또는 전체 목록
            if filename:
                if ws_filter:
                    rows = await conn.fetch(
                        "SELECT id, filename, file_path, file_type, file_size, created_at "
                        "FROM chat_drive_files WHERE workspace_id = $1 AND filename ILIKE $2 "
                        "ORDER BY created_at DESC LIMIT 10",
                        ws_filter, f"%{filename}%",
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT id, filename, file_path, file_type, file_size, created_at "
                        "FROM chat_drive_files WHERE filename ILIKE $1 "
                        "ORDER BY created_at DESC LIMIT 10",
                        f"%{filename}%",
                    )
            else:
                if ws_filter:
                    rows = await conn.fetch(
                        "SELECT id, filename, file_path, file_type, file_size, created_at "
                        "FROM chat_drive_files WHERE workspace_id = $1 "
                        "ORDER BY created_at DESC LIMIT 20",
                        ws_filter,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT id, filename, file_path, file_type, file_size, created_at "
                        "FROM chat_drive_files "
                        "ORDER BY created_at DESC LIMIT 20",
                    )
        finally:
            await pool.release(conn)

        if not rows:
            return {"status": "not_found", "message": f"'{filename}' 파일을 찾을 수 없습니다.", "hint": "filename을 비워서 전체 목록을 조회해 보세요."}

        # 1건이면 내용 읽기, 여러 건이면 목록 반환
        if len(rows) == 1 or (filename and len(rows) >= 1):
            target = rows[0]
            fpath = target["file_path"]
            fname = target["filename"]

            if not os.path.isfile(fpath):
                return {"status": "file_missing", "filename": fname, "path": fpath, "message": "디스크에서 파일을 찾을 수 없습니다."}

            ext = os.path.splitext(fpath)[1].lower()
            text_exts = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css",
                         ".yaml", ".yml", ".toml", ".sh", ".sql", ".log", ".xml", ".ini", ".cfg", ".conf"}
            if ext in text_exts:
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read(max_chars)
                    return {
                        "status": "ok",
                        "filename": fname,
                        "file_size": target["file_size"],
                        "content": content,
                        "truncated": len(content) >= max_chars,
                    }
                except Exception as e:
                    return {"status": "read_error", "filename": fname, "error": str(e)}
            else:
                return {"status": "binary_file", "filename": fname, "file_type": ext, "file_size": target["file_size"],
                        "message": f"바이너리 파일({ext})은 텍스트로 읽을 수 없습니다."}

        # 여러 건 → 목록
        file_list = [
            {"filename": r["filename"], "type": r["file_type"], "size": r["file_size"],
             "uploaded": str(r["created_at"])[:19]}
            for r in rows
        ]
        return {"status": "list", "files": file_list, "total": len(file_list),
                "hint": "특정 파일을 읽으려면 filename에 정확한 이름을 지정하세요."}


# ─── 하위 호환성 ─────────────────────────────────────────────────────────────

_INTENT_TOOL_MAP: Dict[str, list] = {
    "health_check":        ["health_check"],
    "dashboard":           ["dashboard_query"],
    "diagnosis":           ["dashboard_query", "health_check"],
    "search":              ["web_search"],
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
    # AADS-186E-3 딥리서치 + 코드탐색 인텐트
    "deep_research":          ["deep_research"],
    "code_explorer":          ["code_explorer"],
    "analyze_changes":        ["analyze_changes"],
    "search_all_projects":    ["search_all_projects"],
    # AADS-188B 시맨틱 코드 검색 인텐트
    "semantic_code_search":   ["semantic_code_search"],
    # AADS-188C Phase 2: 메타 도구 인텐트
    "task_query":             ["check_directive_status"],
    "status_check":           ["check_directive_status"],
    # AADS-159: 브라우저 인텐트
    "browser":                ["browser_navigate", "browser_snapshot"],
    "browser_action":         ["browser_navigate", "browser_snapshot", "browser_screenshot"],
    # AADS-190: 원격 쓰기/패치/실행/Git 인텐트
    "code_modify":            ["read_remote_file", "write_remote_file", "patch_remote_file", "run_remote_command"],
    "code_fix":               ["read_remote_file", "patch_remote_file", "run_remote_command"],
    "deploy":                 ["run_remote_command", "git_remote_status", "git_remote_add", "git_remote_commit", "git_remote_push"],
    "git_operation":          ["git_remote_status", "git_remote_add", "git_remote_commit", "git_remote_push", "git_remote_create_branch"],
    "remote_execute":         ["run_remote_command", "read_remote_file"],
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
