from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc_agent", "commands"))

import browser_auto  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_browser_auto_state() -> None:
    browser_auto.CDPSessionManager._sessions.clear()
    browser_auto.CDPCommandGuardManager._guards.clear()


@pytest.mark.asyncio
async def test_browser_eval_maps_syntax_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send_cdp_command(_port, _method, _params=None, **_kwargs):
        return {
            "result": {
                "type": "object",
                "subtype": "error",
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {
                        "className": "SyntaxError",
                        "description": "SyntaxError: Unexpected token '}'",
                    },
                },
            },
            "_target": {"id": "page-1", "url": "https://www.vvic.com/search"},
        }

    monkeypatch.setattr(browser_auto, "_send_cdp_command", fake_send_cdp_command)

    result = await browser_auto.browser_eval({"expression": "(() => {}})()", "port": 9222})

    assert result["status"] == "error"
    assert result["data"]["error_code"] == "SYNTAX_ERROR"
    assert "Unexpected token" in result["data"]["error"]


@pytest.mark.asyncio
async def test_browser_eval_returns_spa_shell_only_when_vvic_shell_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send_cdp_command(_port, _method, _params=None, **_kwargs):
        return {
            "result": {
                "type": "undefined",
                "value": None,
            },
            "_target": {
                "id": "page-1",
                "url": "https://www.vvic.com/search?keyword=%EC%9B%90%ED%94%BC%EC%8A%A4",
                "title": "VVIC Search",
            },
        }

    async def fake_collect_page_diagnostics(_port, **_kwargs):
        return {
            "readyState": "complete",
            "href": "https://www.vvic.com/search?keyword=%EC%9B%90%ED%94%BC%EC%8A%A4",
            "title": "VVIC Search",
            "bodyTextLength": 12,
            "matchedSelector": "",
            "cardCount": 0,
        }

    monkeypatch.setattr(browser_auto, "_send_cdp_command", fake_send_cdp_command)
    monkeypatch.setattr(browser_auto, "_collect_page_diagnostics", fake_collect_page_diagnostics)

    result = await browser_auto.browser_eval({"expression": "document.body?.innerText", "port": 9222})

    assert result["status"] == "error"
    assert result["data"]["error_code"] == "SPA_SHELL_ONLY"
    assert result["data"]["diagnostics"]["cardCount"] == 0


@pytest.mark.asyncio
async def test_browser_eval_skips_diagnostics_when_timeout_budget_is_almost_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_send_cdp_command(_port, _method, _params=None, **_kwargs):
        return {
            "result": {
                "type": "string",
                "value": "ok",
            },
            "_target": {
                "id": "page-1",
                "url": "https://www.vvic.com/search?keyword=%EC%9B%90%ED%94%BC%EC%8A%A4",
                "title": "VVIC Search",
            },
        }

    async def fail_collect_page_diagnostics(_port, **_kwargs):
        raise AssertionError("diagnostics should be skipped when timeout budget is nearly exhausted")

    ticks = iter([100.0, 104.8])

    monkeypatch.setattr(browser_auto, "_send_cdp_command", fake_send_cdp_command)
    monkeypatch.setattr(browser_auto, "_collect_page_diagnostics", fail_collect_page_diagnostics)
    monkeypatch.setattr(browser_auto, "_record_cdp_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_auto, "_time", lambda: next(ticks))

    result = await browser_auto.browser_eval(
        {
            "expression": "JSON.stringify({ok:true})",
            "port": 9222,
            "work_key": "ntv2-vvic-scrape",
            "command_timeout_seconds": 5,
            "evaluate_timeout_seconds": 4.5,
        }
    )

    assert result["status"] == "success"
    assert result["data"]["value"] == "ok"
    assert result["data"]["diagnostics"] == {}


@pytest.mark.asyncio
async def test_browser_eval_defaults_to_non_await_promise(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_send_cdp_command(_port, _method, params=None, **_kwargs):
        captured["params"] = dict(params or {})
        return {
            "result": {
                "type": "string",
                "value": "ok",
            },
            "_target": {
                "id": "page-1",
                "url": "https://www.vvic.com/search?keyword=%EC%9B%90%ED%94%BC%EC%8A%A4",
                "title": "VVIC Search",
            },
        }

    async def fake_collect_page_diagnostics(_port, **_kwargs):
        return {}

    monkeypatch.setattr(browser_auto, "_send_cdp_command", fake_send_cdp_command)
    monkeypatch.setattr(browser_auto, "_collect_page_diagnostics", fake_collect_page_diagnostics)

    result = await browser_auto.browser_eval(
        {
            "expression": "JSON.stringify({ok:true})",
            "port": 9555,
            "work_key": "ntv2-vvic-scrape",
        }
    )

    assert result["status"] == "success"
    assert captured["params"]["awaitPromise"] is False  # type: ignore[index]


def test_runtime_timeout_cleanup_keeps_work_key_session() -> None:
    browser_auto.CDPSessionManager.register("ntv2-vvic-scrape", 9555, "/tmp/vvic", pid=1234)
    exc = browser_auto.CDPCommandError(
        browser_auto._ERROR_RUNTIME_EVALUATE_TIMEOUT,
        "Runtime.evaluate timed out",
        details={
            "runtime_cleanup": {"attempted": True, "succeeded": True},
            "detach_cleanup": {"attempted": True, "succeeded": True},
        },
    )

    result = browser_auto._command_error_response(
        9555,
        {"work_key": "ntv2-vvic-scrape", "port": 9555},
        exc,
    )

    assert result["status"] == "error"
    assert result["data"]["session_released"] is False
    assert browser_auto.CDPSessionManager.get_session("ntv2-vvic-scrape") is not None


@pytest.mark.asyncio
async def test_close_session_closes_only_registered_work_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_auto.CDPSessionManager.register(
        "aads-chat-e2e",
        9333,
        "/tmp/aads-chat-e2e",
        pid=1234,
        auto_close=True,
    )
    probe_calls = 0

    async def fake_probe(_port: int):
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/test"}
        return None

    async def fake_targets(_port: int):
        return [{"type": "page", "id": "page-1", "url": "https://aads.newtalk.kr/"}]

    async def fake_send(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(browser_auto, "_probe_cdp_version", fake_probe)
    monkeypatch.setattr(browser_auto, "_list_cdp_targets", fake_targets)
    monkeypatch.setattr(browser_auto, "_send_cdp", fake_send)

    result = await browser_auto.browser_close_session({"work_key": "aads-chat-e2e"})

    assert result["status"] == "success"
    assert result["data"]["closed"] is True
    assert result["data"]["targets_before"] == 1
    assert browser_auto.CDPSessionManager.get_session("aads-chat-e2e") is None


@pytest.mark.asyncio
async def test_close_session_protects_general_browser() -> None:
    browser_auto.CDPSessionManager.register("general", 9222, "/tmp/general", pid=99)

    result = await browser_auto.browser_close_session({"work_key": "general"})

    assert result["status"] == "error"
    assert result["data"]["error_code"] == "SHARED_BROWSER_PROTECTED"
    assert browser_auto.CDPSessionManager.get_session("general") is not None


@pytest.mark.asyncio
async def test_close_session_uses_server_port_after_agent_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    probe_calls = 0

    async def fake_probe(_port: int):
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9555/devtools/browser/orphan"}
        return None

    async def fake_targets(_port: int):
        return [{"type": "page", "id": "orphan-page", "url": "https://aads.newtalk.kr/"}]

    async def fake_send(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(browser_auto, "_probe_cdp_version", fake_probe)
    monkeypatch.setattr(browser_auto, "_list_cdp_targets", fake_targets)
    monkeypatch.setattr(browser_auto, "_send_cdp", fake_send)

    result = await browser_auto.browser_close_session({
        "work_key": "orphan-e2e",
        "port": 9555,
    })

    assert result["status"] == "success"
    assert result["data"]["closed"] is True
    assert result["data"]["port"] == 9555


@pytest.mark.asyncio
async def test_idle_cleanup_targets_only_auto_close_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    ephemeral = browser_auto.CDPSessionManager.register(
        "e2e-expired",
        9333,
        "/tmp/e2e-expired",
        auto_close=True,
        idle_timeout_seconds=60,
    )
    persistent = browser_auto.CDPSessionManager.register(
        "persistent",
        9444,
        "/tmp/persistent",
        auto_close=False,
        idle_timeout_seconds=60,
    )
    ephemeral.last_heartbeat_at = 0
    persistent.last_heartbeat_at = 0
    closed: list[str] = []

    async def fake_close(params):
        closed.append(params["work_key"])
        return {"status": "success", "data": {"closed": True}}

    monkeypatch.setattr(browser_auto, "browser_close_session", fake_close)
    monkeypatch.setattr(browser_auto, "_time", lambda: 1_000.0)

    results = await browser_auto.cleanup_idle_browser_sessions()

    assert closed == ["e2e-expired"]
    assert results[0]["idle_seconds"] == 1000
