"""
AADS-190: 프로젝트별 원격 DB 쿼리 도구.
CEO 채팅에서 KIS/GO100/SF/NTV2 등 외부 프로젝트 DB에 SELECT 쿼리 실행.

DB 매핑:
- KIS: PostgreSQL 16 (contabo14, 5.104.86.14:5432, kisautotrade)
- GO100: KIS와 동일 DB (kisautotrade)
- SF: MariaDB (cafe24_114, SSH 터널 → localhost:3306, autoda)
- NTV2: MySQL 8.0 Docker (cafe24_114, SSH 터널 → localhost:3307, newtalk_v2)

보안:
- SELECT/WITH/EXPLAIN만 허용 (DML/DDL 전면 차단)
- 세미콜론 다중 쿼리 차단
- 민감 컬럼(password, token, secret, api_key) 마스킹
- 결과 LIMIT 1000, 기본 100
- 쿼리 타임아웃 기본 90초 (환경변수로 조정)
- 연결 풀링 (PostgreSQL: asyncpg pool, MySQL: SSH 터널 + pymysql)
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.services.server_registry import get_server_host

logger = logging.getLogger(__name__)

# ─── 프로젝트별 DB 설정 ──────────────────────────────────────────────────────

_SUPPORTED_PROJECTS = ("AADS", "KIS", "GO100", "SF", "NTV2")

# GO100은 KIS와 동일 DB — 별칭 매핑
_PROJECT_ALIAS = {"GO100": "KIS"}

# DB 엔진 타입 (환경변수 {PROJECT}_DB_TYPE으로 오버라이드 가능)
_DEFAULT_DB_TYPE: Dict[str, str] = {
    "AADS": "postgresql",  # 내부 DB (Docker postgres)
    "KIS": "postgresql",
    "GO100": "postgresql",  # KIS 별칭
    "SF": "mysql",
    "NTV2": "mysql",
}

_DEFAULT_DB_ENDPOINT: Dict[str, Tuple[str, str]] = {
    "KIS": (get_server_host("contabo14"), "5432"),
    "SF": ("127.0.0.1", "3306"),
    "NTV2": ("127.0.0.1", "3307"),
}

_LEGACY_DB_HOST_ALIAS: Dict[str, str] = {
    "211.188.51.113": get_server_host("contabo14"),
}


def _env_value(names: Tuple[str, ...], default: str = "") -> str:
    """첫 번째로 설정된 환경변수 값을 반환한다."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _env_int(names: Tuple[str, ...], default: int) -> int:
    """정수 환경변수 파싱. 잘못된 값은 기존 기본값으로 폴백한다."""
    value = _env_value(names, "")
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_db_type(project: str) -> str:
    """DB 타입 조회. 별칭 해석 후 환경변수 오버라이드를 적용한다."""
    resolved = _PROJECT_ALIAS.get(project, project)
    return os.getenv(
        f"{resolved.upper()}_DB_TYPE",
        _DEFAULT_DB_TYPE.get(resolved, "postgresql"),
    )


def _ssh_tunnel_config(
    project: str,
    *,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    ssh_key: str,
    remote_host: str,
    remote_port: int,
) -> Dict[str, Any]:
    """프로젝트별 SSH 설정. 환경변수 없으면 기존 기본값 유지."""
    project = project.upper()
    return {
        "ssh_host": _env_value(
            (f"{project}_SSH_HOST", f"SSH_{project}_HOST", "SSH_HOST"),
            ssh_host,
        ),
        "ssh_port": _env_int(
            (f"{project}_SSH_PORT", f"SSH_{project}_PORT", "SSH_PORT"),
            ssh_port,
        ),
        "ssh_user": _env_value(
            (f"{project}_SSH_USER", f"SSH_{project}_USER", "SSH_USER"),
            ssh_user,
        ),
        "ssh_key": _env_value(
            (
                f"{project}_SSH_KEY_PATH",
                f"SSH_{project}_KEY_PATH",
                f"{project}_SSH_KEY",
                f"SSH_{project}_KEY",
                "SSH_KEY_PATH",
                "SSH_KEY",
            ),
            ssh_key,
        ),
        "remote_host": _env_value(
            (f"{project}_DB_HOST", f"SSH_{project}_REMOTE_HOST", "SSH_REMOTE_HOST"),
            remote_host,
        ),
        "remote_port": _env_int(
            (f"{project}_DB_PORT", f"SSH_{project}_REMOTE_PORT", "SSH_REMOTE_PORT"),
            remote_port,
        ),
    }


# SSH 터널 필요 프로젝트 (MySQL on cafe24_114)
_SSH_TUNNEL_PROJECTS: Dict[str, Dict[str, Any]] = {
    "SF": _ssh_tunnel_config(
        "SF",
        ssh_host=get_server_host("cafe24_114"),
        ssh_port=22,
        ssh_user="root",
        ssh_key="/root/.ssh/id_ed25519_newtalk",
        remote_host="127.0.0.1",
        remote_port=3306,
    ),
    "NTV2": _ssh_tunnel_config(
        "NTV2",
        ssh_host=get_server_host("cafe24_114"),
        ssh_port=22,
        ssh_user="root",
        ssh_key="/root/.ssh/id_ed25519_newtalk",
        remote_host="127.0.0.1",
        remote_port=3307,
    ),
}

_SENSITIVE_COLUMNS = re.compile(
    r"(password|passwd|pwd|secret|token|api_key|apikey|private_key|"
    r"access_key|refresh_token|session_key|auth_code|credentials)",
    re.IGNORECASE,
)

_FORBIDDEN_SQL = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"EXECUTE|EXEC|MERGE|REPLACE|CALL|SET|LOCK|UNLOCK|VACUUM|REINDEX|"
    r"COPY|LOAD|IMPORT)",
    re.IGNORECASE,
)

_ALLOWED_SQL_START = re.compile(
    r"^\s*(SELECT|WITH|EXPLAIN)\b",
    re.IGNORECASE,
)

_INJECTION_PATTERNS = [
    re.compile(r";\s*(SELECT|INSERT|UPDATE|DELETE|DROP)", re.IGNORECASE),
    re.compile(r"--\s*$", re.MULTILINE),
]

# C1: WITH CTE에서 DML 사용 차단
_DML_IN_CTE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b',
    re.IGNORECASE,
)

# ─── 연결 캐시 ───────────────────────────────────────────────────────────────

_pg_pools: Dict[str, Any] = {}       # asyncpg 풀
_pg_pool_refs: Dict[int, int] = {}   # pool.close 전 acquire 대기/사용 중인 요청 수
_pg_pool_closing: set[int] = set()
_SSH_TUNNEL_POOL: Dict[Tuple[str, int, str, int], Dict[str, Any]] = {}
_SSH_TUNNEL_IDLE_SECONDS = 300.0
_MAX_POOL_SIZE = 3
_PG_POOL_CONNECT_TIMEOUT_SECONDS = _env_int(("PROJECT_DB_PG_CONNECT_TIMEOUT_SECONDS",), 15)
_PG_POOL_ACQUIRE_TIMEOUT_SECONDS = _env_int(("PROJECT_DB_PG_ACQUIRE_TIMEOUT_SECONDS",), 20)
_PROJECT_DB_QUERY_TIMEOUT_SECONDS = _env_int(("PROJECT_DB_QUERY_TIMEOUT_SECONDS",), 90)
_MYSQL_CONNECT_TIMEOUT_SECONDS = _env_int(("PROJECT_DB_MYSQL_CONNECT_TIMEOUT_SECONDS",), 15)
_MYSQL_READ_TIMEOUT_SECONDS = _env_int(("PROJECT_DB_MYSQL_READ_TIMEOUT_SECONDS",), 90)
_pool_lock = asyncio.Lock()          # PG 풀 생성/재생성 경쟁 방지
_pool_drain_condition = asyncio.Condition(_pool_lock)
_ssh_tunnel_lock = threading.Lock()  # SSH 터널 생성/정리 경쟁 방지


class ProjectDbTimeoutError(TimeoutError):
    """Project DB timeout with the exact layer that failed."""

    def __init__(self, phase: str, timeout_seconds: int, message: str = "") -> None:
        self.phase = phase
        self.timeout_seconds = timeout_seconds
        super().__init__(message or f"{phase} exceeded {timeout_seconds}s")


# ─── 환경변수에서 DB 설정 조회 ────────────────────────────────────────────────

def _get_project_db_config(project: str) -> Optional[Dict[str, str]]:
    """환경변수에서 프로젝트 DB 접속 정보 조회. 별칭 자동 해석."""
    resolved = _PROJECT_ALIAS.get(project, project)
    prefix = resolved.upper()
    db_type = _get_db_type(resolved)
    default_host, default_port = _DEFAULT_DB_ENDPOINT.get(
        resolved,
        ("", "5432" if db_type == "postgresql" else "3306"),
    )
    database = os.getenv(f"{prefix}_DB_NAME", "")
    user = os.getenv(f"{prefix}_DB_USER", "")
    password = os.getenv(f"{prefix}_DB_PASSWORD", "")
    host = _env_value((f"{prefix}_DB_HOST",), "")
    if not host and (database or user or password):
        host = default_host
    if not host:
        return None
    normalized_host = _LEGACY_DB_HOST_ALIAS.get(host, host)
    if normalized_host != host:
        logger.warning(
            "query_project_database: legacy DB host remapped | project=%s host=%s -> %s",
            project,
            host,
            normalized_host,
        )
        host = normalized_host
    return {
        "host": host,
        "port": _env_value((f"{prefix}_DB_PORT",), default_port),
        "database": database,
        "user": user,
        "password": password,
        "type": db_type,
    }


# ─── SQL 검증 ────────────────────────────────────────────────────────────────

def validate_query(query: str) -> Optional[str]:
    """SQL 쿼리 검증. 문제 시 에러 메시지, 통과 시 None."""
    q = query.strip()
    if not q:
        return "쿼리가 비어있습니다"
    if len(q) > 10000:
        return "쿼리가 10000자를 초과합니다"
    if not _ALLOWED_SQL_START.match(q):
        return "SELECT, WITH, EXPLAIN 쿼리만 허용됩니다"
    if _FORBIDDEN_SQL.match(q):
        return "INSERT/UPDATE/DELETE/DROP 등 변경 쿼리는 차단됩니다"

    cleaned = re.sub(r"'[^']*'", "", q)
    cleaned = re.sub(r'"[^"]*"', "", cleaned)
    if cleaned.count(";") > 1:
        return "다중 쿼리(세미콜론 2개 이상)는 차단됩니다"
    if ";" in cleaned:
        after = cleaned.split(";", 1)[1].strip()
        if after:
            return "세미콜론 뒤에 추가 쿼리가 감지되었습니다"

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(q):
            return "잠재적 SQL 인젝션 패턴이 감지되었습니다"

    # C1: string literals 제거 후 DML 키워드 검사 (WITH CTE 악용 차단)
    stripped_for_dml = re.sub(r"'[^']*'", "", cleaned)
    if _DML_IN_CTE.search(stripped_for_dml):
        return "쿼리에 금지된 DML 키워드가 포함되어 있습니다"

    # CEO 지시: self-join / CROSS JOIN / WHERE 강제 제거 — 자유로운 분석 쿼리 허용
    # 안전장치: auto LIMIT은 유지 (쿼리에 없으면 자동 추가)

    return None


# ─── 공통 유틸 ────────────────────────────────────────────────────────────────

def _mask_sensitive_values(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """민감 컬럼 값 마스킹."""
    masked = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            if _SENSITIVE_COLUMNS.search(k) and v is not None:
                new_row[k] = "****MASKED****"
            else:
                new_row[k] = v
        masked.append(new_row)
    return masked


def _serialize_value(v: Any) -> Any:
    """JSON 직렬화 불가 타입 변환."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return f"<binary {len(v)} bytes>"
    if isinstance(v, (int, float, str, bool)):
        return v
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


# ─── PostgreSQL 연결 (asyncpg) ────────────────────────────────────────────────

def _is_pg_pool_open(pool: Any) -> bool:
    """asyncpg Pool이 acquire 가능한 상태인지 확인."""
    return pool is not None and not getattr(pool, "_closed", False)


def _should_recreate_pg_pool(exc: Exception) -> bool:
    """stale pool/connection 계열 오류만 1회 재생성 대상으로 분류."""
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return True

    exc_name = exc.__class__.__name__
    if exc_name in {
        "InterfaceError",
        "InternalClientError",
        "ConnectionDoesNotExistError",
        "CannotConnectNowError",
        "TooManyConnectionsError",
    }:
        return True

    message = str(exc).lower()
    return "pool is closed" in message or "connection is closed" in message


async def _discard_pg_pool(project: str, pool: Any = None) -> None:
    """캐시된 PG 풀을 제거하고 현재 acquire 대기/사용 요청이 빠질 때까지 드레인한다."""
    resolved = _PROJECT_ALIAS.get(project, project)
    pool_to_close = None
    pool_id = None

    async with _pool_drain_condition:
        cached_pool = _pg_pools.get(resolved)
        if pool is None or cached_pool is pool:
            pool_to_close = _pg_pools.pop(resolved, None)
        if not pool_to_close:
            return

        pool_id = id(pool_to_close)
        if pool_id in _pg_pool_closing:
            return

        _pg_pool_closing.add(pool_id)
        while _pg_pool_refs.get(pool_id, 0) > 0:
            await _pool_drain_condition.wait()

    try:
        if pool_to_close and _is_pg_pool_open(pool_to_close):
            await pool_to_close.close()
    except Exception:
        logger.exception(f"query_project_database: PG 풀 종료 실패 | {project}({resolved})")
    finally:
        async with _pool_drain_condition:
            _pg_pool_closing.discard(pool_id)
            _pg_pool_refs.pop(pool_id, None)


async def _get_pg_pool(project: str):
    """PostgreSQL asyncpg 풀 반환 (캐싱). H2: Lock으로 경쟁 방지."""
    import asyncpg

    resolved = _PROJECT_ALIAS.get(project, project)

    async with _pool_lock:
        pool = _pg_pools.get(resolved)
        if _is_pg_pool_open(pool):
            return pool
        _pg_pools.pop(resolved, None)

        config = _get_project_db_config(project)
        if not config or not config["database"]:
            raise ValueError(f"프로젝트 {project} DB 설정 없음")

        try:
            dsn = (
                f"postgresql://{config['user']}:{config['password']}"
                f"@{config['host']}:{config['port']}/{config['database']}"
            )
            pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=_MAX_POOL_SIZE,
                command_timeout=_PROJECT_DB_QUERY_TIMEOUT_SECONDS,
                timeout=_PG_POOL_CONNECT_TIMEOUT_SECONDS,
            )
        except Exception:
            # H3: DSN에 credentials 포함 — 상세 에러 로그만 남기고 안전한 메시지 반환
            logger.exception(f"query_project_database: PG 풀 생성 실패 | {project}({resolved})")
            raise ConnectionError(f"프로젝트 {resolved} PostgreSQL 연결 실패") from None

        _pg_pools[resolved] = pool
        logger.info(f"query_project_database: PG 풀 생성 | {project}({resolved})")
        return pool


@asynccontextmanager
async def _borrow_pg_pool(project: str):
    """풀 acquire 대기 중인 요청까지 드레인 대상으로 추적한다."""
    resolved = _PROJECT_ALIAS.get(project, project)
    pool = None
    pool_id = None

    while True:
        candidate = await _get_pg_pool(project)
        async with _pool_drain_condition:
            if _pg_pools.get(resolved) is candidate and _is_pg_pool_open(candidate):
                pool = candidate
                pool_id = id(candidate)
                _pg_pool_refs[pool_id] = _pg_pool_refs.get(pool_id, 0) + 1
                break
        await asyncio.sleep(0)

    try:
        yield pool
    finally:
        async with _pool_drain_condition:
            refs = _pg_pool_refs.get(pool_id, 0)
            if refs <= 1:
                _pg_pool_refs.pop(pool_id, None)
            else:
                _pg_pool_refs[pool_id] = refs - 1
            _pool_drain_condition.notify_all()


async def _query_postgresql(project: str, q: str) -> List[Dict[str, Any]]:
    """PostgreSQL 쿼리 실행. C2: read-only 트랜잭션으로 안전하게 실행."""
    last_error: Optional[Exception] = None

    for attempt in range(2):
        pool = None
        try:
            async with _borrow_pg_pool(project) as pool:
                if not _is_pg_pool_open(pool):
                    raise ConnectionError("PostgreSQL pool is closed")
                try:
                    conn_cm = pool.acquire(timeout=_PG_POOL_ACQUIRE_TIMEOUT_SECONDS)
                    conn = await conn_cm.__aenter__()
                except asyncio.TimeoutError as exc:
                    raise ProjectDbTimeoutError(
                        "pool_acquire_timeout",
                        _PG_POOL_ACQUIRE_TIMEOUT_SECONDS,
                        "PostgreSQL pool acquire timeout",
                    ) from exc
                try:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL default_transaction_read_only = on")
                        await conn.execute(
                            "SELECT set_config('statement_timeout', $1, true)",
                            str(max(1, _PROJECT_DB_QUERY_TIMEOUT_SECONDS) * 1000),
                        )
                        try:
                            rows = await conn.fetch(q, timeout=_PROJECT_DB_QUERY_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError as exc:
                            raise ProjectDbTimeoutError(
                                "query_statement_timeout",
                                _PROJECT_DB_QUERY_TIMEOUT_SECONDS,
                                "PostgreSQL query timeout",
                            ) from exc
                        except Exception as exc:
                            if exc.__class__.__name__ in {"QueryCanceledError", "TimeoutError"}:
                                raise ProjectDbTimeoutError(
                                    "query_statement_timeout",
                                    _PROJECT_DB_QUERY_TIMEOUT_SECONDS,
                                    str(exc),
                                ) from exc
                            raise
                    return [{k: _serialize_value(v) for k, v in dict(r).items()} for r in rows]
                finally:
                    await conn_cm.__aexit__(None, None, None)
        except Exception as exc:
            if attempt == 0 and _should_recreate_pg_pool(exc):
                logger.warning(
                    f"query_project_database: PG 풀 재생성 후 재시도 | {project} "
                    f"error={exc.__class__.__name__}"
                )
                if pool is not None:
                    await _discard_pg_pool(project, pool)
                else:
                    await _discard_pg_pool(project)
                last_error = exc
                continue
            raise

    if last_error:
        raise last_error
    raise ConnectionError("PostgreSQL pool acquire failed")


# ─── MySQL 연결 (SSH 터널 subprocess + pymysql) ──────────────────────────────


def _find_free_port() -> int:
    """빈 TCP 포트 찾기."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ssh_tunnel_pool_key(tunnel_config: Dict[str, Any]) -> Tuple[str, int, str, int]:
    """SSH 터널 풀 키: SSH endpoint + 원격 DB endpoint."""
    return (
        str(tunnel_config["ssh_host"]),
        int(tunnel_config["ssh_port"]),
        str(tunnel_config["remote_host"]),
        int(tunnel_config["remote_port"]),
    )


def _is_ssh_tunnel_usable(info: Dict[str, Any]) -> bool:
    """프로세스와 로컬 포워드 포트가 모두 살아있는지 확인한다."""
    proc = info.get("process")
    if not proc or proc.poll() is not None:
        return False

    try:
        local_port = int(info.get("local_port", 0))
    except (TypeError, ValueError):
        return False
    if local_port <= 0:
        return False

    try:
        with socket.create_connection(("127.0.0.1", local_port), timeout=1):
            return True
    except OSError:
        return False


def _redact_ssh_key_paths(message: str) -> str:
    """로그/응답에 SSH 키 경로가 노출되지 않도록 치환한다."""
    safe = message or ""
    key_paths = {
        "/root/.ssh/id_ed25519",
        "/root/.ssh/id_rsa",
    }
    for config in _SSH_TUNNEL_PROJECTS.values():
        key_path = str(config.get("ssh_key", ""))
        if key_path:
            key_paths.add(key_path)
            key_paths.add(os.path.expanduser(key_path))
    for key_path in key_paths:
        safe = safe.replace(key_path, "<ssh-key>")
    return safe


def _sanitize_ssh_error(message: str) -> str:
    """SSH 오류 메시지에서 키 경로를 제거한다."""
    return _redact_ssh_key_paths((message or "").strip().replace("\n", " ")[:200])


def _terminate_process(proc: subprocess.Popen) -> None:
    """터널 프로세스를 정상 종료 요청한다."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        logger.warning("query_project_database: SSH 터널 종료 대기 시간 초과")


def _cleanup_idle_ssh_tunnels(now: Optional[float] = None) -> None:
    """5분 이상 미사용 또는 이미 종료된 SSH 터널을 다음 호출에서 정리."""
    now = time.monotonic() if now is None else now
    for pool_key, info in list(_SSH_TUNNEL_POOL.items()):
        proc = info.get("process")
        if not proc or proc.poll() is not None:
            _SSH_TUNNEL_POOL.pop(pool_key, None)
            continue

        last_used = float(info.get("last_used", 0.0))
        if now - last_used > _SSH_TUNNEL_IDLE_SECONDS:
            _SSH_TUNNEL_POOL.pop(pool_key, None)
            _terminate_process(proc)
            logger.info(f"query_project_database: SSH 터널 idle 정리 | key={pool_key}")


def _drop_ssh_tunnel(project: str) -> None:
    """문제 있는 터널을 풀에서 제거한다."""
    tunnel_config = _SSH_TUNNEL_PROJECTS.get(project)
    if not tunnel_config:
        return

    pool_key = _ssh_tunnel_pool_key(tunnel_config)
    with _ssh_tunnel_lock:
        info = _SSH_TUNNEL_POOL.pop(pool_key, None)
        if info and info.get("process"):
            _terminate_process(info["process"])


def _should_recreate_ssh_tunnel(exc: Exception) -> bool:
    """SSH 터널 단절로 볼 수 있는 MySQL 연결 오류만 재시도 대상으로 분류."""
    if isinstance(exc, OSError):
        return True

    code = exc.args[0] if getattr(exc, "args", None) else None
    if code in {2002, 2003, 2006, 2013}:
        return True

    if exc.__class__.__name__ == "InterfaceError":
        return True

    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "connection refused",
            "can't connect",
            "lost connection",
            "server has gone away",
            "connection reset",
        )
    )


def _ensure_ssh_tunnel(project: str) -> int:
    """SSH 터널 subprocess 시작/재사용. 로컬 포트 반환."""
    tunnel_config = _SSH_TUNNEL_PROJECTS.get(project)
    if not tunnel_config:
        raise ValueError(f"프로젝트 {project}의 SSH 터널 설정 없음")

    pool_key = _ssh_tunnel_pool_key(tunnel_config)

    with _ssh_tunnel_lock:
        now = time.monotonic()
        _cleanup_idle_ssh_tunnels(now)

        info = _SSH_TUNNEL_POOL.get(pool_key)
        if info:
            if _is_ssh_tunnel_usable(info):
                info["last_used"] = now
                return int(info["local_port"])
            stale = _SSH_TUNNEL_POOL.pop(pool_key, None)
            if stale and stale.get("process"):
                _terminate_process(stale["process"])

        # SSH 키 찾기
        ssh_key = os.path.expanduser(str(tunnel_config["ssh_key"]))
        if not os.path.exists(ssh_key):
            for alt_key in ["/root/.ssh/id_ed25519", "/root/.ssh/id_rsa"]:
                if os.path.exists(alt_key):
                    ssh_key = alt_key
                    break

        local_port = _find_free_port()
        remote_host = tunnel_config["remote_host"]
        remote_port = tunnel_config["remote_port"]
        ssh_host = tunnel_config["ssh_host"]
        ssh_port = int(tunnel_config["ssh_port"])
        ssh_user = tunnel_config["ssh_user"]

        cmd = [
            "ssh", "-N", "-L",
            f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
            "-i", ssh_key,
            "-p", str(ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ConnectTimeout=10",
            f"{ssh_user}@{ssh_host}",
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # 터널 연결 대기
        for _ in range(20):
            time.sleep(0.3)
            if _is_ssh_tunnel_usable({"process": proc, "local_port": local_port}):
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                safe_stderr = _sanitize_ssh_error(stderr)
                detail = f": {safe_stderr}" if safe_stderr else ""
                raise RuntimeError(f"SSH 터널 시작 실패{detail}")
        else:
            _terminate_process(proc)
            raise RuntimeError("SSH 터널 연결 타임아웃 (6초)")

        _SSH_TUNNEL_POOL[pool_key] = {
            "process": proc,
            "local_port": local_port,
            "last_used": time.monotonic(),
        }
        logger.info(
            f"query_project_database: SSH 터널 생성 | {project} "
            f"local:{local_port} -> {ssh_host}:{ssh_port}/{remote_host}:{remote_port}"
        )
        return local_port


def _query_mysql_sync(project: str, q: str, config: Dict[str, str]) -> List[Dict[str, Any]]:
    """MySQL 쿼리 실행 (동기, SSH 터널 경유)."""
    import pymysql

    for attempt in range(2):
        local_port = _ensure_ssh_tunnel(project)
        try:
            conn = pymysql.connect(
                host="127.0.0.1",
                port=local_port,
                user=config["user"],
                password=config["password"],
                database=config["database"],
                charset="utf8mb4",
                connect_timeout=_MYSQL_CONNECT_TIMEOUT_SECONDS,
                read_timeout=_MYSQL_READ_TIMEOUT_SECONDS,
                cursorclass=pymysql.cursors.DictCursor,
            )
            try:
                with conn.cursor() as cursor:
                    cursor.execute(q)
                    rows = cursor.fetchall()
                    return [{k: _serialize_value(v) for k, v in row.items()} for row in rows]
            finally:
                conn.close()
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError, OSError) as exc:
            if attempt == 0 and _should_recreate_ssh_tunnel(exc):
                logger.warning(f"query_project_database: SSH 터널 재생성 후 재시도 | {project}")
                _drop_ssh_tunnel(project)
                continue
            raise

    raise ConnectionError(f"프로젝트 {project} MySQL 연결 실패")


async def _query_mysql(project: str, q: str, db_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """MySQL 쿼리 실행 (async wrapper). db_name으로 DB 오버라이드 가능."""
    # NTV2에서 autoda(V1 DB) 접근 시 → SF 터널(port 3306, 호스트 MariaDB) 경유
    tunnel_project = project
    if project == "NTV2" and db_name and db_name.lower() == "autoda":
        tunnel_project = "SF"
        config = _get_project_db_config("SF")
        if config:
            config = dict(config)
            config["database"] = db_name
        else:
            raise ValueError("SF DB 설정 없음 (NTV2 V1 autoda 접근용)")
    else:
        config = _get_project_db_config(project)
        if not config or not config["database"]:
            raise ValueError(f"프로젝트 {project} DB 설정 없음")
        if db_name:
            config = dict(config)
            config["database"] = db_name

    return await asyncio.to_thread(_query_mysql_sync, tunnel_project, q, config)


# ─── H1: 셧다운 시 SSH 터널/PG 풀 정리 ────────────────────────────────────────

async def close_all_project_connections():
    """서버 종료 시 모든 프로젝트 DB 연결 및 SSH 터널 정리."""
    for key in list(_pg_pools.keys()):
        try:
            await _discard_pg_pool(key)
        except Exception:
            pass

    for key, info in list(_SSH_TUNNEL_POOL.items()):
        try:
            _terminate_process(info["process"])
        except Exception:
            pass
    _SSH_TUNNEL_POOL.clear()

    logger.info("close_all_project_connections: 모든 프로젝트 DB 연결 정리 완료")


# ─── 메인 쿼리 함수 ──────────────────────────────────────────────────────────

async def query_project_database(
    project: str,
    query: str,
    limit: int = 100,
    db_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    프로젝트별 원격 DB에 SELECT 쿼리 실행.

    Args:
        project: KIS, GO100, SF, NTV2
        query: SELECT SQL 쿼리
        limit: 반환 행 수 (기본 100, 최대 1000)
        db_name: DB 이름 (미지정 시 프로젝트 메인 DB)

    Returns:
        {"project": str, "rows": list, "row_count": int, "columns": list}
    """
    project = project.upper().strip()
    if project not in _SUPPORTED_PROJECTS:
        return {"error": f"지원 프로젝트: {', '.join(_SUPPORTED_PROJECTS)}"}

    error = validate_query(query)
    if error:
        return {"error": error}

    limit = max(1, min(limit, 1000))
    q = query.strip().rstrip(";")
    if "LIMIT" not in q.upper():
        q += f" LIMIT {limit}"

    try:
        resolved = _PROJECT_ALIAS.get(project, project)
        db_type = _get_db_type(resolved)

        if db_type == "postgresql":
            result_rows = await asyncio.wait_for(
                _query_postgresql(project, q),
                timeout=max(1, _PROJECT_DB_QUERY_TIMEOUT_SECONDS + _PG_POOL_ACQUIRE_TIMEOUT_SECONDS + 5),
            )
        else:
            result_rows = await asyncio.wait_for(
                _query_mysql(resolved, q, db_name=db_name),
                timeout=max(1, _MYSQL_CONNECT_TIMEOUT_SECONDS + _MYSQL_READ_TIMEOUT_SECONDS + 10),
            )

        result_rows = _mask_sensitive_values(result_rows)
        columns = list(result_rows[0].keys()) if result_rows else []

        logger.info(
            f"query_project_database: OK | project={project} "
            f"db_type={db_type} rows={len(result_rows)} query={q[:80]}"
        )

        return {
            "project": project,
            "db_type": db_type,
            "rows": result_rows,
            "row_count": len(result_rows),
            "columns": columns,
            "query": q,
            "timeout_seconds": _PROJECT_DB_QUERY_TIMEOUT_SECONDS if db_type == "postgresql" else _MYSQL_READ_TIMEOUT_SECONDS,
        }

    except ProjectDbTimeoutError as e:
        logger.error(
            "query_project_database: TIMEOUT | project=%s phase=%s timeout=%ss query=%s",
            project,
            e.phase,
            e.timeout_seconds,
            q[:80],
        )
        return {
            "error": f"DB 쿼리 시간 초과 ({project}): {e.timeout_seconds}초 초과",
            "error_code": e.phase,
            "project": project,
            "timeout_seconds": e.timeout_seconds,
            "timeout_policy": {
                "tool_executor_timeout_seconds": int(os.getenv("AADS_DATABASE_TOOL_TIMEOUT_SECONDS", "125")),
                "pool_acquire_timeout_seconds": _PG_POOL_ACQUIRE_TIMEOUT_SECONDS,
                "query_statement_timeout_seconds": _PROJECT_DB_QUERY_TIMEOUT_SECONDS,
            },
            "query": q,
            "hint": "조건/기간을 줄이거나 요약 테이블/인덱스를 사용하십시오.",
        }

    except asyncio.TimeoutError:
        timeout_sec = _PROJECT_DB_QUERY_TIMEOUT_SECONDS if _get_db_type(_PROJECT_ALIAS.get(project, project)) == "postgresql" else _MYSQL_READ_TIMEOUT_SECONDS
        logger.error(
            f"query_project_database: TIMEOUT | project={project} timeout={timeout_sec}s query={q[:80]}"
        )
        return {
            "error": f"DB 쿼리 시간 초과 ({project}): {timeout_sec}초 초과",
            "error_code": "tool_executor_or_driver_timeout",
            "project": project,
            "timeout_seconds": timeout_sec,
            "timeout_policy": {
                "tool_executor_timeout_seconds": int(os.getenv("AADS_DATABASE_TOOL_TIMEOUT_SECONDS", "125")),
                "pool_acquire_timeout_seconds": _PG_POOL_ACQUIRE_TIMEOUT_SECONDS,
                "query_statement_timeout_seconds": _PROJECT_DB_QUERY_TIMEOUT_SECONDS,
            },
            "query": q,
            "hint": "조건/기간을 줄이거나 요약 테이블/인덱스를 사용하십시오.",
        }

    except Exception as e:
        # H3: credentials가 포함될 수 있는 에러 메시지는 로그에만 기록
        safe_msg = _redact_ssh_key_paths(str(e))
        logger.error(
            f"query_project_database: FAIL | project={project} error={safe_msg}"
        )
        # DSN/credentials 패턴 제거
        if any(kw in safe_msg.lower() for kw in ("password", "postgresql://", "mysql://", "credentials")):
            safe_msg = "연결 오류가 발생했습니다 (상세 내용은 서버 로그 참조)"
        return {"error": f"DB 쿼리 실패 ({project}): {safe_msg}"}


# ─── DB 목록 조회 ─────────────────────────────────────────────────────────────

async def list_project_databases() -> Dict[str, Any]:
    """설정된 프로젝트 DB 목록 및 연결 상태 조회."""
    result = {}
    for project in _SUPPORTED_PROJECTS:
        config = _get_project_db_config(project)
        alias = _PROJECT_ALIAS.get(project)
        db_type = _get_db_type(_PROJECT_ALIAS.get(project, project))

        if not config or not config["host"]:
            result[project] = {"status": "not_configured"}
            continue

        info = {
            "host": config["host"],
            "port": config["port"],
            "database": config["database"],
            "db_type": db_type,
        }
        if alias:
            info["alias_of"] = alias

        try:
            if db_type == "postgresql":
                async with _borrow_pg_pool(project) as pool:
                    async with pool.acquire(timeout=10) as conn:
                        version = await conn.fetchval("SELECT version()")
                info["status"] = "connected"
                info["version"] = version[:60] if version else "unknown"
            else:
                # MySQL: 간단한 연결 테스트
                rows = await _query_mysql(
                    _PROJECT_ALIAS.get(project, project),
                    "SELECT version() as v"
                )
                info["status"] = "connected"
                info["version"] = rows[0]["v"][:60] if rows else "unknown"
        except Exception as e:
            info["status"] = "error"
            info["error"] = _redact_ssh_key_paths(str(e))[:150]

        result[project] = info
    return result
