"""
CEO Chat 도구 정의 및 실행 (AADS-157 + AADS-159)

5개 기존 도구: read_file, read_github, search_logs, query_db, fetch_url
6개 browser 도구: browser_navigate, browser_snapshot, browser_screenshot,
                  browser_click, browser_fill, browser_tab_list

보안 규칙 (하드코딩, LLM 우회 불가):
  - read_file: /root/aads/ 하위만 허용. /etc, /proc, /root/.ssh 차단
  - query_db: SELECT만 허용. INSERT/UPDATE/DELETE/DROP/ALTER 차단
  - search_logs: 최근 100줄, 최대 10KB
  - fetch_url: 최대 20KB
  - browser: 허용 도메인만 접근 (*.newtalk.kr, github.com, localhost)
"""
import asyncio
import asyncpg
import base64
import httpx
import logging
import re
import shlex
import subprocess

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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
    # ── Browser 도구 (AADS-159) ────────────────────────────────────────────
    {
        "name": "browser_navigate",
        "description": "브라우저로 URL 이동. 허용 도메인: *.newtalk.kr, github.com, raw.githubusercontent.com, localhost",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "이동할 URL (예: https://aads.newtalk.kr/)",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "현재 페이지의 접근성 트리를 텍스트로 추출. LLM이 페이지 구조·콘텐츠를 분석하는 데 최적.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "현재 페이지 PNG 스크린샷 촬영. base64 인코딩 결과 반환.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "browser_click",
        "description": "CSS selector 또는 텍스트로 요소 클릭.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "클릭할 요소의 CSS selector (예: button#submit, text=로그인)",
                }
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_fill",
        "description": "입력 필드에 텍스트 채우기.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "입력 필드의 CSS selector (예: input[name=username])",
                },
                "value": {
                    "type": "string",
                    "description": "입력할 텍스트",
                },
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "browser_tab_list",
        "description": "현재 열린 브라우저 탭 목록 반환 (URL + 제목).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── SSH 원격 파일 접근 도구 (AADS-165) ────────────────────────────────────
    {
        "name": "list_remote_dir",
        "description": "원격 서버의 디렉터리 구조 탐색. 프로젝트명으로 서버·경로 자동 매핑.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (KIS, GO100, SF, NTV2)",
                    "enum": ["KIS", "GO100", "SF", "NTV2"],
                },
                "path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대경로 (선택, 기본: 루트)",
                    "default": "",
                },
                "keyword": {
                    "type": "string",
                    "description": "파일명 검색어 (선택)",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "탐색 깊이 (기본: 3, 최대: 5)",
                    "default": 3,
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "read_remote_file",
        "description": "원격 서버의 파일 내용 읽기. 프로젝트명으로 서버·경로 자동 매핑.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "프로젝트명 (KIS, GO100, SF, NTV2)",
                    "enum": ["KIS", "GO100", "SF", "NTV2"],
                },
                "file_path": {
                    "type": "string",
                    "description": "WORKDIR 기준 상대 파일 경로 (예: src/main.py)",
                },
            },
            "required": ["project", "file_path"],
        },
    },
]

# ─── SSH 원격 접근 상수 (AADS-165, 하드코딩 — LLM 우회 불가) ─────────────────
_PROJECT_SERVER_MAP: Dict[str, Dict[str, str]] = {
    "KIS":  {"server": "211.188.51.113", "workdir": "/root/webapp"},
    "GO100": {"server": "211.188.51.113", "workdir": "/root/go100"},
    "SF":   {"server": "116.120.58.155", "workdir": "/data/shortflow"},
    "NTV2": {"server": "116.120.58.155", "workdir": "/srv/newtalk-v2"},
}

# SSH 보안 규칙 (하드코딩, LLM 우회 불가)
# 화이트리스트: 영숫자, 점, 하이픈, 밑줄, 슬래시만 허용 (보안 리뷰 반영)
_SSH_PATH_WHITELIST = re.compile(r'^[a-zA-Z0-9._/\-]*$')
_SSH_KEYWORD_WHITELIST = re.compile(r'^[a-zA-Z0-9._\-]*$')
_SSH_SENSITIVE_PATTERNS = re.compile(
    r'(\.env|\.ssh/|id_rsa|\.git/config|secrets|password|token'
    r'|\.npmrc|\.pypirc|\.netrc|credentials|private_key|kubeconfig'
    r'|\.aws/|\.kube/|\.docker/|\.pem$|\.key$|authorized_keys|known_hosts)',
    re.IGNORECASE,
)
_SSH_TIMEOUT = 10  # 초 (ConnectTimeout=5 + CommandTimeout=5)
_SSH_WRITE_TIMEOUT = 15  # 쓰기 작업은 조금 더 여유
_SSH_CMD_TIMEOUT = 30  # 원격 명령 실행 타임아웃
_SSH_MAX_RESULT_BYTES = 50 * 1024  # 50KB
_SSH_MAX_WRITE_BYTES = 1024 * 1024  # 1MB 쓰기 제한
_SSH_MAX_FILES = 100
_SSH_MAX_DEPTH = 5

# run_remote_command 허용 명령 화이트리스트 (보안 하드코딩, LLM 우회 불가)
_REMOTE_CMD_WHITELIST: List[str] = [
    "systemctl restart",
    "systemctl start",
    "systemctl stop",
    "systemctl status",
    "docker restart",
    "docker start",
    "docker stop",
    "docker ps",
    "docker logs",
    "pip install",
    "pip list",
    "python -m py_compile",
    "python -c",
    "pytest",
    "cat /proc/meminfo",
    "cat /proc/cpuinfo",
    "df -h",
    "free -m",
    "ps aux",
    "tail -n",
    "head -n",
    "wc -l",
    "grep",
    "find",
    "ls",
    "pwd",
    "whoami",
    "date",
    "uptime",
    "crontab -l",
    # Docker 확장 (AADS-190)
    "docker compose up",
    "docker compose down",
    "docker compose build",
    "docker compose pull",
    "docker exec",
    "docker images",
    "docker stats",
    "docker inspect",
    "docker network ls",
    "docker volume ls",
    # Nginx (AADS-190)
    "nginx -t",
    "nginx -s reload",
    "nginx -s stop",
    "cat /etc/nginx",
    # Supervisord (AADS-190)
    "supervisorctl status",
    "supervisorctl restart",
    "supervisorctl start",
    "supervisorctl stop",
    # 추가 시스템 도구
    "journalctl",
    "netstat -tlnp",
    "ss -tlnp",
    "curl -s",
    "wget -q",
    "top -bn1",
    "du -sh",
    "env",
    "cat /etc/os-release",
    # Git 명령 (AADS-190)
    "git status",
    "git log",
    "git diff",
    "git add",
    "git commit",
    "git push",
    "git pull",
    "git checkout",
    "git branch",
    "git stash",
    "git show",
    "git remote",
]

# run_remote_command 차단 패턴 (보안 하드코딩, LLM 우회 불가)
_REMOTE_CMD_BLOCKED = re.compile(
    r"(rm\s+-[rf]|mkfs|dd\s+if=|shutdown|halt|reboot|kill\s+-9\s+1\b"
    r"|>\s*/dev/|chmod\s+[0-7]{3,4}\s+/|pkill\s+-9"
    r"|DROP\s+(TABLE|DATABASE)|DELETE\s+FROM|TRUNCATE"
    r"|curl.*\|.*sh|wget.*\|.*sh|bash\s+-c"
    r"|\brm\b.*\s+/[a-z]"  # rm /anything 차단
    r"|:(){:|fork\s*bomb"
    r"|git\s+push\s+.*--force"  # force push 차단
    r"|git\s+reset\s+--hard"  # hard reset 차단
    r"|git\s+clean\s+-[fd])",  # clean 차단
    re.IGNORECASE,
)

# ─── 보안 상수 ─────────────────────────────────────────────────────────────────
_FILE_WHITELIST = "/root/aads/"
_FILE_BLACKLIST = ["/etc", "/proc", "/root/.ssh", "/root/.genspark/directives"]
_SQL_BLOCKED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|UNION|INTO\s+OUTFILE|LOAD_FILE)\b",
    re.IGNORECASE,
)
_MAX_LOG_BYTES = 10 * 1024   # 10 KB
_MAX_URL_BYTES = 20 * 1024   # 20 KB
_MAX_DB_ROWS = 50

# ─── Browser 보안 상수 (AADS-159, 하드코딩 — LLM 우회 불가) ──────────────
_BROWSER_ALLOWED_DOMAINS = frozenset([
    "aads.newtalk.kr",
    "github.com",
    "raw.githubusercontent.com",
    "localhost",
    "127.0.0.1",
])
_BROWSER_ALLOWED_SUFFIX = ".newtalk.kr"
_BROWSER_TIMEOUT_MS = 60_000   # 60초 세션 타임아웃
_BROWSER_MAX_TABS = 3          # 최대 3탭

# Playwright 싱글턴 (FastAPI event loop 내 유지)
_pw_handle = None
_pw_browser = None
_pw_context = None
_pw_init_lock: Optional[asyncio.Lock] = None


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


# ─── SSH 원격 접근 도구 함수 (AADS-165) ──────────────────────────────────────────

def _validate_ssh_path(raw_path: str, workdir: str) -> Optional[str]:
    """SSH 경로 보안 검증. 위반 시 에러 문자열, 통과 시 None."""
    if not _SSH_PATH_WHITELIST.match(raw_path):
        return "[ERROR] 접근 거부: 경로에 허용되지 않는 문자가 포함되어 있습니다."
    if _SSH_SENSITIVE_PATTERNS.search(raw_path):
        return "[ERROR] 접근 거부: 민감한 파일 패턴이 감지되었습니다."
    # WORKDIR 탈출 방지: .. resolve
    from posixpath import normpath, join as pjoin
    resolved = normpath(pjoin(workdir, raw_path))
    if not resolved.startswith(workdir):
        return f"[ERROR] 접근 거부: WORKDIR({workdir}) 바깥 경로 접근 불가."
    return None


async def tool_list_remote_dir(
    project: str, path: str = "", keyword: str = "", max_depth: int = 3
) -> str:
    """원격 서버 디렉터리 탐색 (읽기 전용, find)."""
    project = project.upper()
    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {', '.join(_PROJECT_SERVER_MAP.keys())}"

    server = mapping["server"]
    workdir = mapping["workdir"]
    max_depth = min(max(1, max_depth), _SSH_MAX_DEPTH)

    # 보안 검증
    if path:
        err = _validate_ssh_path(path, workdir)
        if err:
            return err
    if keyword and not _SSH_KEYWORD_WHITELIST.match(keyword):
        return "[ERROR] 접근 거부: keyword에 허용되지 않는 문자가 포함되어 있습니다."

    from posixpath import normpath, join as pjoin
    target = normpath(pjoin(workdir, path)) if path else workdir

    # find 명령 조립 (읽기 전용, shlex.quote로 인젝션 방지)
    find_cmd = f"find {shlex.quote(target)} -maxdepth {max_depth} -type f"
    if keyword:
        find_cmd += f" -name {shlex.quote('*' + keyword + '*')}"
    find_cmd += f" | head -{_SSH_MAX_FILES}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            f"root@{server}", find_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SSH_TIMEOUT)
        output = stdout.decode("utf-8", errors="replace")
        if not output.strip():
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            if err_msg:
                logger.warning(f"ssh_list_remote_dir_stderr project={project} err={err_msg}")
            return f"[{project}] 파일 없음 (경로: {target})"
        if len(output.encode("utf-8")) > _SSH_MAX_RESULT_BYTES:
            output = output[:_SSH_MAX_RESULT_BYTES] + "\n...(50KB 초과, 잘림)"
        return f"[{project} 디렉터리 — {target}]\n{output}"
    except asyncio.TimeoutError:
        return f"[ERROR] SSH 타임아웃 ({_SSH_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 접속 실패: {e}"


async def tool_read_remote_file(project: str, file_path: str) -> str:
    """원격 서버 파일 읽기 (읽기 전용, cat)."""
    project = project.upper()
    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {', '.join(_PROJECT_SERVER_MAP.keys())}"

    server = mapping["server"]
    workdir = mapping["workdir"]

    # 보안 검증
    err = _validate_ssh_path(file_path, workdir)
    if err:
        return err

    from posixpath import normpath, join as pjoin
    resolved = normpath(pjoin(workdir, file_path))

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            f"root@{server}", f"cat {shlex.quote(resolved)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SSH_TIMEOUT)
        output = stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            logger.warning(f"ssh_read_remote_file_failed project={project} path={resolved} err={err_msg}")
            return f"[ERROR] 파일 읽기 실패: 파일이 존재하지 않거나 읽기 권한이 없습니다."
        if len(output.encode("utf-8")) > _SSH_MAX_RESULT_BYTES:
            output = output[:_SSH_MAX_RESULT_BYTES] + "\n...(50KB 초과, 잘림)"
        return f"[{project} 파일 — {resolved}]\n{output}"
    except asyncio.TimeoutError:
        return f"[ERROR] SSH 타임아웃 ({_SSH_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 접속 실패: {e}"


# ─── SSH 원격 쓰기 도구 함수 (AADS-190: write_remote_file, patch_remote_file, run_remote_command) ───


async def tool_write_remote_file(project: str, file_path: str, content: str, backup: bool = True) -> str:
    """원격 서버 파일 쓰기 (SSH, 자동 백업 포함). Yellow 등급."""
    project = project.upper()
    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {', '.join(_PROJECT_SERVER_MAP.keys())}"

    server = mapping["server"]
    workdir = mapping["workdir"]

    if not file_path:
        return "[ERROR] file_path 필수"
    if not content:
        return "[ERROR] content 필수 (빈 파일 쓰기 차단)"

    # 크기 제한
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > _SSH_MAX_WRITE_BYTES:
        return f"[ERROR] 파일 크기 초과: {len(content_bytes):,} bytes > 1MB 제한"

    # 보안 검증 (읽기와 동일 경로 검증)
    err = _validate_ssh_path(file_path, workdir)
    if err:
        return err

    from posixpath import normpath, join as pjoin
    resolved = normpath(pjoin(workdir, file_path))

    # 추가 쓰기 보안: .env, .ssh, credentials 등 민감 파일 차단
    _write_blocked = [".env", ".ssh/", "id_rsa", "id_ed25519", "credentials",
                      "private_key", ".pem", ".key", "authorized_keys", ".netrc",
                      ".aws/", ".kube/", ".docker/"]
    for pattern in _write_blocked:
        if pattern in resolved.lower():
            return f"[ERROR] 민감 파일 쓰기 차단: {file_path}"

    try:
        # 1단계: 백업 (기존 파일이 있으면)
        if backup:
            backup_cmd = (
                f"test -f {shlex.quote(resolved)} && "
                f"cp {shlex.quote(resolved)} {shlex.quote(resolved + '.bak_aads')}"
            )
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                f"root@{server}", backup_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=_SSH_WRITE_TIMEOUT)

        # 2단계: 디렉토리 생성 + 파일 쓰기 (stdin pipe)
        from posixpath import dirname as pdirname
        mkdir_and_cat = f"mkdir -p {shlex.quote(pdirname(resolved))} && cat > {shlex.quote(resolved)}"
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            f"root@{server}", mkdir_and_cat,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=content_bytes), timeout=_SSH_WRITE_TIMEOUT
        )
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            logger.error(f"ssh_write_remote_file_failed project={project} path={resolved} err={err_msg}")
            return f"[ERROR] 파일 쓰기 실패: {err_msg}"

        logger.info(f"write_remote_file OK | project={project} path={resolved} size={len(content_bytes)}")
        backup_note = " (백업: .bak_aads)" if backup else ""
        return f"[{project} 파일 쓰기 완료 — {resolved}] {len(content_bytes):,} bytes{backup_note}"

    except asyncio.TimeoutError:
        return f"[ERROR] SSH 쓰기 타임아웃 ({_SSH_WRITE_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 쓰기 실패: {e}"


async def tool_patch_remote_file(project: str, file_path: str, old_string: str, new_string: str) -> str:
    """원격 서버 파일 부분 수정 (diff 기반 패치). Yellow 등급.
    old_string을 찾아 new_string으로 교체. 정확히 1개만 매치되어야 함."""
    project = project.upper()
    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {', '.join(_PROJECT_SERVER_MAP.keys())}"

    if not file_path:
        return "[ERROR] file_path 필수"
    if not old_string:
        return "[ERROR] old_string 필수"
    if old_string == new_string:
        return "[ERROR] old_string과 new_string이 동일"

    # 1단계: 현재 파일 읽기
    current = await tool_read_remote_file(project, file_path)
    if current.startswith("[ERROR]"):
        return current

    # read_remote_file 출력에서 헤더 제거하고 실제 내용만 추출
    lines = current.split("\n", 1)
    if len(lines) > 1 and lines[0].startswith(f"[{project}"):
        file_content = lines[1]
    else:
        file_content = current

    # 2단계: old_string 매치 확인
    count = file_content.count(old_string)
    if count == 0:
        return f"[ERROR] old_string을 찾을 수 없음 (파일에 해당 문자열 없음)"
    if count > 1:
        return f"[ERROR] old_string이 {count}회 중복 발견. 더 구체적인 문자열 필요"

    # 3단계: 교체 후 쓰기
    patched_content = file_content.replace(old_string, new_string, 1)
    result = await tool_write_remote_file(project, file_path, patched_content, backup=True)

    if result.startswith("[ERROR]"):
        return result

    # 변경 요약
    old_lines = old_string.count("\n") + 1
    new_lines = new_string.count("\n") + 1
    return f"[{project} 파일 패치 완료 — {file_path}] {old_lines}줄 → {new_lines}줄 교체\n{result}"


async def tool_run_remote_command(project: str, command: str) -> str:
    """원격 서버 명령 실행 (허용 명령 화이트리스트 기반). Yellow 등급."""
    project = project.upper()
    mapping = _PROJECT_SERVER_MAP.get(project)
    if not mapping:
        return f"[ERROR] 알 수 없는 프로젝트: {project}. 사용 가능: {', '.join(_PROJECT_SERVER_MAP.keys())}"

    server = mapping["server"]
    workdir = mapping["workdir"]

    if not command or not command.strip():
        return "[ERROR] command 필수"

    command = command.strip()

    # 보안 1: 차단 패턴 검사
    if _REMOTE_CMD_BLOCKED.search(command):
        logger.warning(f"run_remote_command BLOCKED | project={project} cmd={command[:120]}")
        return f"[ERROR] 위험 명령 차단: {command[:80]}"

    # 보안 2: 화이트리스트 검사 (명령 앞부분이 허용 목록에 있어야 함)
    cmd_allowed = False
    for allowed in _REMOTE_CMD_WHITELIST:
        if command.startswith(allowed):
            cmd_allowed = True
            break
    if not cmd_allowed:
        logger.warning(f"run_remote_command WHITELIST_DENY | project={project} cmd={command[:120]}")
        return (
            f"[ERROR] 허용되지 않은 명령: {command[:80]}\n"
            f"허용 명령 목록: {', '.join(sorted(set(c.split()[0] for c in _REMOTE_CMD_WHITELIST)))}"
        )

    # 보안 3: 파이프/리다이렉트/세미콜론 차단 (단일 명령만 허용)
    if any(c in command for c in ["|", ";", "&&", "||", "`", "$(", ">>"]):
        # 단, grep | head 같은 안전한 파이프는 허용
        if "|" in command:
            pipe_parts = command.split("|")
            for part in pipe_parts[1:]:
                part_cmd = part.strip().split()[0] if part.strip() else ""
                if part_cmd not in ("head", "tail", "wc", "grep", "sort", "uniq"):
                    return f"[ERROR] 파이프/체인 명령 차단 (보안): {command[:80]}"
        else:
            return f"[ERROR] 파이프/체인 명령 차단 (보안): {command[:80]}"

    # 실행: workdir에서 명령 수행
    full_cmd = f"cd {shlex.quote(workdir)} && {command}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            f"root@{server}", full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SSH_CMD_TIMEOUT)
        out = stdout.decode("utf-8", errors="replace")
        err_out = stderr.decode("utf-8", errors="replace")

        # 결과 크기 제한
        if len(out.encode("utf-8")) > _SSH_MAX_RESULT_BYTES:
            out = out[:_SSH_MAX_RESULT_BYTES] + "\n...(50KB 초과, 잘림)"

        result_parts = [f"[{project} 명령 실행 — exit={proc.returncode}]"]
        result_parts.append(f"$ {command}")
        if out.strip():
            result_parts.append(out.strip())
        if err_out.strip() and proc.returncode != 0:
            result_parts.append(f"[STDERR] {err_out.strip()[:2000]}")

        logger.info(f"run_remote_command OK | project={project} cmd={command[:80]} exit={proc.returncode}")
        return "\n".join(result_parts)

    except asyncio.TimeoutError:
        return f"[ERROR] SSH 명령 타임아웃 ({_SSH_CMD_TIMEOUT}초): {server}"
    except Exception as e:
        return f"[ERROR] SSH 명령 실행 실패: {e}"


# ─── Git 쓰기 도구 함수 (AADS-190: git_add, git_commit, git_push) ────────────


async def tool_git_remote_add(project: str, files: str = ".") -> str:
    """원격 서버 git add (스테이징)."""
    return await tool_run_remote_command(project, f"git add {files}")


async def tool_git_remote_commit(project: str, message: str) -> str:
    """원격 서버 git commit."""
    if not message or not message.strip():
        return "[ERROR] commit message 필수"
    # 메시지에서 위험 문자 제거
    safe_msg = message.replace("'", "\\'").replace('"', '\\"')[:200]
    return await tool_run_remote_command(project, f'git commit -m "{safe_msg}"')


async def tool_git_remote_push(project: str, branch: str = "") -> str:
    """원격 서버 git push (force push 차단)."""
    cmd = "git push"
    if branch:
        if not re.match(r'^[a-zA-Z0-9._/\-]+$', branch):
            return "[ERROR] 브랜치명에 허용되지 않는 문자"
        cmd += f" origin {branch}"
    return await tool_run_remote_command(project, cmd)


async def tool_git_remote_status(project: str) -> str:
    """원격 서버 git status."""
    return await tool_run_remote_command(project, "git status --short")


async def tool_git_remote_create_branch(project: str, branch_name: str) -> str:
    """원격 서버 새 브랜치 생성 및 체크아웃."""
    if not branch_name or not re.match(r'^[a-zA-Z0-9._/\-]+$', branch_name):
        return "[ERROR] 유효하지 않은 브랜치명"
    return await tool_run_remote_command(project, f"git checkout -b {branch_name}")


# ─── Browser 도구 함수 (AADS-159) ──────────────────────────────────────────────

def _browser_domain_ok(url: str) -> Optional[str]:
    """도메인 화이트리스트 검사. 차단이면 에러 문자열, 통과이면 None."""
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except Exception:
        return "[접근 차단] URL 파싱 실패"
    if hostname in _BROWSER_ALLOWED_DOMAINS:
        return None
    if hostname.endswith(_BROWSER_ALLOWED_SUFFIX):
        return None
    return f"[접근 차단] 허용되지 않은 도메인입니다: {hostname}"


async def _acquire_pw_context() -> Tuple[Any, Optional[str]]:
    """Playwright 컨텍스트 싱글턴 취득. 실패 시 (None, 에러메시지)."""
    global _pw_handle, _pw_browser, _pw_context, _pw_init_lock
    if _pw_init_lock is None:
        _pw_init_lock = asyncio.Lock()
    async with _pw_init_lock:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None, "[브라우저 도구 사용 불가] playwright 패키지가 설치되지 않았습니다."
        try:
            need_init = (
                _pw_context is None
                or _pw_browser is None
                or not _pw_browser.is_connected()
            )
            if need_init:
                if _pw_handle is not None:
                    try:
                        await _pw_handle.stop()
                    except Exception:
                        pass
                _pw_handle = await async_playwright().start()
                _pw_browser = await _pw_handle.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--memory-pressure-off",
                    ],
                )
                _pw_context = await _pw_browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    java_script_enabled=True,
                )
            return _pw_context, None
        except Exception as e:
            return None, f"[브라우저 도구 사용 불가] 초기화 실패: {e}"


async def _current_page(ctx: Any) -> Any:
    """현재(최신) 페이지 반환. 없으면 새 페이지 생성."""
    pages = ctx.pages
    return pages[-1] if pages else await ctx.new_page()


def _snapshot_to_text(node: Dict, depth: int = 0) -> str:
    """접근성 트리 노드를 들여쓰기 텍스트로 변환."""
    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    line = f"{indent}[{role}]{(' ' + name) if name else ''}{(' = ' + str(value)) if value else ''}"
    child_lines = "\n".join(
        _snapshot_to_text(c, depth + 1) for c in node.get("children", [])
    )
    return line + ("\n" + child_lines if child_lines else "")


async def _ensure_aads_auth(page: Any) -> None:
    """AADS 대시보드 인증 토큰 자동 주입 (내부 서비스용)."""
    try:
        from app.auth import create_token
        token = create_token(user_id="browser-agent", email="ceo@aads.dev")
        await page.evaluate(f"() => localStorage.setItem('aads_token', '{token}')")
    except Exception as e:
        logger.debug(f"browser auth inject failed: {e}")


async def _do_aads_login(page: Any) -> None:
    """AADS 대시보드 로그인 페이지에서 자동 로그인 수행."""
    import os
    email = os.getenv("AADS_ADMIN_EMAIL", "admin@aads.dev")
    password = os.getenv("AADS_ADMIN_PASSWORD", "")
    if not password:
        # 비밀번호 없으면 토큰 직접 주입 시도
        await _ensure_aads_auth(page)
        return

    # 이메일 입력 (첫 번째 input 필드)
    email_input = page.locator("input").first
    await email_input.clear(timeout=5000)
    await email_input.fill(email, timeout=5000)
    # 비밀번호 입력
    pw_input = page.locator("input[type='password']").first
    await pw_input.fill(password, timeout=5000)
    # 로그인 버튼 클릭
    login_btn = page.locator("button:has-text('로그인')").first
    await login_btn.click(timeout=5000)
    # 로그인 후 페이지 전환 대기
    await page.wait_for_timeout(3000)


async def tool_browser_navigate(url: str) -> str:
    """브라우저로 URL 이동 (도메인 화이트리스트 검사 포함)."""
    blocked = _browser_domain_ok(url)
    if blocked:
        return blocked
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        pages = ctx.pages
        if len(pages) >= _BROWSER_MAX_TABS:
            page = pages[-1]  # 마지막 탭 재사용
        else:
            page = await ctx.new_page()

        await page.goto(url, timeout=_BROWSER_TIMEOUT_MS, wait_until="domcontentloaded")

        # AADS 대시보드 로그인 리다이렉트 감지 → 자동 로그인
        if "/login" in page.url and "/login" not in url and "newtalk.kr" in url:
            try:
                await _do_aads_login(page)
                await page.goto(url, timeout=_BROWSER_TIMEOUT_MS, wait_until="domcontentloaded")
            except Exception as login_err:
                logger.warning(f"browser auto-login failed: {login_err}")

        title = await page.title()
        return f"[탐색 완료]\n제목: {title}\nURL: {page.url}"
    except Exception as e:
        return f"[ERROR] 브라우저 탐색 실패: {e}"


async def tool_browser_snapshot() -> str:
    """현재 페이지의 UI 구조를 텍스트로 추출 (LLM 최적)."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        url = page.url
        title = await page.title()

        # Playwright 1.47+ : page.accessibility 제거됨
        # aria snapshot 사용 (1.49+), 실패 시 DOM 텍스트 추출 폴백
        snap_text = ""
        try:
            snap_text = await page.locator("body").aria_snapshot()
        except Exception:
            pass

        if not snap_text:
            # 폴백: 주요 UI 요소 텍스트 추출
            elements = await page.evaluate("""() => {
                const items = [];
                const els = document.querySelectorAll(
                    'button, a, input, select, textarea, h1, h2, h3, h4, [role], label, nav, header, footer, main, aside'
                );
                for (const el of els) {
                    const tag = el.tagName.toLowerCase();
                    const role = el.getAttribute('role') || '';
                    const text = (el.textContent || '').trim().substring(0, 100);
                    const placeholder = el.getAttribute('placeholder') || '';
                    const type = el.getAttribute('type') || '';
                    const href = el.getAttribute('href') || '';
                    if (text || placeholder) {
                        items.push({tag, role, text, placeholder, type, href});
                    }
                    if (items.length >= 200) break;
                }
                return items;
            }""")
            lines = [f"[UI 요소 추출 — {url}]", f"제목: {title}", ""]
            for el in elements:
                parts = [f"<{el['tag']}>"]
                if el.get('role'):
                    parts.append(f"role={el['role']}")
                if el.get('type'):
                    parts.append(f"type={el['type']}")
                if el.get('text'):
                    parts.append(f"'{el['text'][:80]}'")
                if el.get('placeholder'):
                    parts.append(f"placeholder='{el['placeholder']}'")
                if el.get('href'):
                    parts.append(f"href={el['href'][:80]}")
                lines.append("  " + " ".join(parts))
            snap_text = "\n".join(lines)

        if len(snap_text) > 20_000:
            snap_text = snap_text[:20_000] + "\n...(20KB 초과, 잘림)"
        return snap_text if snap_text.startswith("[") else f"[ARIA 스냅샷 — {url}]\n{snap_text}"
    except Exception as e:
        return f"[ERROR] 스냅샷 실패: {e}"


async def tool_browser_screenshot() -> str:
    """현재 페이지 PNG 스크린샷 촬영 (base64 반환)."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        data = await page.screenshot(full_page=False, timeout=_BROWSER_TIMEOUT_MS)
        b64 = base64.b64encode(data).decode("ascii")
        return f"[스크린샷 PNG — base64]\nURL: {page.url}\nDATA:{b64}"
    except Exception as e:
        return f"[ERROR] 스크린샷 실패: {e}"


async def tool_browser_click(selector: str) -> str:
    """CSS selector로 요소 클릭."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        await page.click(selector, timeout=30_000)
        return f"[클릭 완료] selector={selector}"
    except Exception as e:
        return f"[ERROR] 클릭 실패 ({selector}): {e}"


async def tool_browser_fill(selector: str, value: str) -> str:
    """입력 필드에 텍스트 채우기."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        page = await _current_page(ctx)
        await page.fill(selector, value, timeout=30_000)
        return f"[입력 완료] selector={selector}, value='{value[:50]}'"
    except Exception as e:
        return f"[ERROR] 입력 실패 ({selector}): {e}"


async def tool_browser_tab_list() -> str:
    """열린 탭 목록 반환."""
    ctx, err = await _acquire_pw_context()
    if err:
        return err
    try:
        pages = ctx.pages
        if not pages:
            return f"(열린 탭 없음 — 최대 {_BROWSER_MAX_TABS}개)"
        lines = [f"[열린 탭 {len(pages)}/{_BROWSER_MAX_TABS}]"]
        for i, p in enumerate(pages):
            title = await p.title()
            lines.append(f"  [{i}] {title} — {p.url}")
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] 탭 목록 조회 실패: {e}"


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
    # ── Browser 도구 (AADS-159) ─────────────────────────────────────────────
    elif name == "browser_navigate":
        return await tool_browser_navigate(params.get("url", ""))
    elif name == "browser_snapshot":
        return await tool_browser_snapshot()
    elif name == "browser_screenshot":
        return await tool_browser_screenshot()
    elif name == "browser_click":
        return await tool_browser_click(params.get("selector", ""))
    elif name == "browser_fill":
        return await tool_browser_fill(params.get("selector", ""), params.get("value", ""))
    elif name == "browser_tab_list":
        return await tool_browser_tab_list()
    # ── SSH 원격 접근 도구 (AADS-165) ────────────────────────────────────────
    elif name == "list_remote_dir":
        return await tool_list_remote_dir(
            params.get("project", ""),
            params.get("path", ""),
            params.get("keyword", ""),
            params.get("max_depth", 3),
        )
    elif name == "read_remote_file":
        return await tool_read_remote_file(
            params.get("project", ""),
            params.get("file_path", ""),
        )
    else:
        return f"[ERROR] 알 수 없는 도구: {name}"
