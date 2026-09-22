"""Isolated CDP tab capture/input. Never activates a window or uses OS input."""
from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from importlib import import_module

cdp = import_module(f"{__package__}.browser_auto")

_SESSIONS: dict[str, 'Tab'] = {}
_OPEN_LOCK = asyncio.Lock()


def validate_url(value: str) -> str:
    if value == 'about:blank':
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('unsupported_target_url')
    return value


class Tab:
    def __init__(self, port: int, work_key: str, target_id: str, ws: Any):
        self.port, self.work_key, self.target_id, self.ws = port, work_key, target_id, ws
        self.lock = asyncio.Lock()
        self.touched = time.monotonic()
        self.frames: dict[str, tuple[float, dict]] = {}

    async def send(self, method: str, params: dict | None = None) -> dict:
        return await cdp._request_cdp(self.ws, method, params or {}, timeout_seconds=8)

    async def state(self) -> dict:
        result = await self.send('Runtime.evaluate', {
            'expression': 'JSON.stringify({url:location.href,width:innerWidth,height:innerHeight,epoch:performance.timeOrigin})',
            'returnByValue': True,
        })
        import json
        return json.loads(result['result']['value'])

    async def frame(self) -> dict:
        before = await self.state()
        image = await self.send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 65, 'captureBeyondViewport': False})
        after = await self.state()
        if before != after:
            raise ValueError('page_changed_retry')
        frame_id = uuid.uuid4().hex
        now = time.monotonic()
        self.frames = {k: v for k, v in self.frames.items() if now - v[0] < 5}
        self.frames[frame_id] = (now, after)
        return {'type': 'frame', 'frame': image['data'], 'media_type': 'image/jpeg',
                'frame_id': frame_id, 'target_id': self.target_id, **after}

    async def control(self, params: dict) -> dict:
        old = self.frames.get(str(params.get('frame_id') or ''))
        if not old or time.monotonic() - old[0] > 5 or old[1] != await self.state():
            raise ValueError('stale_frame_retry')
        action = params.get('action')
        state = old[1]
        if action in {'click', 'scroll'}:
            x, y = float(params.get('x', 0)), float(params.get('y', 0))
            if not all(math.isfinite(v) for v in (x, y)) or not (0 <= x < state['width'] and 0 <= y < state['height']):
                raise ValueError('invalid_coordinate')
            if action == 'click':
                for kind in ('mousePressed', 'mouseReleased'):
                    await self.send('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
            else:
                delta = float(params.get('deltaY', 0))
                if not math.isfinite(delta):
                    raise ValueError('invalid_scroll')
                await self.send('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': x, 'y': y, 'deltaX': 0, 'deltaY': max(-800, min(800, delta))})
        elif action == 'type':
            text = str(params.get('text') or '')
            if not text or len(text) > 10000:
                raise ValueError('invalid_text_length')
            focused = await self.send('Runtime.evaluate', {'expression': "(() => {const e=document.activeElement;return !!e && (e.matches('input,textarea') || e.isContentEditable)})()", 'returnByValue': True})
            if focused.get('result', {}).get('value') is not True:
                raise ValueError('focus_input_first')
            if params.get('replace', True):
                await self.send('Input.dispatchKeyEvent', {'type': 'keyDown', 'key': 'a', 'code': 'KeyA', 'modifiers': 2})
                await self.send('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'a', 'code': 'KeyA', 'modifiers': 2})
            await self.send('Input.insertText', {'text': text})
        elif action == 'press':
            key = str(params.get('key') or '')
            codes = {'Enter': 13, 'Tab': 9, 'Escape': 27, 'Backspace': 8, 'ArrowUp': 38, 'ArrowDown': 40}
            if key not in codes:
                raise ValueError('unsupported_key')
            for kind in ('keyDown', 'keyUp'):
                await self.send('Input.dispatchKeyEvent', {'type': kind, 'key': key, 'code': key, 'windowsVirtualKeyCode': codes[key], **({'text': '\r'} if key == 'Enter' and kind == 'keyDown' else {})})
        else:
            raise ValueError('unsupported_control_action')
        return {'action': action, 'target_id': self.target_id}


async def _open(params: dict) -> dict:
    import websockets
    port, work_key = int(params['port']), str(params['work_key'])
    session = cdp.CDPSessionManager.get_session(work_key)
    if not work_key.startswith('chat-pc-') or not session or session.port != port:
        raise ValueError('tab_profile_mismatch')
    async with _OPEN_LOCK:
        for token, tab in list(_SESSIONS.items()):
            if time.monotonic() - tab.touched > 60:
                _SESSIONS.pop(token, None)
                with contextlib.suppress(Exception):
                    await tab.ws.close()
            elif tab.port == port:
                raise ValueError('tab_already_in_use')
        pages = [p for p in await cdp._list_cdp_targets(port) if p.get('type') == 'page']
        target_id = str(params.get('target_id') or '')
        if target_id:
            pages = [p for p in pages if p.get('id') == target_id]
        if len(pages) != 1:
            raise ValueError('explicit_tab_required')
        page = pages[0]
        endpoint = urlparse(page['webSocketDebuggerUrl'])
        if endpoint.scheme != 'ws' or endpoint.hostname not in {'localhost', '127.0.0.1', '::1'} or endpoint.port != port:
            raise ValueError('invalid_tab_endpoint')
        ws = await cdp._connect_cdp_ws(websockets, page['webSocketDebuggerUrl'], open_timeout=8)
        tab = Tab(port, work_key, page['id'], ws)
        try:
            url = str(params.get('url') or '')
            if url:
                validate_url(url)
                await tab.send('Page.navigate', {'url': url})
            token = uuid.uuid4().hex
            _SESSIONS[token] = tab
            return {'tab_token': token, 'target_id': tab.target_id}
        except BaseException:
            await ws.close()
            raise


async def execute(params: dict) -> dict:
    try:
        op = params.get('op')
        if op == 'open':
            data = await _open(params)
        else:
            token = str(params.get('tab_token') or '')
            tab = _SESSIONS.get(token)
            if not tab or tab.port != int(params['port']) or tab.work_key != params['work_key']:
                raise ValueError('tab_binding_expired')
            async with tab.lock:
                if _SESSIONS.get(token) is not tab or (op != 'close' and time.monotonic() - tab.touched > 60):
                    raise ValueError('tab_binding_expired')
                tab.touched = time.monotonic()
                if op == 'close':
                    _SESSIONS.pop(token, None)
                    await tab.ws.close()
                    data = {'closed': True}
                elif op == 'frame':
                    data = await tab.frame()
                elif op == 'control':
                    data = await tab.control(params)
                else:
                    raise ValueError('unsupported_tab_operation')
        return {'status': 'success', 'data': data}
    except Exception as exc:
        # Never include CDP exception payloads: they may contain typed credentials.
        message = str(exc) if isinstance(exc, ValueError) else 'tab_connection_lost'
        return {'status': 'error', 'data': {'error': message, 'error_code': 'PC_TAB_ERROR'}}
