from __future__ import annotations

import asyncio

import pytest

from app.services.browser_live_control import BrowserLiveControlError, ServerBrowserLiveSession


class FakeCDP:
    def __init__(self, element: dict | None = None) -> None:
        self.element = element or {
            "selector": "input[name=\"q\"]",
            "tag": "input",
            "input_type": "text",
            "label": "검색",
        }
        self.calls: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None):
        payload = params or {}
        self.calls.append((method, payload))
        if method == "Runtime.evaluate":
            return {"result": {"value": self.element}}
        return {}


def test_click_uses_cdp_input_and_returns_replayable_selector() -> None:
    live = ServerBrowserLiveSession(target_url="https://example.com")
    cdp = FakeCDP()
    live._cdp = cdp

    result = asyncio.run(live.apply_control({"action": "click", "x": 22, "y": 44}))

    methods = [method for method, _ in cdp.calls]
    assert methods == [
        "Runtime.evaluate",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert result["recipe_step"] == {
        "action": "click",
        "selector": 'input[name="q"]',
        "risk": "READ",
        "description": "검색",
    }


def test_password_text_is_not_returned_in_recipe_step() -> None:
    live = ServerBrowserLiveSession(target_url="https://example.com/login")
    live._cdp = FakeCDP({
        "selector": "#password",
        "tag": "input",
        "input_type": "password",
        "label": "비밀번호",
    })

    result = asyncio.run(live.apply_control({"action": "type", "text": "never-persist-this"}))

    step = result["recipe_step"]
    assert result["secret"] is True
    assert "value" not in step
    assert step["credential"] is True
    assert step["credential_variable"]


def test_control_rejects_invalid_coordinates_and_unknown_actions() -> None:
    live = ServerBrowserLiveSession(target_url="https://example.com")
    live._cdp = FakeCDP()

    with pytest.raises(BrowserLiveControlError, match="invalid_coordinate"):
        asyncio.run(live.apply_control({"action": "click", "x": "bad", "y": 10}))
    with pytest.raises(BrowserLiveControlError, match="unsupported_control_action"):
        asyncio.run(live.apply_control({"action": "drop-database"}))


def test_target_url_must_be_http() -> None:
    with pytest.raises(BrowserLiveControlError, match="unsupported_target_url"):
        ServerBrowserLiveSession(target_url="file:///etc/passwd")
