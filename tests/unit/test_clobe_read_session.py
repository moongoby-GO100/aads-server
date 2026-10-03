"""ReadSession: 401 갱신(세션 열기 중 포함), 일시 장애 재시도, last_success_at 기록 횟수."""
from __future__ import annotations

import httpx
import pytest

from app.services import clobe_mcp_client as clobe


class _Pool:
    def __init__(self):
        self.updates = 0

    async def execute(self, sql, *a):
        assert "last_success_at" in sql
        self.updates += 1


class _Http:
    async def aclose(self):
        return None


@pytest.fixture
def env(monkeypatch):
    pool = _Pool()
    state = {"refreshes": 0, "marks": [], "opens": 0, "script": []}

    async def noop_assert(name):
        return None

    async def valid_token():
        return "tok"

    async def refresh(*, force=False):
        state["refreshes"] += 1

    async def mark(status, error):
        state["marks"].append((status, error))

    class Session:
        def __init__(self, http, token):
            pass

        async def initialize(self):
            state["opens"] += 1
            step = state["script"].pop(0)
            if isinstance(step, Exception):
                raise step
            return {}

        async def request(self, method, params=None):
            step = state["script"].pop(0)
            if isinstance(step, Exception):
                raise step
            return step

    monkeypatch.setattr(clobe, "assert_collection_tool", noop_assert)
    monkeypatch.setattr(clobe, "_valid_access_token", valid_token)
    monkeypatch.setattr(clobe, "refresh_access_token", refresh)
    monkeypatch.setattr(clobe, "_mark", mark)
    monkeypatch.setattr(clobe, "_McpSession", Session)
    monkeypatch.setattr(clobe, "get_pool", lambda: pool)
    monkeypatch.setattr(clobe.httpx, "AsyncClient", lambda **kw: _Http())
    monkeypatch.setattr(clobe.ReadSession, "TRANSIENT_BACKOFF_SECONDS", 0)
    return pool, state


def _ok(payload):
    return {"structuredContent": payload}


def _unauth():
    return clobe.ClobeReauthRequired("mcp_unauthorized")


@pytest.mark.asyncio
async def test_many_calls_write_last_success_once(env):
    pool, state = env
    state["script"] = [{}] + [_ok({"n": i}) for i in range(50)]
    async with clobe.ReadSession() as s:
        for _ in range(50):
            await s.call("get_my_context")
    assert pool.updates == 1


@pytest.mark.asyncio
async def test_no_success_means_no_last_success_write(env):
    pool, state = env
    state["script"] = [{}, clobe.ClobeError("tool_error")]
    with pytest.raises(clobe.ClobeError):
        async with clobe.ReadSession() as s:
            await s.call("get_my_context")
    assert pool.updates == 0


@pytest.mark.asyncio
async def test_401_on_request_refreshes_once_then_succeeds(env):
    pool, state = env
    state["script"] = [{}, _unauth(), {}, _ok({"ok": 1})]
    async with clobe.ReadSession() as s:
        assert await s.call("get_my_context") == {"ok": 1}
    assert state["refreshes"] == 1 and state["marks"] == []


@pytest.mark.asyncio
async def test_401_during_session_open_is_retried_with_refresh(env):
    pool, state = env
    state["script"] = [_unauth(), {}, _ok({"ok": 2})]
    async with clobe.ReadSession() as s:
        assert await s.call("get_my_context") == {"ok": 2}
    assert state["refreshes"] == 1 and state["opens"] == 2


@pytest.mark.asyncio
async def test_401_again_after_refresh_marks_reauth_and_stops(env):
    pool, state = env
    state["script"] = [_unauth(), {}, _unauth()]
    with pytest.raises(clobe.ClobeReauthRequired):
        async with clobe.ReadSession() as s:
            await s.call("get_my_context")
    assert state["refreshes"] == 1
    assert state["marks"] == [(clobe.STATUS_REAUTH, "mcp_unauthorized_after_refresh")]


@pytest.mark.asyncio
async def test_non_401_reauth_is_not_retried(env):
    pool, state = env
    state["script"] = [clobe.ClobeReauthRequired("refresh_failed:invalid_grant")]
    with pytest.raises(clobe.ClobeReauthRequired):
        async with clobe.ReadSession() as s:
            await s.call("get_my_context")
    assert state["refreshes"] == 0


@pytest.mark.asyncio
async def test_transient_error_reopens_session_and_retries(env):
    pool, state = env
    state["script"] = [{}, clobe.ClobeTransientError("mcp_http_503"), {}, _ok({"ok": 3})]
    async with clobe.ReadSession() as s:
        assert await s.call("get_my_context") == {"ok": 3}
    assert state["refreshes"] == 0 and state["opens"] == 2


@pytest.mark.asyncio
async def test_transient_error_in_open_is_retried_and_bounded(env):
    pool, state = env
    boom = clobe.ClobeTransientError("mcp_network_error")
    state["script"] = [boom, boom, boom]
    with pytest.raises(clobe.ClobeTransientError):
        async with clobe.ReadSession() as s:
            await s.call("get_my_context")
    assert state["opens"] == 3 and state["script"] == []


@pytest.mark.asyncio
async def test_transient_after_401_does_not_refresh_twice(env):
    pool, state = env
    state["script"] = [_unauth(), {}, clobe.ClobeTransientError("mcp_http_503"), {}, _ok({"ok": 4})]
    async with clobe.ReadSession() as s:
        assert await s.call("get_my_context") == {"ok": 4}
    assert state["refreshes"] == 1


@pytest.mark.asyncio
async def test_session_that_fails_after_partial_success_does_not_record_success(env):
    pool, state = env
    state["script"] = [{}, _ok({"n": 1}), clobe.ClobeError("tool_error")]
    with pytest.raises(clobe.ClobeError):
        async with clobe.ReadSession() as s:
            await s.call("get_my_context")
            await s.call("get_my_context")
    assert pool.updates == 0


@pytest.mark.asyncio
async def test_session_that_catches_the_failure_and_ends_cleanly_records_success(env):
    pool, state = env
    state["script"] = [{}, _ok({"n": 1}), clobe.ClobeError("tool_error")]
    async with clobe.ReadSession() as s:
        await s.call("get_my_context")
        with pytest.raises(clobe.ClobeError):
            await s.call("get_my_context")
    assert pool.updates == 1


class _ScriptedHttp:
    def __init__(self, *steps):
        self.steps = list(steps)

    async def post(self, url, json=None, headers=None):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _init_ok():
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})


@pytest.fixture
def real_session(monkeypatch):
    monkeypatch.setattr(clobe, "_require_clobe_url", lambda url: url)


@pytest.mark.asyncio
async def test_real_initialize_401_raises_the_message_the_retry_path_matches_on(real_session):
    session = clobe._McpSession(_ScriptedHttp(httpx.Response(401)), "tok")
    with pytest.raises(clobe.ClobeReauthRequired) as exc:
        await session.initialize()
    assert str(exc.value) == clobe.UNAUTHORIZED


@pytest.mark.asyncio
async def test_real_initialized_notification_401_is_also_a_refreshable_401(real_session):
    session = clobe._McpSession(_ScriptedHttp(_init_ok(), httpx.Response(401)), "tok")
    with pytest.raises(clobe.ClobeReauthRequired) as exc:
        await session.initialize()
    assert str(exc.value) == clobe.UNAUTHORIZED


@pytest.mark.asyncio
async def test_real_initialized_notification_network_error_is_transient(real_session):
    session = clobe._McpSession(_ScriptedHttp(_init_ok(), httpx.ConnectError("boom")), "tok")
    with pytest.raises(clobe.ClobeTransientError):
        await session.initialize()
