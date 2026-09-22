"""Interactive server-browser streaming and control over a single CDP session.

The session is intentionally bound to one WebSocket connection.  Frames and
input therefore stay on the same API slot during a blue/green deployment and
we never try to serialize a live Playwright/CDP object through Redis or the DB.
The latest frame is persisted separately by the API for reconnect/recovery.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from urllib.parse import urlparse

VIEWPORT_WIDTH = 1366
VIEWPORT_HEIGHT = 768


class BrowserLiveControlError(RuntimeError):
    """A user-visible live-control failure."""


class ServerBrowserLiveSession:
    """Own one headless Chromium page, its screencast, and CDP input channel."""

    def __init__(self, *, target_url: str, quality: int = 70) -> None:
        parsed = urlparse(str(target_url or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise BrowserLiveControlError("unsupported_target_url")
        self.target_url = target_url
        self.quality = max(30, min(int(quality), 90))
        self.width = VIEWPORT_WIDTH
        self.height = VIEWPORT_HEIGHT
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._closed = False

    async def start(self) -> dict[str, Any]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - runtime image contract
            raise BrowserLiveControlError("playwright_unavailable") from exc

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            self._context = await self._browser.new_context(
                viewport={"width": self.width, "height": self.height},
                ignore_https_errors=True,
            )
            self._page = await self._context.new_page()
            await self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=20_000)
            self._cdp = await self._context.new_cdp_session(self._page)
            await self._cdp.send("Page.enable")
            self._cdp.on("Page.screencastFrame", self._on_screencast_frame)
            await self._cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": self.quality,
                    "maxWidth": self.width,
                    "maxHeight": self.height,
                    "everyNthFrame": 1,
                },
            )
            return await self.page_state()
        except Exception:
            await self.close()
            raise

    async def _on_screencast_frame(self, event: dict[str, Any]) -> None:
        if self._closed or self._cdp is None:
            return
        session_id = event.get("sessionId")
        if session_id is not None:
            with contextlib.suppress(Exception):
                await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        payload = {
            "type": "frame",
            "frame": str(event.get("data") or ""),
            "media_type": "image/jpeg",
            "width": self.width,
            "height": self.height,
            "metadata": event.get("metadata") or {},
        }
        if self._frames.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._frames.get_nowait()
        self._frames.put_nowait(payload)

    async def next_frame(self, *, timeout: float = 15.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._frames.get(), timeout=timeout)

    async def page_state(self) -> dict[str, Any]:
        if self._page is None:
            return {"url": self.target_url, "title": ""}
        title = ""
        with contextlib.suppress(Exception):
            title = await self._page.title()
        return {"url": str(self._page.url or self.target_url), "title": title}

    async def apply_control(self, command: dict[str, Any]) -> dict[str, Any]:
        if self._cdp is None:
            raise BrowserLiveControlError("live_session_not_started")
        action = str(command.get("action") or "").strip().lower()
        if action == "click":
            return await self._click(command)
        if action == "type":
            return await self._type(command)
        if action == "press":
            return await self._press(command)
        raise BrowserLiveControlError("unsupported_control_action")

    async def _click(self, command: dict[str, Any]) -> dict[str, Any]:
        x = self._bounded_coordinate(command.get("x"), self.width)
        y = self._bounded_coordinate(command.get("y"), self.height)
        element = await self._describe_element_at(x, y)
        await self._cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        selector = str(element.get("selector") or "")
        return {
            "action": "click",
            "element": element,
            "recipe_step": {
                "action": "click",
                "selector": selector,
                "risk": "READ",
                "description": str(element.get("label") or "사용자 화면 클릭")[:200],
            } if selector else None,
        }

    async def _type(self, command: dict[str, Any]) -> dict[str, Any]:
        text = str(command.get("text") or "")
        if not text:
            raise BrowserLiveControlError("empty_text")
        element = await self._describe_active_element()
        if command.get("replace") is not False:
            await self._cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2})
            await self._cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2})
        await self._cdp.send("Input.insertText", {"text": text})
        selector = str(element.get("selector") or "")
        is_secret = bool(command.get("secret")) or str(element.get("input_type") or "").lower() == "password"
        recipe_step: dict[str, Any] | None = None
        if selector:
            recipe_step = {
                "action": "fill",
                "selector": selector,
                "risk": "WRITE_EXTERNAL" if is_secret else "READ",
                "description": str(element.get("label") or "사용자 화면 입력")[:200],
            }
            if is_secret:
                recipe_step.update({
                    "credential": True,
                    "credential_variable": self._credential_variable(element),
                })
            else:
                recipe_step["value"] = text
        return {
            "action": "type",
            "element": element,
            "secret": is_secret,
            "recipe_step": recipe_step,
        }

    async def _press(self, command: dict[str, Any]) -> dict[str, Any]:
        key = str(command.get("key") or "Enter")[:40]
        element = await self._describe_active_element()
        code, virtual_key = self._key_metadata(key)
        base = {"key": key, "code": code, "windowsVirtualKeyCode": virtual_key, "nativeVirtualKeyCode": virtual_key}
        await self._cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        await self._cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
        selector = str(element.get("selector") or "")
        return {
            "action": "press",
            "element": element,
            "recipe_step": {
                "action": "press",
                "selector": selector,
                "value": key,
                "risk": "READ",
                "description": f"사용자 키 입력: {key}",
            } if selector else None,
        }

    async def _describe_element_at(self, x: float, y: float) -> dict[str, Any]:
        return await self._evaluate_element_descriptor(
            f"document.elementFromPoint({json.dumps(x)}, {json.dumps(y)})"
        )

    async def _describe_active_element(self) -> dict[str, Any]:
        return await self._evaluate_element_descriptor("document.activeElement")

    async def _evaluate_element_descriptor(self, element_expression: str) -> dict[str, Any]:
        expression = f"""
        (() => {{
          const el = {element_expression};
          if (!el || el === document.documentElement || el === document.body) return {{}};
          const esc = (value) => window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
          const attr = (name) => el.getAttribute && el.getAttribute(name);
          let selector = '';
          if (el.id) selector = '#' + esc(el.id);
          if (!selector && attr('data-testid')) selector = `[data-testid="${{String(attr('data-testid')).replace(/"/g, '\\"')}}"]`;
          if (!selector && attr('name')) selector = `${{el.tagName.toLowerCase()}}[name="${{String(attr('name')).replace(/"/g, '\\"')}}"]`;
          if (!selector && attr('aria-label')) selector = `${{el.tagName.toLowerCase()}}[aria-label="${{String(attr('aria-label')).replace(/"/g, '\\"')}}"]`;
          if (!selector) {{
            const parts = [];
            let node = el;
            while (node && node.nodeType === 1 && node !== document.body && parts.length < 6) {{
              let part = node.tagName.toLowerCase();
              const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((child) => child.tagName === node.tagName) : [];
              if (siblings.length > 1) part += `:nth-of-type(${{siblings.indexOf(node) + 1}})`;
              parts.unshift(part);
              node = node.parentElement;
            }}
            selector = parts.join(' > ');
          }}
          return {{
            selector,
            tag: String(el.tagName || '').toLowerCase(),
            input_type: String(attr('type') || '').toLowerCase(),
            label: String(attr('aria-label') || attr('placeholder') || attr('name') || el.innerText || el.textContent || '').trim().slice(0, 200),
          }};
        }})()
        """
        result = await self._cdp.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        value = ((result.get("result") or {}).get("value") or {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _bounded_coordinate(raw: Any, maximum: int) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise BrowserLiveControlError("invalid_coordinate") from exc
        return max(0.0, min(value, float(maximum - 1)))

    @staticmethod
    def _credential_variable(element: dict[str, Any]) -> str:
        raw = str(element.get("label") or "password").strip().lower()
        normalized = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
        return (normalized or "password")[:60]

    @staticmethod
    def _key_metadata(key: str) -> tuple[str, int]:
        mapping = {
            "Enter": ("Enter", 13),
            "Tab": ("Tab", 9),
            "Escape": ("Escape", 27),
            "Backspace": ("Backspace", 8),
            "ArrowUp": ("ArrowUp", 38),
            "ArrowDown": ("ArrowDown", 40),
        }
        return mapping.get(key, (key, ord(key.upper()) if len(key) == 1 else 0))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cdp is not None:
            with contextlib.suppress(Exception):
                await self._cdp.send("Page.stopScreencast")
            with contextlib.suppress(Exception):
                await self._cdp.detach()
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
