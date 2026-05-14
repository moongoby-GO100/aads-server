from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc_agent", "commands"))

import browser_auto  # noqa: E402


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
