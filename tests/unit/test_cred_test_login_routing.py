"""credential_test_login 브라우저 라우팅 / 타임아웃 상한 회귀 테스트."""
from __future__ import annotations

from typing import Any

import pytest

from app.api import ceo_chat_tools

CREDENTIAL_ID = "0b2b9e16-c06f-4f6f-9a91-f56726d43507"
TENANT_ID = "00000000-0000-0000-0000-000000000012"


class _FakePage:
    url = "https://example.test/dashboard"

    async def goto(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeCtx:
    async def new_page(self) -> _FakePage:
        return _FakePage()


@pytest.fixture
def observed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_get_credential(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "id": CREDENTIAL_ID,
            "tenant_id": TENANT_ID,
            "service": "example_service",
            "login_url": "https://example.test/login",
            "username": "u",
            "password": "p",
            "login_steps": [{"action": "noop"}],
        }

    async def fake_noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_steps(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def fake_acquire(**kwargs: Any) -> tuple[Any, None]:
        seen["kwargs"] = kwargs
        return _FakeCtx(), None

    monkeypatch.setattr("app.core.credential_vault.get_credential", fake_get_credential)
    monkeypatch.setattr("app.core.credential_vault.mark_used", fake_noop)
    monkeypatch.setattr("app.core.credential_vault.mark_verified", fake_noop)
    monkeypatch.setattr("app.core.credential_vault.execute_login_steps", fake_steps)
    monkeypatch.setattr("app.core.credential_vault.login_session_completed", fake_steps)
    monkeypatch.setattr("app.browser_bridge.aads_adapter.acquire_browser_context", fake_acquire)
    return seen


@pytest.mark.asyncio
async def test_no_session_no_work_key_uses_server_headless(observed: dict[str, Any]) -> None:
    result = await ceo_chat_tools.tool_credential_test_login(CREDENTIAL_ID, tenant_id=TENANT_ID)

    kwargs = observed["kwargs"]
    assert kwargs["browser_work_key"] is None
    assert kwargs["browser_session_id"] is None
    assert kwargs["prefer_headless"] is True
    assert "status: success" in result
    assert "browser_work_key: (none, server_headless)" in result


@pytest.mark.asyncio
async def test_explicit_work_key_is_passed_through(observed: dict[str, Any]) -> None:
    result = await ceo_chat_tools.tool_credential_test_login(
        CREDENTIAL_ID, tenant_id=TENANT_ID, browser_work_key="pc-agent-1"
    )

    kwargs = observed["kwargs"]
    assert kwargs["browser_work_key"] == "pc-agent-1"
    assert kwargs.get("prefer_headless") is not True
    assert "browser_work_key: pc-agent-1" in result


@pytest.mark.asyncio
async def test_explicit_session_id_drops_work_key(observed: dict[str, Any]) -> None:
    await ceo_chat_tools.tool_credential_test_login(
        CREDENTIAL_ID,
        tenant_id=TENANT_ID,
        browser_session_id="sess-1",
        browser_work_key="pc-agent-1",
    )

    kwargs = observed["kwargs"]
    assert kwargs["browser_session_id"] == "sess-1"
    assert kwargs["browser_work_key"] is None
    assert kwargs.get("prefer_headless") is not True


def test_outer_timeout_exceeds_headless_launch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.browser_bridge.aads_adapter import _HEADLESS_LAUNCH_TIMEOUT_SECONDS

    assert ceo_chat_tools._e2e_credential_browser_timeout() > _HEADLESS_LAUNCH_TIMEOUT_SECONDS

    monkeypatch.setattr(ceo_chat_tools, "_E2E_CREDENTIAL_BROWSER_TEST_TIMEOUT_SECONDS", 20.0)
    assert ceo_chat_tools._e2e_credential_browser_timeout() > _HEADLESS_LAUNCH_TIMEOUT_SECONDS

    monkeypatch.setattr("app.browser_bridge.aads_adapter._HEADLESS_LAUNCH_TIMEOUT_SECONDS", 90.0)
    assert ceo_chat_tools._e2e_credential_browser_timeout() > 90.0
