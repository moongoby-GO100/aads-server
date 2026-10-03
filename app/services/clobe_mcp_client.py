"""클로브AI MCP OAuth 연결(읽기 전용).

클로브 MCP 는 표준 MCP OAuth(PKCE S256, public client, 동적 등록)다. scope 에 읽기 전용이
없으므로 읽기 전용은 여기서 강제한다: tools/list 로 실제 확인된 조회형 도구만 허용하고,
쓰기·발급·송금·신고 계열과 미확인 도구는 네트워크 호출 전에 거부한다.

토큰·code·code_verifier·state 원문은 로그/응답/예외 메시지에 남기지 않는다.
토큰은 Fernet(app.core.credential_vault.encrypt_value)으로 암호화해 저장한다.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from app.core.credential_vault import decrypt_value, encrypt_value
from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

CLOBE_MCP_URL = os.getenv("CLOBE_MCP_URL", "https://api.clobe.ai/mcp")
CLOBE_AUTHORIZE_URL = os.getenv("CLOBE_OAUTH_AUTHORIZE_URL", "https://api.clobe.ai/oauth/authorize")
CLOBE_TOKEN_URL = os.getenv("CLOBE_OAUTH_TOKEN_URL", "https://api.clobe.ai/oauth/token")
CLOBE_REGISTER_URL = os.getenv("CLOBE_OAUTH_REGISTER_URL", "https://api.clobe.ai/oauth/register")
CLOBE_REVOKE_URL = os.getenv("CLOBE_OAUTH_REVOKE_URL", "https://api.clobe.ai/oauth/revoke")
CLOBE_ALLOWED_HOSTS = frozenset({"api.clobe.ai"})
CLOBE_SCOPE = "mcp offline_access"
MCP_PROTOCOL_VERSION = "2025-06-18"
STATE_TTL = timedelta(minutes=10)
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
CONNECTION_ID = "default"
_REFRESH_LOCK_KEY = 0x636C6F6265  # advisory lock key ("clobe")
_REFRESH_SKEW = timedelta(seconds=60)

STATUS_NOT_CONNECTED = "not_connected"
STATUS_CONNECTED = "connected"
STATUS_REAUTH = "reauth_required"
STATUS_REVOKED = "revoked"


class ClobeError(RuntimeError):
    """메시지에는 토큰·code·응답 본문을 넣지 않는다 — 분류 코드만."""


class ClobeToolDenied(ClobeError):
    pass


class ClobeReauthRequired(ClobeError):
    pass


# initialize·tools/call 의 401 은 모두 이 메시지로 올라온다. 갱신 재시도 경로는 이 값만 탄다.
# 토큰 갱신 자체의 실패(refresh_failed:* 등)는 다른 메시지라 재시도 없이 재동의로 간다.
UNAUTHORIZED = "mcp_unauthorized"


class ClobeTransientError(ClobeError):
    """재시도하면 풀릴 수 있는 장애(네트워크·429·5xx). 상태를 바꾸지 않는다."""


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS clobe_mcp_oauth_client (
    redirect_uri TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS clobe_mcp_oauth_state (
    state_hash        TEXT PRIMARY KEY,
    code_verifier_enc TEXT NOT NULL,
    redirect_uri      TEXT NOT NULL,
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    consumed_at       TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS clobe_mcp_connection (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    client_id        TEXT,
    access_token_enc TEXT,
    refresh_token_enc TEXT,
    token_expires_at TIMESTAMPTZ,
    scope            TEXT,
    connected_at     TIMESTAMPTZ,
    last_success_at  TIMESTAMPTZ,
    last_error       TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS clobe_mcp_tools (
    name         TEXT PRIMARY KEY,
    description  TEXT,
    input_schema JSONB,
    annotations  JSONB,
    allowed      BOOLEAN NOT NULL DEFAULT FALSE,
    deny_reason  TEXT,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with get_pool().acquire() as conn:
        await conn.execute(SCHEMA_DDL)
    _schema_ready = True


# ── 토큰 마스킹 ─────────────────────────────────────────

def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    return f"***({len(value)})"


def _safe_oauth_error(resp: httpx.Response) -> str:
    """OAuth 오류 코드(RFC 6749 §5.2)만 꺼낸다. 본문 나머지는 버린다."""
    try:
        code = str(resp.json().get("error", ""))
    except Exception:  # noqa: BLE001 - 본문 형식 무관
        return f"http_{resp.status_code}"
    return re.sub(r"[^a-z_]", "", code.lower())[:40] or f"http_{resp.status_code}"


def _require_clobe_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in CLOBE_ALLOWED_HOSTS:
        raise ClobeError("clobe_url_not_allowed")
    return url


# ── PKCE / state ────────────────────────────────────────

def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86자, RFC 7636 범위 43~128


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state() -> str:
    return secrets.token_urlsafe(32)


def hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def redirect_uri() -> str:
    explicit = os.getenv("CLOBE_OAUTH_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = os.getenv("AADS_PUBLIC_BASE_URL", "https://aads.newtalk.kr").rstrip("/")
    return f"{base}/api/v1/integrations/clobe/oauth/callback"


def build_authorize_url(*, client_id: str, redirect: str, state: str, challenge: str) -> str:
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": CLOBE_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": CLOBE_MCP_URL,
    })
    return f"{_require_clobe_url(CLOBE_AUTHORIZE_URL)}?{query}"


# ── 도구 허용목록(읽기 전용 강제) ─────────────────────────

_READ_VERBS = frozenset({
    "get", "list", "search", "query", "read", "fetch", "find", "view", "describe",
    "lookup", "show", "count", "summarize", "summary", "check",
})
# 어느 위치에 있든 하나라도 있으면 거부한다. 거짓 거부는 안전한 쪽이다.
_DENY_TOKENS = frozenset({
    "create", "update", "delete", "remove", "write", "edit", "modify", "set", "add",
    "insert", "upload", "send", "issue", "publish", "transfer", "remit", "pay",
    "withdraw", "submit", "file", "filing", "declare", "register", "cancel", "approve",
    "sign", "execute", "run", "post", "put", "patch", "import", "sync", "connect",
    "disconnect", "revoke", "apply", "request", "confirm", "reject", "save", "store",
    "generate", "trigger", "start", "stop", "reset", "clear", "merge", "link", "unlink",
})


def _name_tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def classify_tool(name: str, annotations: dict[str, Any] | None = None) -> tuple[bool, str]:
    """(허용 여부, 사유). 이름이 조회 동사로 시작하고 거부 토큰이 없을 때만 허용한다."""
    tokens = _name_tokens(name or "")
    if not tokens:
        return False, "empty_name"
    hit = next((t for t in tokens if t in _DENY_TOKENS), None)
    if hit:
        return False, f"deny_token:{hit}"
    if tokens[0] not in _READ_VERBS:
        return False, "not_read_verb"
    ann = annotations or {}
    if ann.get("readOnlyHint") is False:
        return False, "annotation_not_read_only"
    if ann.get("destructiveHint") is True:
        return False, "annotation_destructive"
    return True, "read_only"


async def _allowed_tool_names() -> set[str]:
    rows = await get_pool().fetch("SELECT name FROM clobe_mcp_tools WHERE allowed IS TRUE")
    return {r["name"] for r in rows}


async def assert_tool_callable(name: str) -> None:
    """이름 규칙 → 실측(tools/list) 기록 순서로 검사한다. 어느 쪽이든 실패하면 거부."""
    ok, reason = classify_tool(name)
    if not ok:
        raise ClobeToolDenied(f"tool_denied:{reason}")
    await ensure_schema()
    if name not in await _allowed_tool_names():
        raise ClobeToolDenied("tool_denied:not_observed_in_tools_list")


# ── 동적 클라이언트 등록 ─────────────────────────────────

async def get_or_register_client(redirect: str) -> str:
    await ensure_schema()
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT client_id FROM clobe_mcp_oauth_client WHERE redirect_uri = $1", redirect
    )
    if row:
        return row["client_id"]
    body = {
        "client_name": "AADS Obys (read-only)",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": CLOBE_SCOPE,
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(_require_clobe_url(CLOBE_REGISTER_URL), json=body)
    if resp.status_code not in (200, 201):
        logger.warning("clobe_register_failed error=%s", _safe_oauth_error(resp))
        raise ClobeError("clobe_register_failed")
    client_id = str(resp.json().get("client_id") or "")
    if not client_id:
        raise ClobeError("clobe_register_no_client_id")
    await pool.execute(
        "INSERT INTO clobe_mcp_oauth_client (redirect_uri, client_id) VALUES ($1, $2) "
        "ON CONFLICT (redirect_uri) DO NOTHING",
        redirect, client_id,
    )
    row = await pool.fetchrow(
        "SELECT client_id FROM clobe_mcp_oauth_client WHERE redirect_uri = $1", redirect
    )
    return row["client_id"]


# ── 연결 시작 / 콜백 ─────────────────────────────────────

async def start_authorization(created_by: str | None = None) -> dict[str, Any]:
    await ensure_schema()
    redirect = redirect_uri()
    client_id = await get_or_register_client(redirect)
    verifier = new_code_verifier()
    state = new_state()
    expires_at = datetime.now(timezone.utc) + STATE_TTL
    await get_pool().execute(
        "INSERT INTO clobe_mcp_oauth_state "
        "(state_hash, code_verifier_enc, redirect_uri, created_by, expires_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        hash_state(state), encrypt_value(verifier), redirect, created_by, expires_at,
    )
    url = build_authorize_url(
        client_id=client_id, redirect=redirect, state=state,
        challenge=code_challenge_s256(verifier),
    )
    return {"authorize_url": url, "expires_at": expires_at.isoformat()}


async def _consume_state(state: str) -> dict[str, Any] | None:
    """단회·만료 검증을 UPDATE 한 번으로 원자 처리한다. 위조·재사용·만료는 None."""
    row = await get_pool().fetchrow(
        "UPDATE clobe_mcp_oauth_state SET consumed_at = now() "
        "WHERE state_hash = $1 AND consumed_at IS NULL AND expires_at > now() "
        "RETURNING code_verifier_enc, redirect_uri",
        hash_state(state),
    )
    return dict(row) if row else None


def _token_expiry(payload: dict[str, Any]) -> datetime | None:
    try:
        seconds = int(payload.get("expires_in"))
    except (TypeError, ValueError):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def _store_tokens(payload: dict[str, Any], *, client_id: str, previous_refresh_enc: str | None = None) -> None:
    access = str(payload.get("access_token") or "")
    if not access:
        raise ClobeError("token_response_without_access_token")
    refresh = str(payload.get("refresh_token") or "")
    refresh_enc = encrypt_value(refresh) if refresh else previous_refresh_enc
    now = datetime.now(timezone.utc)
    await get_pool().execute(
        "INSERT INTO clobe_mcp_connection "
        "(id, status, client_id, access_token_enc, refresh_token_enc, token_expires_at, "
        " scope, connected_at, last_success_at, last_error, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, NULL, $8) "
        "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, client_id = EXCLUDED.client_id, "
        " access_token_enc = EXCLUDED.access_token_enc, refresh_token_enc = EXCLUDED.refresh_token_enc, "
        " token_expires_at = EXCLUDED.token_expires_at, scope = EXCLUDED.scope, "
        " connected_at = COALESCE(clobe_mcp_connection.connected_at, EXCLUDED.connected_at), "
        " last_success_at = EXCLUDED.last_success_at, last_error = NULL, updated_at = EXCLUDED.updated_at",
        CONNECTION_ID, STATUS_CONNECTED, client_id, encrypt_value(access), refresh_enc,
        _token_expiry(payload), str(payload.get("scope") or CLOBE_SCOPE), now,
    )


async def handle_callback(*, code: str | None, state: str | None, error: str | None = None) -> dict[str, Any]:
    """콜백 처리. 반환값에는 토큰이 없다. 실패는 ClobeError(분류 코드)."""
    await ensure_schema()
    if not state:
        raise ClobeError("state_missing")
    saved = await _consume_state(state)  # 오류 응답이어도 state 는 소진한다
    if saved is None:
        raise ClobeError("state_invalid_or_expired")
    if error:
        raise ClobeError(f"authorization_denied:{re.sub(r'[^a-z_]', '', error.lower())[:40]}")
    if not code:
        raise ClobeError("code_missing")
    client_id = await get_or_register_client(saved["redirect_uri"])
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": saved["redirect_uri"],
        "client_id": client_id,
        "code_verifier": decrypt_value(saved["code_verifier_enc"]),
        "resource": CLOBE_MCP_URL,
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(_require_clobe_url(CLOBE_TOKEN_URL), data=form)
    if resp.status_code != 200:
        err = _safe_oauth_error(resp)
        logger.warning("clobe_code_exchange_failed error=%s", err)
        raise ClobeError(f"code_exchange_failed:{err}")
    await _store_tokens(resp.json(), client_id=client_id)
    logger.info("clobe_connected")
    return {"status": STATUS_CONNECTED}


# ── 토큰 갱신 / 철회 ─────────────────────────────────────

async def _mark(status: str, error: str | None) -> None:
    await get_pool().execute(
        "UPDATE clobe_mcp_connection SET status = $2, last_error = $3, updated_at = now() WHERE id = $1",
        CONNECTION_ID, status, error,
    )


async def _load_connection() -> dict[str, Any] | None:
    await ensure_schema()
    row = await get_pool().fetchrow("SELECT * FROM clobe_mcp_connection WHERE id = $1", CONNECTION_ID)
    return dict(row) if row else None


def _needs_refresh(conn: dict[str, Any]) -> bool:
    exp = conn.get("token_expires_at")
    return bool(exp) and exp <= datetime.now(timezone.utc) + _REFRESH_SKEW


async def refresh_access_token(*, force: bool = False) -> None:
    """프로세스 간 중복 갱신(refresh 토큰 회전 경합)은 advisory lock 으로 직렬화한다."""
    pool = get_pool()
    async with pool.acquire() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock($1)", _REFRESH_LOCK_KEY)
        try:
            conn = await _load_connection()
            if not conn or conn["status"] != STATUS_CONNECTED:
                raise ClobeReauthRequired("not_connected")
            if not force and not _needs_refresh(conn):
                return  # 락을 기다리는 사이 다른 쪽이 이미 갱신했다
            if not conn.get("refresh_token_enc"):
                await _mark(STATUS_REAUTH, "no_refresh_token")
                raise ClobeReauthRequired("no_refresh_token")
            form = {
                "grant_type": "refresh_token",
                "refresh_token": decrypt_value(conn["refresh_token_enc"]),
                "client_id": conn["client_id"],
                "resource": CLOBE_MCP_URL,
            }
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(_require_clobe_url(CLOBE_TOKEN_URL), data=form)
            except httpx.HTTPError as exc:
                # 일시 장애: 상태는 유지하고 재시도 여지를 남긴다.
                logger.warning("clobe_refresh_network_error type=%s", type(exc).__name__)
                raise ClobeTransientError("refresh_network_error") from None
            if resp.status_code >= 500:
                raise ClobeTransientError(f"refresh_server_error:{resp.status_code}")
            if resp.status_code != 200:
                err = _safe_oauth_error(resp)
                logger.warning("clobe_refresh_failed error=%s", err)
                await _mark(STATUS_REAUTH, f"refresh_failed:{err}")
                raise ClobeReauthRequired(f"refresh_failed:{err}")
            await _store_tokens(
                resp.json(), client_id=conn["client_id"],
                previous_refresh_enc=conn["refresh_token_enc"],
            )
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", _REFRESH_LOCK_KEY)


async def _valid_access_token() -> str:
    conn = await _load_connection()
    if not conn or conn["status"] != STATUS_CONNECTED or not conn.get("access_token_enc"):
        raise ClobeReauthRequired("not_connected")
    if _needs_refresh(conn):
        await refresh_access_token()
        conn = await _load_connection()
        if not conn or not conn.get("access_token_enc"):
            raise ClobeReauthRequired("not_connected")
    return decrypt_value(conn["access_token_enc"])


async def revoke_connection() -> dict[str, Any]:
    """동의 철회. 원격 철회가 실패해도 로컬 토큰은 반드시 지운다."""
    conn = await _load_connection()
    remote_ok = False
    if conn and conn.get("client_id"):
        for enc, hint in ((conn.get("refresh_token_enc"), "refresh_token"),
                          (conn.get("access_token_enc"), "access_token")):
            if not enc:
                continue
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.post(
                        _require_clobe_url(CLOBE_REVOKE_URL),
                        data={"token": decrypt_value(enc), "token_type_hint": hint,
                              "client_id": conn["client_id"]},
                    )
                remote_ok = resp.status_code in (200, 204)
            except (httpx.HTTPError, ClobeError) as exc:
                logger.warning("clobe_revoke_remote_error type=%s", type(exc).__name__)
                remote_ok = False
            if remote_ok:
                break
    await get_pool().execute(
        "UPDATE clobe_mcp_connection SET status = $2, access_token_enc = NULL, "
        "refresh_token_enc = NULL, token_expires_at = NULL, updated_at = now() WHERE id = $1",
        CONNECTION_ID, STATUS_REVOKED,
    )
    logger.info("clobe_revoked remote_revoked=%s", remote_ok)
    return {"status": STATUS_REVOKED, "remote_revoked": remote_ok}


# ── MCP streamable HTTP ──────────────────────────────────

def parse_mcp_response(resp: httpx.Response, request_id: int | None) -> dict[str, Any] | None:
    """application/json 또는 text/event-stream 본문에서 JSON-RPC 응답 하나를 꺼낸다."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == request_id:
                return msg
        return None
    if not resp.content:
        return None
    msg = resp.json()
    return msg if isinstance(msg, dict) else None


class _McpSession:
    def __init__(self, http: httpx.AsyncClient, token: str):
        self._http = http
        self._token = token
        self._session_id: str | None = None
        self._next_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        return await self._http.post(_require_clobe_url(CLOBE_MCP_URL), json=payload, headers=self._headers())

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        try:
            resp = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except httpx.HTTPError as exc:
            logger.warning("clobe_mcp_network_error type=%s", type(exc).__name__)
            raise ClobeTransientError("mcp_network_error") from None
        if resp.status_code == 401:
            raise ClobeReauthRequired(UNAUTHORIZED)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ClobeTransientError(f"mcp_http_{resp.status_code}")
        if resp.status_code != 200:
            raise ClobeError(f"mcp_http_{resp.status_code}")
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        msg = parse_mcp_response(resp, rid)
        if msg is None:
            raise ClobeError("mcp_no_response")
        if "error" in msg:
            code = msg["error"].get("code") if isinstance(msg["error"], dict) else None
            raise ClobeError(f"mcp_error:{code}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    async def initialize(self) -> dict[str, Any]:
        result = await self.request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aads-obys-readonly", "version": "1"},
        })
        try:
            ack = await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except httpx.HTTPError as exc:
            logger.warning("clobe_mcp_network_error type=%s", type(exc).__name__)
            raise ClobeTransientError("mcp_network_error") from None
        if ack.status_code == 401:
            raise ClobeReauthRequired(UNAUTHORIZED)
        return result


async def _with_session(fn):
    """access token 으로 세션을 열어 fn 을 실행한다. 401 이면 한 번 갱신 후 재시도."""
    for attempt in (1, 2):
        token = await _valid_access_token()
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            session = _McpSession(http, token)
            try:
                await session.initialize()
                result = await fn(session)
            except ClobeReauthRequired as exc:
                if str(exc) != UNAUTHORIZED:
                    raise
                if attempt == 2:
                    await _mark(STATUS_REAUTH, "mcp_unauthorized_after_refresh")
                    raise
                await refresh_access_token(force=True)
                continue
        await get_pool().execute(
            "UPDATE clobe_mcp_connection SET last_success_at = now(), last_error = NULL WHERE id = $1",
            CONNECTION_ID,
        )
        return result
    raise ClobeReauthRequired("mcp_unauthorized")  # pragma: no cover


async def _record_tools(tools: list[dict[str, Any]]) -> dict[str, int]:
    pool = get_pool()
    allowed = 0
    for tool in tools:
        name = str(tool.get("name") or "")
        if not name:
            continue
        ok, reason = classify_tool(name, tool.get("annotations") if isinstance(tool.get("annotations"), dict) else None)
        allowed += 1 if ok else 0
        await pool.execute(
            "INSERT INTO clobe_mcp_tools (name, description, input_schema, annotations, allowed, deny_reason, observed_at) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, now()) "
            "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, "
            " input_schema = EXCLUDED.input_schema, annotations = EXCLUDED.annotations, "
            " allowed = EXCLUDED.allowed, deny_reason = EXCLUDED.deny_reason, observed_at = now()",
            name, tool.get("description"), json.dumps(tool.get("inputSchema") or {}),
            json.dumps(tool.get("annotations") or {}), ok, None if ok else reason,
        )
    # 이번 tools/list 에서 사라진 도구는 허용에서 뺀다(미확인 도구 기본 거부).
    names = [str(t.get("name")) for t in tools if t.get("name")]
    await pool.execute(
        "UPDATE clobe_mcp_tools SET allowed = FALSE, deny_reason = 'absent_from_tools_list' "
        "WHERE NOT (name = ANY($1::text[]))", names,
    )
    return {"total": len(names), "allowed": allowed, "denied": len(names) - allowed}


async def list_tools() -> dict[str, Any]:
    """initialize → tools/list(페이지네이션 포함). 결과를 DB 에 기록한다."""
    async def _run(session: _McpSession) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            result = await session.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in result.get("tools", []) if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    tools = await _with_session(_run)
    summary = await _record_tools(tools)
    return {**summary, "tools": [t.get("name") for t in tools]}


async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """허용목록을 통과한 조회형 도구만 호출한다. 거부는 네트워크 호출 전에 일어난다."""
    await assert_tool_callable(name)

    async def _run(session: _McpSession) -> dict[str, Any]:
        return await session.request("tools/call", {"name": name, "arguments": arguments or {}})

    return await _with_session(_run)


# ── 상태 / 검증 ──────────────────────────────────────────

async def get_status() -> dict[str, Any]:
    conn = await _load_connection()
    pool = get_pool()
    allowed = await pool.fetchval("SELECT count(*) FROM clobe_mcp_tools WHERE allowed IS TRUE")
    observed = await pool.fetchval("SELECT count(*) FROM clobe_mcp_tools")
    if not conn:
        status = STATUS_NOT_CONNECTED
    else:
        status = conn["status"]
    last = conn.get("last_success_at") if conn else None
    expires = conn.get("token_expires_at") if conn else None
    return {
        "status": status,
        "last_success_at": last.isoformat() if last else None,
        "token_expires_at": expires.isoformat() if expires else None,
        "reauth_required": status in (STATUS_REAUTH, STATUS_REVOKED, STATUS_NOT_CONNECTED),
        "allowed_tool_count": int(allowed or 0),
        "observed_tool_count": int(observed or 0),
        "last_error": conn.get("last_error") if conn else None,
    }


_COMPANY_TOOL_RE = re.compile(r"(company|companies|corp|business|organization|workspace)", re.I)


async def verify_connection() -> dict[str, Any]:
    """tools/list → 회사 목록류 조회 도구 1회 호출. 읽기만. 응답 내용은 반환하지 않는다."""
    listing = await list_tools()
    rows = await get_pool().fetch(
        "SELECT name, input_schema FROM clobe_mcp_tools WHERE allowed IS TRUE ORDER BY name"
    )
    candidate = None
    for row in rows:
        schema = row["input_schema"]
        if isinstance(schema, str):
            schema = json.loads(schema)
        if _COMPANY_TOOL_RE.search(row["name"]) and not (schema or {}).get("required"):
            candidate = row["name"]
            break
    result: dict[str, Any] = {"tools_total": listing["total"], "tools_allowed": listing["allowed"],
                              "company_tool": candidate, "company_call_ok": None}
    if candidate:
        out = await call_tool(candidate, {})
        result["company_call_ok"] = not out.get("isError", False)
        content = out.get("content")
        result["company_response_items"] = len(content) if isinstance(content, list) else None
    return result


# ── 수집 전용 읽기 계약 ───────────────────────────────────
# 위의 일반 허용목록(classify_tool)보다 한 겹 더 좁다. 클로브 tools/list 에는 직원·급여·
# 보안계정·전자신고처럼 수집과 무관한 조회 도구도 있고, 그것들은 수집 경로에서 호출하지 않는다.
# 값은 데이터 종류. 이름·입력 스키마는 2026-10-03 실제 tools/list(DB clobe_mcp_tools)에서 확인했다.
COLLECTION_TOOLS: dict[str, str] = {
    "get_my_context": "context",
    "get_scraping_status": "scraping_status",
    "get_bank_accounts": "bank_account",
    "get_labeled_transactions": "bank_transaction",
    "get_tax_invoices": "tax_invoice",
    "get_cash_receipts": "cash_receipt",
    "get_card_approvals": "card_approval",
}
COLLECTION_DATA_TOOLS: dict[str, str] = {
    "bank_transaction": "get_labeled_transactions",
    "tax_invoice": "get_tax_invoices",
    "cash_receipt": "get_cash_receipts",
    "card_approval": "get_card_approvals",
}


def parse_tool_json(result: dict[str, Any]) -> dict[str, Any]:
    """tools/call 결과에서 JSON 객체 하나를 꺼낸다. 오류·비JSON 은 분류 코드만 담아 거부한다."""
    if result.get("isError"):
        raise ClobeError("tool_error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        try:
            data = json.loads(content[0].get("text") or "")
        except (TypeError, json.JSONDecodeError):
            raise ClobeError("tool_non_json") from None
        if isinstance(data, dict):
            return data
    raise ClobeError("tool_unexpected_shape")


async def assert_collection_tool(name: str) -> None:
    """수집 허용목록 → 이름 규칙 → tools/list 실측 기록. 하나라도 실패하면 네트워크 호출 전에 거부."""
    if name not in COLLECTION_TOOLS:
        raise ClobeToolDenied("tool_denied:not_in_collection_allowlist")
    await assert_tool_callable(name)


class ReadSession:
    """수집용 읽기 전용 MCP 세션. 한 번 열어 여러 페이지를 읽는다.

    401 은 토큰을 한 번만 강제 갱신해 이어가고(세션 열기 중의 401 포함), 일시 장애(네트워크·429·5xx)는
    TRANSIENT_RETRIES 번까지 세션을 새로 열어 재시도한다. last_success_at 은 호출마다가 아니라 세션을 닫을 때 한 번 기록한다.
    """

    TRANSIENT_RETRIES = 2
    TRANSIENT_BACKOFF_SECONDS = 0.5

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._session: _McpSession | None = None
        self._succeeded = False

    async def __aenter__(self) -> "ReadSession":
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        return self

    async def __aexit__(self, exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._session = None
        succeeded, self._succeeded = self._succeeded, False
        # 예외로 끝난 세션은 일부 호출이 성공했어도 성공으로 기록하지 않는다(last_error 도 지우지 않는다).
        if succeeded and exc_type is None:
            try:
                await get_pool().execute(
                    "UPDATE clobe_mcp_connection SET last_success_at = now(), last_error = NULL WHERE id = $1",
                    CONNECTION_ID,
                )
            except Exception as exc:  # noqa: BLE001 - 상태 기록 실패가 이미 읽은 결과를 덮으면 안 된다
                logger.warning("clobe_last_success_update_failed type=%s", type(exc).__name__)

    async def _open(self) -> None:
        token = await _valid_access_token()
        assert self._http is not None
        session = _McpSession(self._http, token)
        await session.initialize()
        self._session = session

    async def call(self, name: str, tool_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """허용된 조회 도구를 호출해 JSON 본문을 돌려준다. 인자는 항상 {"input": {...}} 로 감싼다."""
        await assert_collection_tool(name)
        arguments = {"input": dict(tool_input or {})}
        force_refresh = refreshed_once = False
        transient_left = self.TRANSIENT_RETRIES
        while True:
            try:
                if self._session is None:
                    if force_refresh:
                        await refresh_access_token(force=True)
                        force_refresh = False
                    await self._open()
                result = await self._session.request("tools/call", {"name": name, "arguments": arguments})
                break
            except ClobeReauthRequired as exc:
                self._session = None
                if str(exc) != UNAUTHORIZED:
                    raise
                if refreshed_once:
                    await _mark(STATUS_REAUTH, "mcp_unauthorized_after_refresh")
                    raise
                refreshed_once = force_refresh = True
            except ClobeTransientError:
                self._session = None
                if transient_left <= 0:
                    raise
                transient_left -= 1
                await asyncio.sleep(self.TRANSIENT_BACKOFF_SECONDS)
        self._succeeded = True
        return parse_tool_json(result)


def normalize_reg_no(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def parse_companies(context: dict[str, Any]) -> list[dict[str, Any]]:
    """get_my_context 응답에서 회사 목록을 정규화한다. companyId 가 없는 항목은 버린다."""
    companies = context.get("companies")
    if not isinstance(companies, list):
        raise ClobeError("context_unexpected_shape")
    out: list[dict[str, Any]] = []
    for item in companies:
        if not isinstance(item, dict) or not item.get("companyId"):
            continue
        out.append({
            "company_id": str(item["companyId"]),
            "name": str(item.get("companyName") or "").strip(),
            "reg_no": normalize_reg_no(item.get("businessRegNo")),
            "role": str(item.get("role") or ""),
        })
    return out
