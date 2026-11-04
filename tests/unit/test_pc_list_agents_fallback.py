from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
import pytest

from app.services import tool_executor
from app.services.tool_executor import ToolExecutor


class _FakeResponse:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://x/api/v1/pc-agent/agents")
            raise httpx.HTTPStatusError("err", request=request, response=httpx.Response(self.status_code, request=request))

    def json(self) -> Any:
        return self._payload


def _install_client(monkeypatch: pytest.MonkeyPatch, handler) -> list[str]:  # noqa: ANN001
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, auth: Any = None) -> _FakeResponse:
            calls.append(url)
            return handler(url, len(calls))

    monkeypatch.setattr(tool_executor.httpx, "AsyncClient", FakeClient)
    _patch_runtime(monkeypatch)
    return calls


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(tool_executor.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.list_agent_statuses", lambda: []
    )


def _connect_error(*_a: Any) -> _FakeResponse:
    raise httpx.ConnectError("connection refused")


@pytest.mark.asyncio
async def test_all_candidates_fail_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, _connect_error)

    result = await ToolExecutor()._pc_list_agents({})

    assert result["status"] == "unavailable"
    assert result["online_count"] is None
    assert result["backend_source"] == "unavailable"
    assert result["errors"]
    assert "0대 아님" in result["message"]


@pytest.mark.asyncio
async def test_transient_error_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = {"agent_id": "a1", "status": "online"}

    def handler(url: str, n: int) -> _FakeResponse:
        if n == 1:
            raise httpx.ReadTimeout("timeout")
        return _FakeResponse(200, {"agents": [agent]})

    calls = _install_client(monkeypatch, handler)

    result = await ToolExecutor()._pc_list_agents({})

    assert len(calls) == 2
    assert result["agents"] == [agent]
    assert result["online_count"] == 1
    assert result["backend_source"] == "api_fallback"


@pytest.mark.asyncio
async def test_5xx_retried_once_per_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_client(monkeypatch, lambda url, n: _FakeResponse(503))

    agents, errors, reached = await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    assert agents == [] and reached is False
    assert len(calls) == 2 * len(errors)
    assert all("HTTP 503" in e for e in errors)


@pytest.mark.asyncio
async def test_2xx_empty_agents_is_confirmed_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, lambda url, n: _FakeResponse(200, {"agents": []}))

    result = await ToolExecutor()._pc_list_agents({})

    assert result["online_count"] == 0
    assert result["count"] == 0
    assert result.get("status") != "unavailable"
    assert result["backend_source"] == "api_empty"


@pytest.mark.asyncio
async def test_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_client(monkeypatch, lambda url, n: _FakeResponse(404))

    agents, errors, reached = await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    assert reached is False and agents == []
    assert len(calls) == len(errors)
    assert len(set(calls)) == len(calls)


@pytest.mark.asyncio
async def test_normal_response_keeps_existing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    agents = [{"agent_id": "a1", "status": "online"}, {"agent_id": "a2", "status": "offline"}]
    _install_client(monkeypatch, lambda url, n: _FakeResponse(200, {"agents": agents}))

    result = await ToolExecutor()._pc_list_agents({})

    assert set(result) == {"agents", "count", "online_count", "backend_source"}
    assert result["count"] == 2
    assert result["online_count"] == 1
    assert result["backend_source"] == "api_fallback"


@pytest.mark.asyncio
async def test_legacy_wrapper_returns_list_only(monkeypatch: pytest.MonkeyPatch) -> None:
    agents = [{"agent_id": "a1", "status": "online"}]
    _install_client(monkeypatch, lambda url, n: _FakeResponse(200, {"agents": agents}))
    assert await tool_executor._fetch_pc_agent_statuses_from_api() == agents

    _install_client(monkeypatch, _connect_error)
    assert await tool_executor._fetch_pc_agent_statuses_from_api() == []


_SECRET_URL = "http://user:pass@secret-host.internal:9100/base?token=SECRET#frag"


def _assert_no_secret(result: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    exposed = repr(result) + caplog.text
    for needle in ("pass", "SECRET", "user:", "token=", _SECRET_URL, "frag"):
        assert needle not in exposed, needle


@pytest.mark.asyncio
async def test_configured_url_secrets_not_exposed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AADS_API_URL", _SECRET_URL)
    _install_client(monkeypatch, lambda url, n: _FakeResponse(503))

    with caplog.at_level(logging.WARNING):
        result = await ToolExecutor()._pc_list_agents({})

    assert result["status"] == "unavailable"
    assert any("secret-host.internal:9100" in e for e in result["errors"])
    _assert_no_secret(result, caplog)


@pytest.mark.asyncio
async def test_exception_message_secrets_not_exposed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AADS_API_URL", _SECRET_URL)

    def handler(url: str, n: int) -> _FakeResponse:
        raise httpx.ConnectError("failed http://user:pass@secret-host.internal?token=SECRET")

    _install_client(monkeypatch, handler)

    with caplog.at_level(logging.WARNING):
        result = await ToolExecutor()._pc_list_agents({})

    assert result["status"] == "unavailable"
    assert all(e.endswith(": connect_error") for e in result["errors"])
    _assert_no_secret(result, caplog)


def test_unparseable_url_uses_candidate_label() -> None:
    assert tool_executor._pc_api_candidate_label("user:pass@host:abc/x?token=SECRET", 3) == "candidate-3"
    assert tool_executor._pc_api_candidate_label("http://h:99999999/x", 2) == "candidate-2"


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ReadTimeout("SECRET"), "timeout"),
        (httpx.ConnectError("SECRET"), "connect_error"),
        (ValueError("SECRET"), "invalid_json"),
        (httpx.ReadError("SECRET"), "transport_error"),
    ],
)
def test_error_reason_is_fixed_phrase(exc: BaseException, reason: str) -> None:
    assert tool_executor._pc_api_error_reason(exc) == reason


_BAD_AGENTS = {"agents": [None, "bad"]}


@pytest.mark.asyncio
async def test_invalid_elements_do_not_count_as_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_client(monkeypatch, lambda url, n: _FakeResponse(200, _BAD_AGENTS))

    agents, errors, reached = await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    assert agents == [] and reached is False
    assert len(calls) == len(errors) > 1
    assert all(e.endswith(": invalid payload") for e in errors)


@pytest.mark.asyncio
async def test_all_candidates_invalid_elements_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, lambda url, n: _FakeResponse(200, _BAD_AGENTS))

    result = await ToolExecutor()._pc_list_agents({})

    assert result["status"] == "unavailable"
    assert result["online_count"] is None


@pytest.mark.asyncio
async def test_valid_candidate_after_invalid_one_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = {"agent_id": "a1", "status": "online"}

    def handler(url: str, n: int) -> _FakeResponse:
        return _FakeResponse(200, _BAD_AGENTS if n == 1 else {"agents": [agent]})

    calls = _install_client(monkeypatch, handler)

    result = await ToolExecutor()._pc_list_agents({})

    assert len(calls) == 2
    assert result["agents"] == [agent]
    assert result["online_count"] == 1


@pytest.mark.asyncio
async def test_partially_valid_list_returns_only_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = {"agent_id": "a1", "status": "online"}
    _install_client(monkeypatch, lambda url, n: _FakeResponse(200, {"agents": [None, agent, "bad"]}))

    agents, errors, reached = await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    assert agents == [agent] and reached is True and errors == []


_RAW_SECRET_URL = "http://user:PWSECRET@127.0.0.1:8100?token=QTOK#FRAG"
_SECRET_NEEDLES = ("PWSECRET", "QTOK", "FRAG", _RAW_SECRET_URL)


def _install_real_client(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:  # noqa: ANN001
    """실제 httpx.AsyncClient 에 MockTransport 만 주입해 httpx 의 실제 INFO 로그를 발생시킨다."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(tool_executor.httpx, "AsyncClient", factory)
    _patch_runtime(monkeypatch)
    return requests


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def _ok_agents(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"agents": [{"agent_id": "a1", "status": "online"}]}, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "expect_log"),
    [
        (_ok_agents, True),
        (lambda request: httpx.Response(503, request=request), True),
        (_refuse, False),
    ],
    ids=["2xx", "5xx", "connect_failure"],
)
async def test_httpx_info_log_does_not_expose_url_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, handler, expect_log: bool  # noqa: ANN001
) -> None:
    monkeypatch.setenv("AADS_API_URL", _RAW_SECRET_URL)
    requests = _install_real_client(monkeypatch, handler)

    with caplog.at_level(logging.INFO):
        result = await ToolExecutor()._pc_list_agents({})

    assert requests
    assert bool([r for r in caplog.records if r.name == "httpx"]) is expect_log
    exposed = repr(result) + caplog.text
    for needle in _SECRET_NEEDLES:
        assert needle not in exposed, "secret leaked"
    assert all(b"PWSECRET" not in str(r.url).encode() for r in requests)


@pytest.mark.asyncio
async def test_userinfo_is_sent_as_basic_auth_not_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AADS_API_URL", _RAW_SECRET_URL)
    requests = _install_real_client(monkeypatch, _ok_agents)

    await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    expected = "Basic " + base64.b64encode(b"user:PWSECRET").decode()
    first = requests[0]
    assert first.headers.get("authorization") == expected, "basic auth header mismatch"
    assert (first.url.userinfo, first.url.query, first.url.fragment) == (b"", b"", ""), "url not clean"
    assert str(first.url) == "http://127.0.0.1:8100/api/v1/pc-agent/agents"


@pytest.mark.asyncio
async def test_plain_url_keeps_candidates_and_sends_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AADS_API_URL", "http://aads.internal:8080/")
    monkeypatch.setattr(tool_executor, "_AADS_API_BASE", "http://localhost:8080")
    requests = _install_real_client(monkeypatch, lambda request: httpx.Response(404, request=request))

    agents, errors, reached = await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    assert [str(r.url) for r in requests] == [
        "http://aads.internal:8080/api/v1/pc-agent/agents",
        "http://localhost:8080/api/v1/pc-agent/agents",
        "http://127.0.0.1:8100/api/v1/pc-agent/agents",
        "http://127.0.0.1:8102/api/v1/pc-agent/agents",
    ]
    assert all("authorization" not in r.headers for r in requests)
    assert agents == [] and reached is False and len(errors) == 4


@pytest.mark.asyncio
async def test_api_base_env_secrets_are_stripped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("AADS_API_URL", raising=False)
    monkeypatch.setattr(tool_executor, "_AADS_API_BASE", "http://u2:PW2SECRET@localhost:8080?k=Q2TOK#F2")
    requests = _install_real_client(monkeypatch, lambda request: httpx.Response(404, request=request))

    with caplog.at_level(logging.INFO):
        _agents, errors, _reached = await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    assert str(requests[0].url) == "http://localhost:8080/api/v1/pc-agent/agents"
    exposed = repr(errors) + caplog.text
    for needle in ("PW2SECRET", "Q2TOK", "F2"):
        assert needle not in exposed, "secret leaked"


@pytest.mark.asyncio
async def test_unparseable_candidate_is_skipped_with_fixed_phrase(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AADS_API_URL", "http://u:PWSECRET@host:notaport/x?token=QTOK")
    requests = _install_real_client(monkeypatch, _ok_agents)

    with caplog.at_level(logging.INFO):
        agents, errors, reached = await tool_executor._fetch_pc_agent_statuses_from_api_detailed()

    assert errors == ["candidate-1: invalid candidate"]
    assert reached is True and agents and len(requests) == 1
    for needle in ("PWSECRET", "QTOK", "notaport"):
        assert needle not in repr(errors) + caplog.text, "secret leaked"
