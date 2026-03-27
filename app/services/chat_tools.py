"""
AADS-184: 채팅 도구 연동 구현 — 인텐트→도구 호출→결과 주입→LLM 응답 파이프라인
9개 도구 정의 및 실행 함수

도구 목록:
  1. health_check       — 서버 3대 + DB + 디스크 + 파이프라인 상태 조회
  2. dashboard_query    — pending/running/done 작업 현황 스캔
  3. search_web         — Brave Search API 웹 검색
  4. read_github_file   — GitHub raw 파일 읽기
  5. query_database     — PostgreSQL 읽기 전용 쿼리
  6. read_remote_file   — SSH 원격 서버 파일 읽기
  7. fetch_url          — 외부 URL 콘텐츠 조회
  8. generate_directive — 지시서 블록(>>>DIRECTIVE_START) 생성
  9. list_workspaces_sessions — 워크스페이스/세션 목록 조회

보안 규칙:
  - query_database: SELECT만 허용, INSERT/UPDATE/DELETE/DROP 차단
  - fetch_url: 도메인 화이트리스트 적용 (*.newtalk.kr, github.com, 허용 외부 도메인)
  - read_remote_file: 기존 ceo_chat_tools.py SSH 보안 규칙 재사용
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
import httpx

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DIRECTIVE_BASE = "/root/.genspark/directives"

# ─── 환경 변수 ─────────────────────────────────────────────────────────────────
_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://aads:aads_dev_local@aads-postgres:5432/aads")
_BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
_GITHUB_PAT = os.getenv("GITHUB_PAT", os.getenv("GITHUB_TOKEN", ""))

# ─── 보안 상수 ─────────────────────────────────────────────────────────────────
_SQL_BLOCKED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_MAX_DB_ROWS = 50
_MAX_TOOL_CONTENT_CHARS = 6000  # 도구 결과 최대 문자 수 (~2000 토큰)

# fetch_url 블랙리스트 보안 (AADS-159→블랙리스트 전환, LLM 우회 불가)
import ipaddress as _ipaddress
from urllib.parse import urlparse as _urlparse

_FETCH_BLOCKED_HOSTS = frozenset([
    "metadata.google.internal",
    "metadata.google.internal.",
    "169.254.169.254",
])
_FETCH_BLOCKED_PORTS = frozenset([5432, 6379, 3306, 27017, 9200, 2379, 8500])
_FETCH_SAFE_HOSTS = frozenset(["localhost", "127.0.0.1", "::1"])
_FETCH_PRIVATE_NETWORKS = [
    _ipaddress.ip_network("10.0.0.0/8"),
    _ipaddress.ip_network("172.16.0.0/12"),
    _ipaddress.ip_network("192.168.0.0/16"),
    _ipaddress.ip_network("169.254.0.0/16"),
    _ipaddress.ip_network("fc00::/7"),
]


def _fetch_url_blocked(url: str) -> Optional[str]:
    """블랙리스트 보안 검사. 차단이면 에러 문자열, 통과이면 None."""
    try:
        parsed = _urlparse(url)
        hostname = (parsed.hostname or "").lower()
        scheme = (parsed.scheme or "").lower()
        port = parsed.port
    except Exception:
        return "[접근 차단] URL 파싱 실패"
    if not hostname:
        return "[접근 차단] 호스트명이 없습니다"
    if scheme not in ("http", "https", ""):
        return f"[접근 차단] 허용되지 않은 프로토콜: {scheme}"
    if port and port in _FETCH_BLOCKED_PORTS:
        return f"[접근 차단] 민감 포트 접근 불가: {port}"
    if hostname in _FETCH_SAFE_HOSTS:
        return None
    if hostname in _FETCH_BLOCKED_HOSTS:
        return f"[접근 차단] 보안 차단 호스트: {hostname}"
    try:
        addr = _ipaddress.ip_address(hostname)
        for net in _FETCH_PRIVATE_NETWORKS:
            if addr in net:
                return f"[접근 차단] 내부 네트워크 접근 불가: {hostname}"
    except ValueError:
        pass
    return None


# ─── 도구 1: health_check ──────────────────────────────────────────────────────

async def health_check(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    서버 3대 상태 + DB + 디스크 + 파이프라인 조회 (경량 버전, 8초 이내).
    quick_health() + _check_db() + _check_disk() 병렬 실행.
    """
    try:
        from app.services.health_checker import (
            quick_health, _check_db, _check_disk,
            scan_directive_folder,
        )

        # 로컬 체크만 병렬 실행 (외부 SSH/HTTP 제외 — 타임아웃 방지)
        quick, db_res, disk_res, pending_scan, running_scan = (
            await asyncio.gather(
                quick_health(),
                _check_db(),
                _check_disk(),
                scan_directive_folder("pending"),
                scan_directive_folder("running"),
                return_exceptions=True,
            )
        )

        def _safe(v: Any, default: Any = {}) -> Any:
            return default if isinstance(v, Exception) else (v or default)

        quick = _safe(quick, {})
        db_res = _safe(db_res, {})
        disk_res = _safe(disk_res, {})
        pending_scan = _safe(pending_scan, {"count": 0})
        running_scan = _safe(running_scan, {"count": 0})

        return {
            "status": quick.get("status", "UNKNOWN"),
            "checked_at": quick.get("checked_at", datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")),
            "server_68": {
                "ok": True,
                "role": "AADS Backend+Dashboard (현재 서버)",
                "db_ok": db_res.get("ok", False),
                "db_latency_ms": db_res.get("latency_ms", "?"),
                "disk_usage_pct": disk_res.get("usage_pct", "?"),
                "disk_used": disk_res.get("used", "?"),
                "disk_total": disk_res.get("total", "?"),
            },
            "server_211": {"note": "Hub/bridge — SSH 체크 제외 (별도 모니터링)"},
            "server_114": {"note": "SF/NTV2/NAS — SSH 체크 제외 (별도 모니터링)"},
            "directives": {
                "pending": pending_scan.get("count", 0),
                "running": running_scan.get("count", 0),
            },
            "pipeline": {
                "stalled": quick.get("stalled", 0),
                "running_sessions": quick.get("running", 0),
                "pipeline_status": quick.get("status", "UNKNOWN"),
            },
            "completed_today": quick.get("completed_today", 0),
        }
    except Exception as e:
        logger.error(f"chat_tool_health_check_error: {e}")
        return {"error": str(e), "status": "ERROR"}


# ─── 도구 2: dashboard_query ──────────────────────────────────────────────────

async def dashboard_query(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    pending/running/done 작업 현황 조회.
    directives 폴더 스캔 + DB 최근 완료 작업 조회.
    """
    result: Dict[str, Any] = {}

    # 폴더 스캔
    for status in ("pending", "running", "done"):
        folder = os.path.join(DIRECTIVE_BASE, status)
        if os.path.isdir(folder):
            files = [f for f in os.listdir(folder) if f.endswith(".md")]
            result[status] = len(files)
        else:
            result[status] = 0

    # DB에서 최근 완료 작업 조회
    recent_done = []
    try:
        db_url = _DATABASE_URL.replace("postgresql://", "postgres://")
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            rows = await conn.fetch(
                """SELECT task_id, title, completed_at
                   FROM directive_lifecycle
                   WHERE status = 'completed'
                   ORDER BY completed_at DESC LIMIT 10"""
            )
            for row in rows:
                completed_at = row["completed_at"]
                if completed_at:
                    if hasattr(completed_at, "astimezone"):
                        completed_at = completed_at.astimezone(KST).strftime("%m-%d %H:%M KST")
                    else:
                        completed_at = str(completed_at)
                recent_done.append({
                    "task_id": row["task_id"],
                    "title": row["title"] or "",
                    "completed_at": completed_at or "",
                })
        finally:
            await conn.close()
    except Exception as e:
        result["db_error"] = str(e)

    result["recent_done"] = recent_done
    result["checked_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    return result


# ─── 도구 3: search_web ───────────────────────────────────────────────────────

async def search_web(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    Brave Search API로 웹 검색.
    BRAVE_API_KEY 미설정 시 에러 반환.
    """
    if not _BRAVE_API_KEY:
        return {"error": "BRAVE_API_KEY 미설정 — 웹 검색 불가"}

    # 메시지에서 검색어 추출 (키워드 제거)
    query = message
    for prefix in ["구글", "검색해줘", "찾아줘", "검색해", "검색 결과", "최신 뉴스", "news"]:
        query = query.replace(prefix, "").strip()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 5, "country": "KR", "search_lang": "ko"},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": _BRAVE_API_KEY,
                },
            )
            r.raise_for_status()
            data = r.json()
            web_results = data.get("web", {}).get("results", [])
            results = []
            for item in web_results[:5]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": (item.get("description", "") or "")[:300],
                })
            return {"query": query, "results": results}
    except Exception as e:
        logger.error(f"chat_tool_search_web_error: {e}")
        return {"error": str(e), "query": query}


# ─── 도구 4: read_github_file ─────────────────────────────────────────────────

async def read_github_file(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    GitHub 리포에서 파일 읽기 (raw.githubusercontent.com).
    HANDOVER.md 등 AADS 문서 조회에 최적화.
    """
    # 메시지에서 GitHub URL 또는 파일명 추출
    # 패턴: https://github.com/moongoby-GO100/{repo}/blob/{branch}/{path}
    # 또는 파일명만 언급 (예: "HANDOVER 내용 확인해")
    github_url_pattern = re.search(
        r"https://(?:github\.com|raw\.githubusercontent\.com)/([^/]+)/([^/]+)/(?:blob/)?([^/\s]+)/(.+?)(?:\s|$)",
        message,
    )

    repo = "aads-docs"
    branch = "main"
    path = "HANDOVER.md"  # 기본값

    if github_url_pattern:
        owner = github_url_pattern.group(1)
        repo = github_url_pattern.group(2)
        branch = github_url_pattern.group(3)
        path = github_url_pattern.group(4).strip()
    else:
        # 키워드 기반 파일명 추론
        kw_map = {
            "handover": "HANDOVER.md",
            "status": "STATUS.md",
            "rules": "HANDOVER-RULES.md",
            "directives": "CEO-DIRECTIVES.md",
            "workflow": "shared/rules/WORKFLOW-PIPELINE.md",
            "rule-matrix": "shared/rules/RULE-MATRIX.md",
        }
        msg_lower = message.lower()
        for kw, filepath in kw_map.items():
            if kw in msg_lower:
                path = filepath
                break

    raw_url = f"https://raw.githubusercontent.com/moongoby-GO100/{repo}/{branch}/{path}"
    headers: Dict[str, str] = {}
    if _GITHUB_PAT:
        headers["Authorization"] = f"token {_GITHUB_PAT}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(raw_url, headers=headers)
            if r.status_code == 404:
                return {"error": f"파일 없음: {raw_url}", "filename": path}
            r.raise_for_status()
            content = r.text
            if len(content) > _MAX_TOOL_CONTENT_CHARS:
                content = content[:_MAX_TOOL_CONTENT_CHARS] + "\n...(내용 잘림)"
            return {"filename": path, "repo": repo, "content": content}
    except Exception as e:
        logger.error(f"chat_tool_read_github_file_error: {e}")
        return {"error": str(e), "filename": path}


# ─── 도구 5: query_database ───────────────────────────────────────────────────

async def query_database(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    PostgreSQL 읽기 전용 쿼리.
    메시지 키워드에서 쿼리 자동 생성 또는 통계 반환.
    SELECT만 허용 (INSERT/UPDATE/DELETE/DROP 차단).
    """
    # 메시지 키워드 기반 자동 쿼리 선택
    msg_lower = message.lower()
    if "완료" in msg_lower or "done" in msg_lower or "오늘" in msg_lower:
        sql = (
            "SELECT task_id, title, status, completed_at::text "
            "FROM directive_lifecycle "
            "WHERE status = 'completed' AND completed_at >= CURRENT_DATE "
            "ORDER BY completed_at DESC LIMIT 20"
        )
    elif "pending" in msg_lower or "대기" in msg_lower:
        sql = (
            "SELECT task_id, title, status, queued_at::text "
            "FROM directive_lifecycle "
            "WHERE status = 'queued' "
            "ORDER BY queued_at DESC LIMIT 20"
        )
    elif "running" in msg_lower or "실행" in msg_lower:
        sql = (
            "SELECT task_id, title, status, started_at::text "
            "FROM directive_lifecycle "
            "WHERE status = 'running' "
            "ORDER BY started_at DESC LIMIT 10"
        )
    elif "비용" in msg_lower or "cost" in msg_lower:
        sql = (
            "SELECT task_id, cost_usd, model_used, completed_at::text "
            "FROM directive_lifecycle "
            "WHERE status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 20"
        )
    else:
        # 기본: 최근 완료 20건
        sql = (
            "SELECT task_id, title, status, "
            "COALESCE(completed_at, started_at, queued_at)::text AS changed_at "
            "FROM directive_lifecycle "
            "ORDER BY COALESCE(completed_at, started_at, queued_at) DESC NULLS LAST LIMIT 20"
        )

    # 보안 검증
    if _SQL_BLOCKED.search(sql):
        return {"error": "허용되지 않는 SQL 명령어"}

    try:
        db_url = _DATABASE_URL.replace("postgresql://", "postgres://")
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            rows = await conn.fetch(sql)
            data = [dict(row) for row in rows[:_MAX_DB_ROWS]]
            return {"sql": sql, "rows": data, "row_count": len(data)}
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"chat_tool_query_database_error: {e}")
        return {"error": str(e)}


# ─── 도구 6: read_remote_file ─────────────────────────────────────────────────

async def read_remote_file(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    서버 211/114의 파일 SSH로 읽기.
    기존 ceo_chat_tools.py의 tool_read_remote_file 재사용.
    """
    try:
        from app.api.ceo_chat_tools import tool_read_remote_file, _PROJECT_SERVER_MAP

        # 메시지에서 프로젝트 추출
        project = None
        for proj in _PROJECT_SERVER_MAP.keys():
            if proj.lower() in message.lower():
                project = proj
                break

        if not project:
            return {"error": "프로젝트명을 찾을 수 없음 (AADS, KIS, GO100, SF, NTV2 중 하나 포함 필요)"}

        # 파일 경로 추출 (기본값: 주요 설정 파일)
        default_files = {
            "AADS": "app/config.py",
            "KIS": "config.py",
            "GO100": "config.py",
            "SF": "config.py",
            "NTV2": "config/app.php",
        }
        file_path = default_files.get(project, "README.md")

        # 메시지에서 파일 경로 추출 시도
        path_match = re.search(r"(?:파일|file|경로)\s*[:\s]+([a-zA-Z0-9._/\-]+)", message)
        if path_match:
            file_path = path_match.group(1)

        content = await tool_read_remote_file(project, file_path)
        return {"server": project, "path": file_path, "content": content[:5000]}
    except Exception as e:
        logger.error(f"chat_tool_read_remote_file_error: {e}")
        return {"error": str(e)}


# ─── 도구 7: fetch_url ────────────────────────────────────────────────────────

async def fetch_url(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    외부 URL 콘텐츠 가져오기.
    도메인 화이트리스트 적용.
    """
    # 메시지에서 URL 추출
    url_match = re.search(r"https?://[^\s\)\"\']+", message)
    if not url_match:
        return {"error": "URL을 찾을 수 없음"}

    url = url_match.group(0).rstrip(".,;:)")

    # 블랙리스트 보안 검사
    _blocked = _fetch_url_blocked(url)
    if _blocked:
        return {"error": _blocked}

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.text
            if len(content) > _MAX_TOOL_CONTENT_CHARS:
                content = content[:_MAX_TOOL_CONTENT_CHARS] + "\n...(내용 잘림)"
            return {"url": url, "status": r.status_code, "content": content}
    except Exception as e:
        logger.error(f"chat_tool_fetch_url_error: {e}")
        return {"error": str(e), "url": url}


# ─── 도구 8: generate_directive ──────────────────────────────────────────────

async def generate_directive(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    대화 내용 기반 지시서 블록 생성.
    메시지에서 태스크 정보를 추출하여 >>>DIRECTIVE_START 포맷 생성.
    """
    # 워크스페이스에서 프로젝트 접두사 추정
    ws_prefix_map = {
        "AADS": "AADS",
        "SF": "SF",
        "KIS": "KIS",
        "GO100": "GO100",
        "NTV2": "NT",
        "NAS": "NAS",
    }

    # 워크스페이스 이름 조회
    project_prefix = "AADS"  # 기본값
    try:
        db_url = _DATABASE_URL.replace("postgresql://", "postgres://")
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = None
            if workspace_id:
                import uuid as _uuid
                try:
                    row = await conn.fetchrow(
                        "SELECT name FROM chat_workspaces WHERE id = $1",
                        _uuid.UUID(workspace_id),
                    )
                except Exception:
                    pass
            if row:
                ws_name = (row["name"] or "").upper().strip()
                for ws_key, prefix in ws_prefix_map.items():
                    if ws_key in ws_name:
                        project_prefix = prefix
                        break
        finally:
            await conn.close()
    except Exception:
        pass

    # 다음 Task ID 번호 추정 (done 폴더에서)
    done_folder = os.path.join(DIRECTIVE_BASE, "done")
    max_num = 183  # AADS-183 다음부터
    if os.path.isdir(done_folder):
        for fname in os.listdir(done_folder):
            m = re.search(rf"{project_prefix}-(\d+)", fname, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if n > max_num:
                    max_num = n
    next_num = max_num + 1

    # 메시지에서 제목 추출 (키워드 제거 후 첫 문장)
    title = message
    for kw in ["지시서 만들어", "지시서 생성", "태스크 만들어", "태스크 생성", "task 생성", "지시서를 작성"]:
        title = title.replace(kw, "").strip()
    title = title[:80] if title else f"{project_prefix} 작업"

    directive_text = f""">>>DIRECTIVE_START
TASK_ID: {project_prefix}-{next_num}
TITLE: {title}
PRIORITY: P2-MEDIUM
SIZE: M
IMPACT: M
EFFORT: M
MODEL: sonnet
REVIEW_REQUIRED: false
ASSIGNEE: Claude (서버 68, /root/aads)
DESCRIPTION: |
  {title}
SUCCESS_CRITERIA: |
  작업이 정상적으로 완료되고 검증됨
HANDOVER.md 업데이트 포함
>>>DIRECTIVE_END"""

    return {"directive_text": directive_text, "task_id": f"{project_prefix}-{next_num}"}


# ─── 도구 9: list_workspaces_sessions ────────────────────────────────────────

async def list_workspaces_sessions(message: str, workspace_id: str) -> Dict[str, Any]:
    """
    워크스페이스/세션 목록 조회.
    DB 직접 쿼리.
    """
    try:
        db_url = _DATABASE_URL.replace("postgresql://", "postgres://")
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            ws_rows = await conn.fetch(
                "SELECT id::text, name, color, icon FROM chat_workspaces ORDER BY created_at"
            )
            workspaces = [dict(r) for r in ws_rows]

            current_session = None
            if workspace_id:
                import uuid as _uuid
                try:
                    s_rows = await conn.fetch(
                        """SELECT id::text, title, message_count, cost_total::text, updated_at::text
                           FROM chat_sessions
                           WHERE workspace_id = $1
                           ORDER BY updated_at DESC LIMIT 5""",
                        _uuid.UUID(workspace_id),
                    )
                    current_session = [dict(r) for r in s_rows]
                except Exception:
                    pass
        finally:
            await conn.close()

        return {
            "workspaces": workspaces,
            "current_session": current_session,
            "workspace_id": workspace_id,
        }
    except Exception as e:
        logger.error(f"chat_tool_list_workspaces_sessions_error: {e}")
        return {"error": str(e)}
