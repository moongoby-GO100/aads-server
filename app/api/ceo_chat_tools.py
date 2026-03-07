"""
CEO Chat 도구 정의 및 실행 (AADS-157)

5개 도구: read_file, read_github, search_logs, query_db, fetch_url

보안 규칙 (하드코딩, LLM 우회 불가):
  - read_file: /root/aads/ 하위만 허용. /etc, /proc, /root/.ssh 차단
  - query_db: SELECT만 허용. INSERT/UPDATE/DELETE/DROP/ALTER 차단
  - search_logs: 최근 100줄, 최대 10KB
  - fetch_url: 최대 20KB
"""
import asyncpg
import httpx
import logging
import re
import subprocess

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── 도구 정의 (Anthropic tool_use 포맷) ──────────────────────────────────────
TOOL_DEFINITIONS: List[Dict] = [
    {
        "name": "read_file",
        "description": "서버 68 로컬 파일 읽기. /root/aads/ 하위 경로만 허용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "읽을 파일의 절대 경로 (예: /root/aads/aads-docs/HANDOVER.md)",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_github",
        "description": "moongoby-GO100 GitHub 레포의 파일을 raw URL로 읽기.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "레포 내 파일 경로 (예: aads-docs/HANDOVER.md 또는 HANDOVER.md)",
                },
                "repo": {
                    "type": "string",
                    "description": "레포 이름 (기본값: aads-docs)",
                    "default": "aads-docs",
                },
                "branch": {
                    "type": "string",
                    "description": "브랜치 이름 (기본값: main)",
                    "default": "main",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_logs",
        "description": "Docker 컨테이너 로그 또는 journalctl에서 최근 100줄 검색. 최대 10KB 반환.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "로그 소스: Docker 컨테이너 이름(예: aads-server) 또는 'journalctl'",
                },
                "keyword": {
                    "type": "string",
                    "description": "검색할 키워드 (선택, 없으면 전체 최근 100줄 반환)",
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "query_db",
        "description": "PostgreSQL SELECT 쿼리 실행. SELECT 전용, 최대 50행 반환.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "실행할 SELECT SQL 쿼리 (예: SELECT * FROM task_tracking LIMIT 10)",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "fetch_url",
        "description": "외부 URL GET 요청. 응답 최대 20KB 반환.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "GET 요청할 URL (예: https://aads.newtalk.kr/api/v1/health)",
                }
            },
            "required": ["url"],
        },
    },
]

# ─── 보안 상수 ─────────────────────────────────────────────────────────────────
_FILE_WHITELIST = "/root/aads/"
_FILE_BLACKLIST = ["/etc", "/proc", "/root/.ssh", "/root/.genspark/directives"]
_SQL_BLOCKED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_MAX_LOG_BYTES = 10 * 1024   # 10 KB
_MAX_URL_BYTES = 20 * 1024   # 20 KB
_MAX_DB_ROWS = 50


# ─── 도구 실행 함수들 ──────────────────────────────────────────────────────────

async def tool_read_file(path: str) -> str:
    """로컬 파일 읽기 (화이트리스트 검사)."""
    try:
        resolved = str(Path(path).resolve())
    except Exception as e:
        return f"[ERROR] 경로 처리 실패: {e}"

    if not resolved.startswith(_FILE_WHITELIST):
        return f"[ERROR] 접근 거부: /root/aads/ 하위 경로만 허용됩니다. (요청: {resolved})"
    for blocked in _FILE_BLACKLIST:
        if resolved.startswith(blocked):
            return f"[ERROR] 접근 거부: {blocked} 경로는 차단되어 있습니다."

    try:
        p = Path(resolved)
        if not p.exists():
            return f"[ERROR] 파일 없음: {resolved}"
        if not p.is_file():
            return f"[ERROR] 파일이 아닙니다: {resolved}"
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 50_000:
            content = content[:50_000] + "\n...(50KB 초과, 잘림)"
        return content
    except Exception as e:
        return f"[ERROR] 파일 읽기 실패: {e}"


async def tool_read_github(
    path: str, repo: str = "aads-docs", branch: str = "main"
) -> str:
    """GitHub raw 파일 읽기 (moongoby-GO100 레포)."""
    raw_url = f"https://raw.githubusercontent.com/moongoby-GO100/{repo}/{branch}/{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(raw_url)
            if r.status_code == 404:
                return f"[ERROR] GitHub 파일 없음: {raw_url}"
            r.raise_for_status()
            content = r.text
            if len(content) > 50_000:
                content = content[:50_000] + "\n...(50KB 초과, 잘림)"
            return content
    except Exception as e:
        return f"[ERROR] GitHub 읽기 실패: {e}"


async def tool_search_logs(source: str, keyword: Optional[str] = None) -> str:
    """Docker logs 또는 journalctl 검색 (최근 100줄, 최대 10KB)."""
    try:
        if source.lower() == "journalctl":
            cmd = ["journalctl", "--no-pager", "-n", "100"]
        else:
            cmd = ["docker", "logs", "--tail", "100", source]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout + result.stderr

        if keyword:
            lines = [l for l in output.splitlines() if keyword.lower() in l.lower()]
            output = "\n".join(lines[-100:])

        # 크기 제한
        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_LOG_BYTES:
            output = encoded[-_MAX_LOG_BYTES:].decode("utf-8", errors="replace").lstrip()
            output = "[...앞부분 잘림...]\n" + output

        return output if output.strip() else f"[로그 없음: {source}]"
    except subprocess.TimeoutExpired:
        return f"[ERROR] 로그 조회 타임아웃: {source}"
    except Exception as e:
        return f"[ERROR] 로그 조회 실패: {e}"


async def tool_query_db(sql: str, dsn: str) -> str:
    """PostgreSQL SELECT 쿼리 실행 (SELECT 전용, 최대 50행)."""
    sql_stripped = sql.strip()
    if not sql_stripped.upper().startswith("SELECT"):
        return "[ERROR] SELECT 쿼리만 허용됩니다."
    if _SQL_BLOCKED.search(sql_stripped):
        return "[ERROR] 허용되지 않는 SQL 명령어가 포함되어 있습니다."

    try:
        conn = await asyncpg.connect(dsn=dsn)
        try:
            rows = await conn.fetch(sql_stripped)
            if not rows:
                return "(결과 없음)"
            rows = list(rows[:_MAX_DB_ROWS])
            cols = list(rows[0].keys())
            lines = [" | ".join(cols)]
            lines.append("-" * max(len(lines[0]), 10))
            for r in rows:
                lines.append(" | ".join(str(v) if v is not None else "NULL" for v in r.values()))
            suffix = f"\n(최대 {_MAX_DB_ROWS}행 제한)" if len(rows) == _MAX_DB_ROWS else ""
            return "\n".join(lines) + suffix
        finally:
            await conn.close()
    except Exception as e:
        return f"[ERROR] DB 쿼리 실패: {e}"


async def tool_fetch_url(url: str) -> str:
    """외부 URL GET (최대 20KB)."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.text
            encoded = content.encode("utf-8", errors="replace")
            if len(encoded) > _MAX_URL_BYTES:
                content = encoded[:_MAX_URL_BYTES].decode("utf-8", errors="replace")
                content += "\n...(20KB 초과, 잘림)"
            return content
    except Exception as e:
        return f"[ERROR] URL 조회 실패: {e}"


# ─── 디스패처 ──────────────────────────────────────────────────────────────────

async def execute_tool(name: str, params: Dict[str, Any], dsn: str) -> str:
    """도구 이름과 파라미터로 실제 실행."""
    if name == "read_file":
        return await tool_read_file(params.get("path", ""))
    elif name == "read_github":
        return await tool_read_github(
            params.get("path", ""),
            params.get("repo", "aads-docs"),
            params.get("branch", "main"),
        )
    elif name == "search_logs":
        return await tool_search_logs(
            params.get("source", ""),
            params.get("keyword"),
        )
    elif name == "query_db":
        return await tool_query_db(params.get("sql", ""), dsn)
    elif name == "fetch_url":
        return await tool_fetch_url(params.get("url", ""))
    else:
        return f"[ERROR] 알 수 없는 도구: {name}"
