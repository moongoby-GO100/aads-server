"""클로브 MCP OAuth 연결: PKCE/state, 읽기 전용 강제, 토큰 비노출, refresh 실패 처리."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from app.services import clobe_mcp_client as clobe

REPO = Path(__file__).resolve().parents[2]
_REAL_ASYNC_CLIENT = httpx.AsyncClient

SECRET_ACCESS = "AT-secret-access-0123456789"
SECRET_REFRESH = "RT-secret-refresh-0123456789"
SECRET_CODE = "CODE-secret-abcdef"


class _FakeConn:
    async def execute(self, *_a, **_k):
        return None


class _Acquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *_a):
        return False


class FakePool:
    """clobe_mcp_client 가 쓰는 SQL 만 흉내 내는 메모리 풀."""

    def __init__(self):
        self.clients: dict[str, str] = {}
        self.states: dict[str, dict] = {}
        self.conn: dict | None = None
        self.tools: dict[str, dict] = {}

    def acquire(self):
        return _Acquire()

    async def fetchrow(self, sql, *a):
        if "FROM clobe_mcp_oauth_client" in sql:
            cid = self.clients.get(a[0])
            return {"client_id": cid} if cid else None
        if sql.lstrip().startswith("UPDATE clobe_mcp_oauth_state"):
            row = self.states.get(a[0])
            if not row or row["consumed_at"] or row["expires_at"] <= datetime.now(timezone.utc):
                return None
            row["consumed_at"] = datetime.now(timezone.utc)
            return {"code_verifier_enc": row["code_verifier_enc"], "redirect_uri": row["redirect_uri"]}
        if "FROM clobe_mcp_connection" in sql:
            return dict(self.conn) if self.conn else None
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql, *a):
        if "FROM clobe_mcp_tools WHERE allowed" in sql:
            return [{"name": n, "input_schema": t["input_schema"]} for n, t in self.tools.items() if t["allowed"]]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql, *a):
        if "allowed IS TRUE" in sql:
            return sum(1 for t in self.tools.values() if t["allowed"])
        return len(self.tools)

    async def execute(self, sql, *a):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO clobe_mcp_oauth_client"):
            self.clients.setdefault(a[0], a[1])
        elif s.startswith("INSERT INTO clobe_mcp_oauth_state"):
            self.states[a[0]] = {"code_verifier_enc": a[1], "redirect_uri": a[2], "created_by": a[3],
                                 "expires_at": a[4], "consumed_at": None}
        elif s.startswith("INSERT INTO clobe_mcp_connection"):
            self.conn = {"id": a[0], "status": a[1], "client_id": a[2], "access_token_enc": a[3],
                         "refresh_token_enc": a[4], "token_expires_at": a[5], "scope": a[6],
                         "connected_at": a[7], "last_success_at": a[7], "last_error": None}
        elif s.startswith("UPDATE clobe_mcp_connection SET status = $2, last_error"):
            self.conn.update(status=a[1], last_error=a[2])
        elif s.startswith("UPDATE clobe_mcp_connection SET status = $2, access_token_enc = NULL"):
            self.conn.update(status=a[1], access_token_enc=None, refresh_token_enc=None, token_expires_at=None)
        elif s.startswith("UPDATE clobe_mcp_connection SET last_success_at"):
            self.conn["last_success_at"] = datetime.now(timezone.utc)
        elif s.startswith("INSERT INTO clobe_mcp_tools"):
            self.tools[a[0]] = {"description": a[1], "input_schema": json.loads(a[2]),
                                "allowed": a[4], "deny_reason": a[5]}
        elif s.startswith("UPDATE clobe_mcp_tools SET allowed = FALSE"):
            for n, t in self.tools.items():
                if n not in a[0]:
                    t["allowed"] = False
        else:
            raise AssertionError(f"unexpected execute: {sql}")


class Server:
    """api.clobe.ai 흉내. 요청을 기록한다."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.token_json = {"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH,
                           "expires_in": 3600, "scope": "mcp offline_access"}
        self.tools = [
            {"name": "list_companies", "description": "회사 목록", "inputSchema": {"type": "object"}},
            {"name": "get_vat_summary", "description": "부가세 요약",
             "inputSchema": {"type": "object", "required": ["company_id"]}},
            {"name": "issue_tax_invoice", "description": "세금계산서 발급", "inputSchema": {"type": "object"}},
            {"name": "submit_vat_filing", "description": "신고", "inputSchema": {"type": "object"}},
            {"name": "delete_voucher", "description": "삭제", "inputSchema": {"type": "object"}},
        ]
        self.mcp_calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "client-123"})
        if path == "/oauth/token":
            return httpx.Response(self.token_status, json=self.token_json)
        if path == "/oauth/revoke":
            return httpx.Response(200)
        if path == "/mcp":
            body = json.loads(request.content)
            method = body.get("method")
            self.mcp_calls.append(method)
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                                      headers={"mcp-session-id": "sess-1"})
            if method == "tools/list":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self.tools}})
            if method == "tools/call":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                 "result": {"content": [{"type": "text", "text": "[]"}]}})
        return httpx.Response(404)


@pytest.fixture
def env(monkeypatch):
    pool = FakePool()
    server = Server()
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(clobe, "get_pool", lambda: pool)
    monkeypatch.setattr(clobe, "_schema_ready", True)
    monkeypatch.setattr(clobe, "encrypt_value", lambda v: fernet.encrypt(v.encode()).decode())
    monkeypatch.setattr(clobe, "decrypt_value", lambda v: fernet.decrypt(v.encode()).decode())
    transport = httpx.MockTransport(server.handler)
    monkeypatch.setattr(clobe.httpx, "AsyncClient", lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw))
    monkeypatch.delenv("CLOBE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("AADS_PUBLIC_BASE_URL", raising=False)
    return pool, server


def _state_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


async def _connect(pool):
    out = await clobe.start_authorization("admin-1")
    await clobe.handle_callback(code=SECRET_CODE, state=_state_from(out["authorize_url"]))


# ── PKCE / state ────────────────────────────────────────

def test_pkce_s256_matches_rfc7636_vector():
    assert clobe.code_challenge_s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_length_and_state_uniqueness():
    v = clobe.new_code_verifier()
    assert 43 <= len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", v)
    assert len({clobe.new_state() for _ in range(50)}) == 50


@pytest.mark.asyncio
async def test_authorize_url_has_pkce_and_no_secret(env):
    pool, _ = env
    out = await clobe.start_authorization("admin-1")
    q = parse_qs(urlparse(out["authorize_url"]).query)
    assert out["authorize_url"].startswith("https://api.clobe.ai/oauth/authorize?")
    assert q["code_challenge_method"] == ["S256"] and q["response_type"] == ["code"]
    assert q["scope"] == ["mcp offline_access"] and q["client_id"] == ["client-123"]
    assert q["redirect_uri"] == ["https://aads.newtalk.kr/api/v1/integrations/clobe/oauth/callback"]
    assert "code_verifier" not in q
    saved = next(iter(pool.states.values()))
    assert clobe.hash_state(q["state"][0]) in pool.states  # 원문 state 가 아니라 해시만 저장
    assert q["state"][0] not in pool.states
    assert saved["code_verifier_enc"] and "=" not in q["code_challenge"][0]


@pytest.mark.asyncio
async def test_forged_state_rejected_without_token_call(env):
    _, server = env
    with pytest.raises(clobe.ClobeError, match="state_invalid_or_expired"):
        await clobe.handle_callback(code="x", state="forged-state")
    with pytest.raises(clobe.ClobeError, match="state_missing"):
        await clobe.handle_callback(code="x", state=None)
    assert not any(r.url.path == "/oauth/token" for r in server.requests)


@pytest.mark.asyncio
async def test_state_is_single_use(env):
    pool, _ = env
    out = await clobe.start_authorization()
    state = _state_from(out["authorize_url"])
    await clobe.handle_callback(code=SECRET_CODE, state=state)
    with pytest.raises(clobe.ClobeError, match="state_invalid_or_expired"):
        await clobe.handle_callback(code=SECRET_CODE, state=state)


@pytest.mark.asyncio
async def test_expired_state_rejected(env):
    pool, _ = env
    out = await clobe.start_authorization()
    state = _state_from(out["authorize_url"])
    pool.states[clobe.hash_state(state)]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(clobe.ClobeError, match="state_invalid_or_expired"):
        await clobe.handle_callback(code=SECRET_CODE, state=state)


@pytest.mark.asyncio
async def test_state_ttl_is_ten_minutes(env):
    pool, _ = env
    await clobe.start_authorization()
    row = next(iter(pool.states.values()))
    delta = row["expires_at"] - datetime.now(timezone.utc)
    assert timedelta(minutes=9, seconds=50) < delta <= timedelta(minutes=10)


@pytest.mark.asyncio
async def test_authorization_denied_consumes_state(env):
    out = await clobe.start_authorization()
    state = _state_from(out["authorize_url"])
    with pytest.raises(clobe.ClobeError, match="authorization_denied:access_denied"):
        await clobe.handle_callback(code=None, state=state, error="access_denied")
    with pytest.raises(clobe.ClobeError, match="state_invalid_or_expired"):
        await clobe.handle_callback(code="x", state=state)


@pytest.mark.asyncio
async def test_dynamic_registration_happens_once(env):
    _, server = env
    await clobe.start_authorization()
    await clobe.start_authorization()
    assert sum(1 for r in server.requests if r.url.path == "/oauth/register") == 1


# ── 토큰 저장/마스킹 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_code_exchange_sends_verifier_and_stores_encrypted(env, caplog):
    pool, server = env
    caplog.set_level(logging.DEBUG)
    out = await clobe.start_authorization()
    state = _state_from(out["authorize_url"])
    challenge = parse_qs(urlparse(out["authorize_url"]).query)["code_challenge"][0]
    await clobe.handle_callback(code=SECRET_CODE, state=state)

    form = parse_qs(next(r for r in server.requests if r.url.path == "/oauth/token").content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == [SECRET_CODE]
    assert clobe.code_challenge_s256(form["code_verifier"][0]) == challenge

    assert pool.conn["status"] == "connected"
    for stored in (pool.conn["access_token_enc"], pool.conn["refresh_token_enc"]):
        assert stored and SECRET_ACCESS not in stored and SECRET_REFRESH not in stored
    assert clobe.decrypt_value(pool.conn["access_token_enc"]) == SECRET_ACCESS
    for secret in (SECRET_ACCESS, SECRET_REFRESH, SECRET_CODE, form["code_verifier"][0], state):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_failed_exchange_does_not_leak_into_logs_or_error(env, caplog):
    _, server = env
    caplog.set_level(logging.DEBUG)
    server.token_status = 400
    server.token_json = {"error": "invalid_grant", "error_description": f"bad {SECRET_CODE}"}
    out = await clobe.start_authorization()
    with pytest.raises(clobe.ClobeError) as exc:
        await clobe.handle_callback(code=SECRET_CODE, state=_state_from(out["authorize_url"]))
    assert str(exc.value) == "code_exchange_failed:invalid_grant"
    assert SECRET_CODE not in caplog.text


def test_mask_secret_hides_value():
    assert "secret" not in clobe.mask_secret("my-secret-value")
    assert clobe.mask_secret(None) == ""


# ── 읽기 전용 강제 ───────────────────────────────────────

@pytest.mark.parametrize("name", [
    "issue_tax_invoice", "create_voucher", "update_company", "delete_voucher", "send_email",
    "submit_vat_filing", "file_vat_return", "transfer_funds", "pay_invoice", "register_cert",
    "declare_vat", "sync_bank", "revoke_access", "get_and_delete_voucher", "createVoucher",
    "report_vat", "export_everything", "",
])
def test_write_issue_remit_filing_tools_denied(name):
    assert clobe.classify_tool(name)[0] is False


@pytest.mark.parametrize("name", ["list_companies", "get_vat_summary", "search_transactions", "listCompanies"])
def test_read_tools_allowed_by_name(name):
    assert clobe.classify_tool(name) == (True, "read_only")


def test_annotations_can_only_tighten():
    assert clobe.classify_tool("get_x", {"readOnlyHint": False})[0] is False
    assert clobe.classify_tool("get_x", {"destructiveHint": True})[0] is False
    assert clobe.classify_tool("create_x", {"readOnlyHint": True})[0] is False


@pytest.mark.asyncio
async def test_tools_list_recorded_and_only_read_tools_allowed(env):
    pool, server = env
    await _connect(pool)
    summary = await clobe.list_tools()
    assert summary["total"] == 5 and summary["allowed"] == 2
    assert {n for n, t in pool.tools.items() if t["allowed"]} == {"list_companies", "get_vat_summary"}
    assert pool.tools["issue_tax_invoice"]["deny_reason"] == "deny_token:issue"
    assert (await clobe.get_status())["allowed_tool_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["issue_tax_invoice", "submit_vat_filing", "delete_voucher", "create_anything"])
async def test_write_tool_call_rejected_before_network(env, name):
    pool, server = env
    await _connect(pool)
    await clobe.list_tools()
    server.mcp_calls.clear()
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_tool(name, {})
    assert server.mcp_calls == []  # 호출 자체가 나가지 않는다


@pytest.mark.asyncio
async def test_unobserved_read_looking_tool_denied_by_default(env):
    pool, server = env
    await _connect(pool)
    await clobe.list_tools()
    server.mcp_calls.clear()
    with pytest.raises(clobe.ClobeToolDenied, match="not_observed_in_tools_list"):
        await clobe.call_tool("get_brand_new_tool", {})
    assert server.mcp_calls == []


@pytest.mark.asyncio
async def test_tool_removed_from_listing_loses_allowance(env):
    pool, server = env
    await _connect(pool)
    await clobe.list_tools()
    server.tools = [t for t in server.tools if t["name"] != "list_companies"]
    await clobe.list_tools()
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.call_tool("list_companies", {})


@pytest.mark.asyncio
async def test_allowed_read_tool_call_goes_through(env):
    pool, server = env
    await _connect(pool)
    await clobe.list_tools()
    out = await clobe.call_tool("list_companies", {})
    assert out["content"][0]["type"] == "text"
    assert server.mcp_calls[-1] == "tools/call"
    sent = [r for r in server.requests if r.url.path == "/mcp"][-1]
    assert sent.headers["authorization"] == f"Bearer {SECRET_ACCESS}"
    assert sent.headers["mcp-session-id"] == "sess-1"


@pytest.mark.asyncio
async def test_verify_calls_company_tool_once_and_hides_content(env):
    pool, server = env
    await _connect(pool)
    result = await clobe.verify_connection()
    assert result["company_tool"] == "list_companies" and result["company_call_ok"] is True
    assert server.mcp_calls.count("tools/call") == 1
    assert "text" not in json.dumps(result)


# ── refresh / reauth / revoke ────────────────────────────

def _expire(pool):
    pool.conn["token_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_refresh_success_rotates_and_keeps_old_refresh_if_absent(env):
    pool, server = env
    await _connect(pool)
    old_refresh_enc = pool.conn["refresh_token_enc"]
    _expire(pool)
    server.token_json = {"access_token": "AT-new-0000", "expires_in": 3600}
    await clobe.refresh_access_token()
    assert clobe.decrypt_value(pool.conn["access_token_enc"]) == "AT-new-0000"
    assert pool.conn["refresh_token_enc"] == old_refresh_enc
    assert pool.conn["status"] == "connected"
    form = parse_qs([r for r in server.requests if r.url.path == "/oauth/token"][-1].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == [SECRET_REFRESH]


@pytest.mark.asyncio
async def test_refresh_failure_marks_reauth_required(env, caplog):
    pool, server = env
    caplog.set_level(logging.DEBUG)
    await _connect(pool)
    await clobe.list_tools()
    _expire(pool)
    server.token_status = 400
    server.token_json = {"error": "invalid_grant", "error_description": SECRET_REFRESH}
    with pytest.raises(clobe.ClobeReauthRequired):
        await clobe.call_tool("list_companies", {})
    assert pool.conn["status"] == "reauth_required"
    assert pool.conn["last_error"] == "refresh_failed:invalid_grant"
    assert (await clobe.get_status())["status"] == "reauth_required"
    assert SECRET_REFRESH not in caplog.text


@pytest.mark.asyncio
async def test_refresh_server_error_is_transient_and_keeps_connected(env):
    pool, server = env
    await _connect(pool)
    _expire(pool)
    server.token_status = 503
    with pytest.raises(clobe.ClobeError) as exc:
        await clobe.refresh_access_token()
    assert not isinstance(exc.value, clobe.ClobeReauthRequired)
    assert pool.conn["status"] == "connected"


@pytest.mark.asyncio
async def test_no_call_when_not_connected(env):
    pool, server = env
    pool.tools["list_companies"] = {"input_schema": {}, "allowed": True}
    with pytest.raises(clobe.ClobeReauthRequired):
        await clobe.call_tool("list_companies", {})
    assert server.mcp_calls == []


@pytest.mark.asyncio
async def test_revoke_wipes_tokens_even_if_remote_fails(env, monkeypatch):
    pool, server = env
    await _connect(pool)
    server.handler_orig = server.handler

    def failing(request):
        if request.url.path == "/oauth/revoke":
            raise httpx.ConnectError("boom")
        return server.handler_orig(request)

    transport = httpx.MockTransport(failing)
    monkeypatch.setattr(clobe.httpx, "AsyncClient", lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw))
    out = await clobe.revoke_connection()
    assert out == {"status": "revoked", "remote_revoked": False}
    assert pool.conn["access_token_enc"] is None and pool.conn["refresh_token_enc"] is None


@pytest.mark.asyncio
async def test_revoke_remote_success(env):
    pool, server = env
    await _connect(pool)
    out = await clobe.revoke_connection()
    assert out["remote_revoked"] is True and pool.conn["status"] == "revoked"


# ── 응답 파싱 / URL 가드 / 배선 ───────────────────────────

def test_parse_sse_response_picks_matching_id():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":9,"result":{}}\n\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":1}}\n\n'
    resp = httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    assert clobe.parse_mcp_response(resp, 2)["result"] == {"ok": 1}
    assert clobe.parse_mcp_response(resp, 7) is None


def test_non_clobe_host_rejected():
    for url in ("http://api.clobe.ai/mcp", "https://evil.example/mcp", "https://api.clobe.ai.evil.com/x"):
        with pytest.raises(clobe.ClobeError):
            clobe._require_clobe_url(url)
    assert clobe._require_clobe_url("https://api.clobe.ai/mcp")


def test_admin_routes_require_internal_admin_and_only_callback_is_public():
    from app.api.clobe_integration import router
    from app.auth import require_internal_admin

    for route in router.routes:
        deps = {d.call for d in route.dependant.dependencies}
        if route.path.endswith("/oauth/callback"):
            assert require_internal_admin not in deps
        else:
            assert require_internal_admin in deps, route.path


def test_main_wires_router_and_exempts_only_exact_callback_path():
    src = (REPO / "app/main.py").read_text()
    assert "app.include_router(clobe_integration_router" in src
    exempt = re.search(r"_SERVICE_AUTH_EXACT_PATHS = \{(.*?)\n\}", src, re.S).group(1)
    assert '"/api/v1/integrations/clobe/oauth/callback"' in exempt
    assert "/api/v1/integrations/clobe\"" not in src.split("_AUTH_EXEMPT_PREFIXES = (")[1].split(")")[0]


def test_migration_matches_runtime_ddl():
    sql = (REPO / "migrations/20261003_clobe_mcp_oauth.sql").read_text()
    norm = lambda s: re.sub(r"\s+", " ", s)  # noqa: E731
    for stmt in [s.strip() for s in clobe.SCHEMA_DDL.split(";") if s.strip()]:
        assert norm(stmt) in norm(sql)
