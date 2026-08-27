from __future__ import annotations

import asyncio
import json
import inspect

import pytest

from app.api import ceo_chat_tools
from app.browser_bridge.service import _LocalAgentPage
from app.services import tool_registry
from app.services.tool_executor import ToolExecutor


def test_tool_registry_descriptions_expose_windows_command_aliases() -> None:
    pc_tool = tool_registry._TOOLS["pc_execute"]
    device_tool = tool_registry._TOOLS["device_execute"]

    assert "cmd" in pc_tool["description"]
    assert "powershell" in pc_tool["description"]
    assert "app_launch" in pc_tool["description"]
    assert "file_upload" in pc_tool["description"]
    assert "file_download" in pc_tool["description"]
    assert "cmd" in device_tool["description"]
    assert "powershell" in device_tool["description"]
    assert "file_upload" in device_tool["description"]
    assert "file_download" in device_tool["description"]


def test_ceo_chat_tools_exposes_pc_and_device_execute() -> None:
    names = {tool["name"] for tool in ceo_chat_tools.TOOL_DEFINITIONS}
    assert {"pc_execute", "device_execute"} <= names


@pytest.mark.asyncio
async def test_ceo_chat_tools_pc_execute_delegates_to_tool_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dispatch(self, name: str, params: dict[str, object]):  # noqa: ANN001
        assert name == "pc_execute"
        assert params["command_type"] == "system_info"
        return {"status": "success", "command_id": "pc-1"}

    monkeypatch.setattr(ToolExecutor, "_dispatch", fake_dispatch)

    result = await ceo_chat_tools.execute_tool("pc_execute", {"command_type": "system_info"}, dsn="")

    assert json.loads(result) == {"status": "success", "command_id": "pc-1"}


@pytest.mark.asyncio
async def test_device_execute_routes_pc_command_to_pc_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ToolExecutor()

    async def fake_execute_routed_command(**kwargs):  # noqa: ANN003
        assert kwargs["command_type"] == "powershell"
        assert kwargs["params"] == {"command": "Get-Process"}
        return {"status": "success", "backend": "pc_agent", "command_id": "pc-2"}

    async def fail_send_command(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("device_manager.send_command should not be used for PC commands without a device target")

    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.execute_routed_command", fake_execute_routed_command)
    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.online_agents_count", lambda: 1)
    monkeypatch.setattr("app.services.device_manager.device_manager.get_device", lambda _agent_id: None)
    monkeypatch.setattr("app.services.device_manager.device_manager.send_command", fail_send_command)

    result = await executor._device_execute(
        {
            "command_type": "powershell",
            "params": {"command": "Get-Process"},
        }
    )

    assert result["status"] == "success"
    assert result["backend"] == "pc_agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", ["file_upload", "file_download"])
async def test_device_execute_routes_file_transfer_to_pc_agent(
    monkeypatch: pytest.MonkeyPatch,
    command_type: str,
) -> None:
    executor = ToolExecutor()

    async def fake_execute_routed_command(**kwargs):  # noqa: ANN003
        assert kwargs["command_type"] == command_type
        return {"status": "success", "backend": "pc_agent"}

    async def fail_send_command(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("file transfer must be routed to PC Agent")

    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.execute_routed_command", fake_execute_routed_command)
    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.online_agents_count", lambda: 1)
    monkeypatch.setattr("app.services.device_manager.device_manager.get_device", lambda _agent_id: None)
    monkeypatch.setattr("app.services.device_manager.device_manager.send_command", fail_send_command)

    result = await executor._device_execute({"command_type": command_type, "params": {}})

    assert result == {"status": "success", "backend": "pc_agent"}


@pytest.mark.asyncio
async def test_device_list_includes_pc_agent_manager_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ToolExecutor()
    monkeypatch.setattr("app.services.device_manager.device_manager.get_devices", lambda _device_type: [])
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.list_agent_statuses",
        lambda: [
            {
                "agent_id": "ceo-pc",
                "hostname": "ceo-desktop",
                "os_info": "Windows 11",
                "capabilities": ["interactive_browser"],
                "status": "online",
                "connected_at": "2026-07-23T00:00:00Z",
                "last_seen": "2026-07-23T00:00:05Z",
                "heartbeat_age_seconds": 1.2,
            }
        ],
    )

    result = await executor._device_list({"device_type": "pc"})

    assert result["count"] == 1
    assert result["devices"][0]["agent_id"] == "ceo-pc"
    assert result["devices"][0]["device_type"] == "pc"
    assert result["devices"][0]["status"] == "online"


@pytest.mark.asyncio
async def test_device_list_uses_api_fallback_outside_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ToolExecutor()
    monkeypatch.setattr("app.services.device_manager.device_manager.get_devices", lambda _device_type: [])
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.list_agent_statuses",
        lambda: [],
    )

    async def fake_fetch_statuses() -> list[dict[str, object]]:
        return [
            {
                "agent_id": "ceo-pc",
                "hostname": "ceo-desktop",
                "os_info": "Windows 11",
                "capabilities": ["interactive_browser"],
                "status": "online",
                "connected_at": "2026-07-23T00:00:00Z",
                "last_seen": "2026-07-23T00:00:05Z",
                "heartbeat_age_seconds": 1.2,
            }
        ]

    monkeypatch.setattr(
        "app.services.tool_executor._fetch_pc_agent_statuses_from_api",
        fake_fetch_statuses,
    )

    result = await executor._device_list({"device_type": "pc"})

    assert result["count"] == 1
    assert result["devices"][0]["agent_id"] == "ceo-pc"
    assert result["devices"][0]["status"] == "online"


@pytest.mark.asyncio
async def test_pc_list_agents_uses_api_fallback_outside_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ToolExecutor()
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.list_agent_statuses",
        lambda: [],
    )

    async def fake_fetch_statuses() -> list[dict[str, object]]:
        return [
            {
                "agent_id": "oby-ceo",
                "hostname": "ceo-desktop",
                "capabilities": ["interactive_browser"],
                "command_types": ["browser_launch", "browser_tabs"],
                "status": "online",
            }
        ]

    monkeypatch.setattr(
        "app.services.tool_executor._fetch_pc_agent_statuses_from_api",
        fake_fetch_statuses,
    )

    result = await executor._pc_list_agents({})

    assert result["count"] == 1
    assert result["online_count"] == 1
    assert result["backend_source"] == "api_fallback"
    assert result["agents"][0]["agent_id"] == "oby-ceo"


@pytest.mark.asyncio
async def test_browser_connect_forwards_tenant_id(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, object] = {}

    async def fake_browser_connect(**kwargs):  # noqa: ANN003
        received.update(kwargs)
        return "ok"

    monkeypatch.setattr(ceo_chat_tools, "tool_browser_connect", fake_browser_connect)

    result = await ToolExecutor()._browser_connect(
        {
            "action": "ensure_pc_cdp",
            "agent_id": "ceo-pc",
            "url": "https://aads.newtalk.kr/chat/session-id",
            "tenant_id": "tenant-1",
        }
    )

    assert result == "ok"
    assert received["tenant_id"] == "tenant-1"


def test_browser_connect_preserves_chat_fragment_in_source() -> None:
    source = inspect.getsource(ceo_chat_tools.tool_browser_connect)

    assert '_redirect += "#" + _parsed.fragment' in source


class _FakeAadsAuthPage:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, str]] = []
        self.evaluate_token = ""

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))

    async def evaluate(self, _script: str, token: str) -> None:
        self.evaluate_token = token


class _FakeAgentVaultLoginLocator:
    def __init__(self, page: "_FakeAgentVaultLoginPage", selector: str) -> None:
        self.page = page
        self.selector = selector
        self.first = self

    async def clear(self, *, timeout: int) -> None:
        self.page.events.append(("clear", self.selector, str(timeout)))

    async def fill(self, value: str, *, timeout: int) -> None:
        self.page.events.append(("fill", self.selector, value, str(timeout)))

    async def click(self, *, timeout: int) -> None:
        self.page.events.append(("click", self.selector, str(timeout)))
        self.page.url = "https://v2.newtalk.kr/dashboard"
        self.page.login_visible = False

    async def is_visible(self, *, timeout: int) -> bool:
        self.page.events.append(("visible", self.selector, str(timeout)))
        return self.page.login_visible and (
            "password" in self.selector
            or "email" in self.selector
            or "submit" in self.selector
            or "Login" in self.selector
            or "로그인" in self.selector
        )


class _FakeAgentVaultLoginPage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.login_visible = True
        self.events: list[tuple[str, ...]] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.url = url
        self.events.append(("goto", url, wait_until, str(timeout)))

    def locator(self, selector: str) -> _FakeAgentVaultLoginLocator:
        return _FakeAgentVaultLoginLocator(self, selector)

    async def wait_for_timeout(self, ms: int) -> None:
        self.events.append(("wait", str(ms)))


@pytest.mark.asyncio
async def test_apply_aads_e2e_url_injects_token_and_preserves_fragment() -> None:
    page = _FakeAadsAuthPage()

    target = await ceo_chat_tools._apply_aads_e2e_url(
        page,
        "https://aads.newtalk.kr/e2e-auth.html?token=secret-token&redirect=/chat%23session-1",
    )

    assert page.evaluate_token == "secret-token"
    assert page.goto_calls == [
        ("https://aads.newtalk.kr/e2e-auth.html", "domcontentloaded"),
        ("https://aads.newtalk.kr/chat#session-1", "domcontentloaded"),
    ]
    assert target == "https://aads.newtalk.kr/chat#session-1"


@pytest.mark.asyncio
async def test_agent_vault_login_uses_generic_form_and_marks_used(monkeypatch: pytest.MonkeyPatch) -> None:
    marked: dict[str, object] = {}

    async def fake_mark_agent_credential_used(**kwargs):  # noqa: ANN003
        marked.update(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.agent_vault_service.mark_agent_credential_used",
        fake_mark_agent_credential_used,
    )
    page = _FakeAgentVaultLoginPage()

    ok = await ceo_chat_tools._login_with_agent_vault_credential(
        page,
        {
            "id": "00000000-0000-0000-0000-000000000011",
            "origin": "https://v2.newtalk.kr",
            "work_key": "aads-ceo-browser",
            "username": "admin@example.test",
            "password": "secret-password",
        },
        "https://v2.newtalk.kr/chat",
        tenant_id="00000000-0000-0000-0000-000000000012",
        browser_work_key="aads-ceo-browser",
    )

    assert ok is True
    assert ("goto", "https://v2.newtalk.kr/login", "domcontentloaded", "15000") in page.events
    assert any(event[:3] == ("fill", "input[type='password']", "secret-password") for event in page.events)
    assert marked["credential_id"] == "00000000-0000-0000-0000-000000000011"
    assert marked["origin"] == "https://v2.newtalk.kr"
    assert marked["details"] == {"method": "form_login", "target_url": "https://v2.newtalk.kr/chat"}


def test_credential_test_login_no_longer_uses_genspark_specific_login() -> None:
    source = inspect.getsource(ceo_chat_tools.tool_credential_test_login)

    assert "_attempt_genspark_login" not in source
    assert "_login_with_agent_vault_credential" in source


@pytest.mark.asyncio
async def test_credential_test_login_agent_vault_times_out_to_api_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    credential_id = "00000000-0000-0000-0000-000000000011"
    tenant_id = "00000000-0000-0000-0000-000000000012"

    async def fake_get_credential(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    class _FakePool:
        async def fetchrow(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return {
                "id": credential_id,
                "tenant_id": tenant_id,
                "work_key": "aads-ceo-browser",
                "origin": "https://v2.newtalk.kr",
                "label": "AADS",
                "username_enc": "ceo@example.test",
                "password_enc": "secret-password",
            }

    async def fake_acquire_browser_context(**_kwargs):  # noqa: ANN003
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return None

    class _FakeSession:
        def __init__(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return None

        def get(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return _FakeResponse()

    monkeypatch.setattr("app.core.credential_vault.get_credential", fake_get_credential)
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda value: value)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _FakePool())
    monkeypatch.setattr("app.browser_bridge.aads_adapter.acquire_browser_context", fake_acquire_browser_context)
    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    monkeypatch.setattr(ceo_chat_tools, "_AGENT_VAULT_BROWSER_TEST_TIMEOUT_SECONDS", 0.01)

    result = await ceo_chat_tools.tool_credential_test_login(
        credential_id,
        tenant_id=tenant_id,
    )

    assert "[API 폴백]" in result
    assert "vault_type: agent_vault" in result
    assert "TIMEOUT_AFTER_0.01s" in result


@pytest.mark.asyncio
async def test_credential_test_login_agent_vault_aads_uses_api_login_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    credential_id = "00000000-0000-0000-0000-000000000011"
    tenant_id = "00000000-0000-0000-0000-000000000012"
    marked: dict[str, object] = {}

    async def fake_get_credential(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    class _FakePool:
        async def fetchrow(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return {
                "id": credential_id,
                "tenant_id": tenant_id,
                "work_key": "aads-ceo-browser",
                "origin": "https://aads.newtalk.kr",
                "label": "AADS",
                "username_enc": "ceo@example.test",
                "password_enc": "secret-password",
            }

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return None

    class _FakeSession:
        def __init__(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return None

        def post(self, url, *, json, ssl):  # noqa: ANN001, A002
            marked["api_url"] = url
            marked["email"] = json["email"]
            marked["password"] = json["password"]
            marked["ssl"] = ssl
            return _FakeResponse()

    async def fake_acquire_browser_context(**_kwargs):  # noqa: ANN003
        raise AssertionError("AADS API login fast-path must not open Browser Bridge")

    async def fake_mark_agent_credential_used(**kwargs):  # noqa: ANN003
        marked["used"] = kwargs

    monkeypatch.setattr("app.core.credential_vault.get_credential", fake_get_credential)
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda value: value)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _FakePool())
    monkeypatch.setattr("app.browser_bridge.aads_adapter.acquire_browser_context", fake_acquire_browser_context)
    monkeypatch.setattr("app.services.agent_vault_service.mark_agent_credential_used", fake_mark_agent_credential_used)
    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)

    result = await ceo_chat_tools.tool_credential_test_login(
        credential_id,
        tenant_id=tenant_id,
        browser_work_key="agent-vault-test-aads",
    )

    assert "[API 로그인 테스트]" in result
    assert "status: success" in result
    assert "vault_type: agent_vault" in result
    assert marked["api_url"] == "https://aads.newtalk.kr/api/v1/auth/login"
    assert marked["email"] == "ceo@example.test"
    assert marked["password"] == "secret-password"
    assert marked["ssl"] is False
    assert marked["used"] == {
        "tenant_id": tenant_id,
        "credential_id": credential_id,
        "work_key": "agent-vault-test-aads",
        "origin": "https://aads.newtalk.kr",
        "details": {
            "method": "api_login_test",
            "target_url": "https://aads.newtalk.kr/api/v1/auth/login",
        },
    }


@pytest.mark.asyncio
async def test_local_agent_evaluate_accepts_playwright_argument(monkeypatch) -> None:
    page = object.__new__(_LocalAgentPage)
    captured: dict[str, str] = {}

    async def fake_run(command_type, params, **_kwargs):
        captured["command_type"] = command_type
        captured["expression"] = params["expression"]
        return {"value": "ok"}

    monkeypatch.setattr(page, "_run_browser_command", fake_run)

    result = await page.evaluate("(token) => token", "secret-token")

    assert result == "ok"
    assert captured["command_type"] == "browser_eval"
    assert "secret-token" not in captured["expression"][:200]
    assert captured["expression"].endswith('(secret-token)') is False
    assert captured["expression"].endswith('(\"secret-token\")')
