"""AADS: CDP 브라우저 자동화 — Chrome DevTools Protocol via WebSocket."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import urllib.request
import uuid
from typing import Any, Dict

logger = logging.getLogger(__name__)

CDP_HOST = "localhost"
CDP_PORT = 9222
_ACTIVE_CDP_PORT = CDP_PORT
_MSG_ID = 0


def _next_id() -> int:
    """CDP 메시지 ID 순차 생성."""
    global _MSG_ID
    _MSG_ID += 1
    return _MSG_ID


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_port(value: Any, default: int = CDP_PORT) -> int:
    try:
        port = int(value)
    except Exception:
        return default
    if 1 <= port <= 65535:
        return port
    return default


def _effective_port(params: Dict[str, Any] | None = None) -> int:
    if isinstance(params, dict) and "port" in params:
        return _coerce_port(params.get("port"), _ACTIVE_CDP_PORT)
    return _ACTIVE_CDP_PORT


async def _http_get_json(port: int, path: str, timeout: float = 2.0) -> Any:
    def _fetch() -> Any:
        url = f"http://{CDP_HOST}:{port}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="ignore"))

    return await asyncio.to_thread(_fetch)


async def _probe_cdp_version(port: int) -> Dict[str, Any] | None:
    try:
        payload = await _http_get_json(port, "/json/version", timeout=1.5)
        if isinstance(payload, dict) and payload.get("webSocketDebuggerUrl"):
            return payload
    except Exception:
        return None
    return None


async def _list_cdp_targets(port: int) -> list[dict[str, Any]]:
    payload = await _http_get_json(port, "/json", timeout=2.0)
    if not isinstance(payload, list):
        raise ConnectionError(f"CDP /json 응답이 list가 아닙니다 (port={port})")
    return payload


async def _is_port_open(port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection(CDP_HOST, port)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((CDP_HOST, 0))
        return int(sock.getsockname()[1])


async def _wait_cdp_ready(port: int, timeout_seconds: float) -> Dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.1)
    while asyncio.get_running_loop().time() < deadline:
        info = await _probe_cdp_version(port)
        if info is not None:
            return info
        await asyncio.sleep(0.3)
    return None


def _default_profile_root() -> str:
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "KakaoBot",
            "cdp-profile",
        )
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), ".kakaobot-cdp-profile")
    return os.path.join(os.path.expanduser("~"), ".kakaobot-cdp-profile")


def _resolve_profile_dir(params: Dict[str, Any]) -> str:
    explicit = str(params.get("user_data_dir", "") or "").strip()
    if explicit:
        return explicit
    root = _default_profile_root()
    if not _as_bool(params.get("isolated_profile", False), default=False):
        return root
    isolation_id = str(params.get("isolation_id", "") or "").strip() or uuid.uuid4().hex[:8]
    return os.path.join(root, f"isolated-{isolation_id}")


def _candidate_ports(params: Dict[str, Any]) -> list[int]:
    preferred = _coerce_port(params.get("preferred_port", params.get("port", CDP_PORT)), CDP_PORT)
    candidates: list[int] = [preferred]

    raw_candidates = params.get("port_candidates")
    if isinstance(raw_candidates, list):
        for value in raw_candidates:
            port = _coerce_port(value, 0)
            if port and port not in candidates:
                candidates.append(port)

    if _as_bool(params.get("dynamic_port", False), default=False):
        for fallback in (9222, 9333, 9444, 9555, 9666, 9777):
            if fallback not in candidates:
                candidates.append(fallback)
        random_free = _find_free_port()
        if random_free not in candidates:
            candidates.append(random_free)

    return candidates


async def _get_ws_url(target_idx: int = 0, port: int | None = None) -> str:
    """Chrome 디버그 WS URL 획득 (/json/version 또는 /json)."""
    resolved_port = int(port or _ACTIVE_CDP_PORT)
    version = await _probe_cdp_version(resolved_port)
    if version is None:
        raise ConnectionError(
            f"CDP_NOT_READY: http://{CDP_HOST}:{resolved_port}/json/version 응답 없음"
        )
    ws_url = str(version.get("webSocketDebuggerUrl", "") or "").strip()

    try:
        targets = await _list_cdp_targets(resolved_port)
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            pages = [t for t in targets if t.get("webSocketDebuggerUrl")]
        if pages:
            if target_idx >= len(pages):
                target_idx = 0
            ws_url = str(pages[target_idx].get("webSocketDebuggerUrl") or ws_url)
    except Exception:
        pass

    if not ws_url:
        raise ConnectionError(
            f"CDP_NOT_READY: webSocketDebuggerUrl 누락 (port={resolved_port})"
        )
    return ws_url


async def _send_cdp(ws_url: str, method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """CDP 명령 전송 및 결과 수신."""
    import websockets

    msg_id = _next_id()
    payload: Dict[str, Any] = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps(payload))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if resp.get("id") == msg_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP 오류: {resp['error']}")
                return resp.get("result", {})


async def _send_cdp_multi(ws_url: str, commands: list[tuple[str, Dict[str, Any] | None]]) -> list[Dict[str, Any]]:
    """여러 CDP 명령을 하나의 WS 연결로 순차 실행."""
    import websockets

    results = []
    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        for method, params in commands:
            msg_id = _next_id()
            payload: Dict[str, Any] = {"id": msg_id, "method": method}
            if params:
                payload["params"] = params
            await ws.send(json.dumps(payload))
            while True:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if resp.get("id") == msg_id:
                    if "error" in resp:
                        raise RuntimeError(f"CDP 오류: {resp['error']}")
                    results.append(resp.get("result", {}))
                    break
    return results


def _chrome_not_running_error(port: int) -> Dict[str, Any]:
    return {
        "status": "error",
        "data": {
            "error": f"Chrome이 CDP 모드로 준비되지 않았습니다 (port {port})",
            "error_code": "CDP_NOT_READY",
            "hint": "browser_launch 명령으로 Chrome을 시작하세요",
            "port": port,
        },
    }


# ── 커맨드 핸들러 ─────────────────────────────────────────────────────────

async def browser_navigate(params: Dict[str, Any]) -> Dict[str, Any]:
    """URL 이동. params: url(필수)"""
    url = params.get("url", "")
    if not url:
        return {"status": "error", "data": {"error": "url 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        result = await _send_cdp(ws_url, "Page.navigate", {"url": url})
        logger.info("브라우저 이동: %s", url)
        return {"status": "success", "data": {"url": url, "frameId": result.get("frameId", "")}}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_click(params: Dict[str, Any]) -> Dict[str, Any]:
    """CSS 셀렉터 클릭. params: selector(필수)"""
    selector = params.get("selector", "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        # querySelector로 노드 찾기 → 좌표 계산 → 클릭
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            var rect = el.getBoundingClientRect();
            var x = rect.left + rect.width / 2;
            var y = rect.top + rect.height / 2;
            el.click();
            return JSON.stringify({{"x": x, "y": y, "clicked": true}});
        }})()
        """
        result = await _send_cdp(ws_url, "Runtime.evaluate", {"expression": js, "returnByValue": True})
        value = result.get("result", {}).get("value", "{}")
        data = json.loads(value) if isinstance(value, str) else value
        if "error" in data:
            return {"status": "error", "data": data}
        logger.info("브라우저 클릭: %s", selector)
        return {"status": "success", "data": data}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_fill(params: Dict[str, Any]) -> Dict[str, Any]:
    """입력 필드에 텍스트 입력. params: selector(필수), value(필수)"""
    selector = params.get("selector", "")
    value = params.get("value", "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            el.focus();
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{"filled": true, "selector": {json.dumps(selector)}}});
        }})()
        """
        result = await _send_cdp(ws_url, "Runtime.evaluate", {"expression": js, "returnByValue": True})
        res_value = result.get("result", {}).get("value", "{}")
        data = json.loads(res_value) if isinstance(res_value, str) else res_value
        if "error" in data:
            return {"status": "error", "data": data}
        logger.info("브라우저 입력: %s", selector)
        return {"status": "success", "data": data}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_press_key(params: Dict[str, Any]) -> Dict[str, Any]:
    """키 입력. params: key(필수), selector(선택)."""
    key = str(params.get("key", "") or "")
    selector = str(params.get("selector", "") or "")
    if not key:
        return {"status": "error", "data": {"error": "key 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        if selector:
            focus_js = f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
                el.focus();
                return JSON.stringify({{"focused": true}});
            }})()
            """
            focus = await _send_cdp(ws_url, "Runtime.evaluate", {"expression": focus_js, "returnByValue": True})
            value = focus.get("result", {}).get("value", "{}")
            data = json.loads(value) if isinstance(value, str) else value
            if isinstance(data, dict) and data.get("error"):
                return {"status": "error", "data": data}

        if len(key) == 1:
            await _send_cdp(ws_url, "Input.insertText", {"text": key})
        else:
            await _send_cdp(ws_url, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
            await _send_cdp(ws_url, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        return {"status": "success", "data": {"key": key, "selector": selector}}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_select_option(params: Dict[str, Any]) -> Dict[str, Any]:
    """select 옵션 선택. params: selector(필수), value(필수: 문자열 또는 목록)."""
    selector = str(params.get("selector", "") or "")
    value = params.get("value")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}
    if value is None:
        return {"status": "error", "data": {"error": "value 파라미터가 필요합니다"}}

    values = value if isinstance(value, list) else [value]
    values = [str(v) for v in values]
    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            if (el.tagName.toLowerCase() !== 'select') return JSON.stringify({{"error": "select 요소가 아닙니다: " + {json.dumps(selector)}}});
            var wanted = new Set({json.dumps(values)});
            var matched = [];
            for (var option of el.options) {{
                var ok = wanted.has(option.value) || wanted.has((option.textContent || '').trim());
                if (el.multiple) option.selected = ok;
                else if (ok) el.value = option.value;
                if (ok) matched.push(option.value);
            }}
            if (!matched.length) return JSON.stringify({{"error": "일치하는 옵션이 없습니다", "value": {json.dumps(values)}}});
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{"selected": matched, "selector": {json.dumps(selector)}}});
        }})()
        """
        result = await _send_cdp(ws_url, "Runtime.evaluate", {"expression": js, "returnByValue": True})
        res_value = result.get("result", {}).get("value", "{}")
        data = json.loads(res_value) if isinstance(res_value, str) else res_value
        if "error" in data:
            return {"status": "error", "data": data}
        return {"status": "success", "data": data}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_check(params: Dict[str, Any]) -> Dict[str, Any]:
    """체크박스/라디오 상태 설정. params: selector(필수), checked(기본 true)."""
    selector = str(params.get("selector", "") or "")
    checked = _as_bool(params.get("checked", True), default=True)
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        js = f"""
        (function() {{
            var el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다: " + {json.dumps(selector)}}});
            if (!('checked' in el)) return JSON.stringify({{"error": "checked 속성이 없는 요소입니다: " + {json.dumps(selector)}}});
            var desired = {json.dumps(checked)};
            if (Boolean(el.checked) !== desired) el.click();
            el.checked = desired;
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{"selector": {json.dumps(selector)}, "checked": Boolean(el.checked)}});
        }})()
        """
        result = await _send_cdp(ws_url, "Runtime.evaluate", {"expression": js, "returnByValue": True})
        res_value = result.get("result", {}).get("value", "{}")
        data = json.loads(res_value) if isinstance(res_value, str) else res_value
        if "error" in data:
            return {"status": "error", "data": data}
        return {"status": "success", "data": data}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_file_upload(params: Dict[str, Any]) -> Dict[str, Any]:
    """file input에 PC 로컬 파일 지정. params: selector(필수), file_paths 또는 file_path."""
    selector = str(params.get("selector", "") or "")
    raw_paths = params.get("file_paths", params.get("file_path", ""))
    file_paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
    file_paths = [os.path.abspath(os.path.expanduser(str(p))) for p in file_paths if str(p or "").strip()]
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}
    if not file_paths:
        return {"status": "error", "data": {"error": "file_paths 파라미터가 필요합니다"}}
    missing = [p for p in file_paths if not os.path.isfile(p)]
    if missing:
        return {"status": "error", "data": {"error": "파일을 찾을 수 없습니다", "missing": missing}}

    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        doc = await _send_cdp(ws_url, "DOM.getDocument", {"depth": -1, "pierce": True})
        root_id = doc.get("root", {}).get("nodeId")
        node = await _send_cdp(ws_url, "DOM.querySelector", {"nodeId": root_id, "selector": selector})
        node_id = node.get("nodeId")
        if not node_id:
            return {"status": "error", "data": {"error": f"요소를 찾을 수 없습니다: {selector}"}}
        await _send_cdp(ws_url, "DOM.setFileInputFiles", {"nodeId": node_id, "files": file_paths})
        return {"status": "success", "data": {"selector": selector, "files": file_paths, "count": len(file_paths)}}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_download(params: Dict[str, Any]) -> Dict[str, Any]:
    """다운로드를 유발하는 요소를 클릭하고 PC 다운로드 파일을 감지한다."""
    selector = str(params.get("selector", "") or "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}
    download_dir = str(params.get("download_dir", "") or "").strip()
    if not download_dir:
        download_dir = os.path.join(os.path.expanduser("~"), "AADSDownloads")
    download_dir = os.path.abspath(os.path.expanduser(download_dir))
    timeout_seconds = float(params.get("timeout_seconds", 60) or 60)

    port = _effective_port(params)
    try:
        os.makedirs(download_dir, exist_ok=True)
        before = {name: os.path.getmtime(os.path.join(download_dir, name)) for name in os.listdir(download_dir)}
        version = await _probe_cdp_version(port)
        if version and version.get("webSocketDebuggerUrl"):
            try:
                await _send_cdp(
                    str(version["webSocketDebuggerUrl"]),
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": download_dir, "eventsEnabled": True},
                )
            except Exception as exc:
                logger.warning("download behavior setup failed: %s", exc)

        click_result = await browser_click({**params, "port": port, "selector": selector})
        if click_result.get("status") != "success":
            return click_result

        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 1)
        while asyncio.get_running_loop().time() < deadline:
            candidates = []
            for name in os.listdir(download_dir):
                if name.endswith((".crdownload", ".tmp")):
                    continue
                path = os.path.join(download_dir, name)
                if not os.path.isfile(path):
                    continue
                mtime = os.path.getmtime(path)
                if name not in before or mtime > before.get(name, 0):
                    candidates.append((mtime, path))
            if candidates:
                candidates.sort(reverse=True)
                path = candidates[0][1]
                return {"status": "success", "data": {"path": path, "size": os.path.getsize(path), "download_dir": download_dir}}
            await asyncio.sleep(0.5)
        return {"status": "error", "data": {"error": "다운로드 파일 감지 시간 초과", "download_dir": download_dir}}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_screenshot(params: Dict[str, Any]) -> Dict[str, Any]:
    """브라우저 스크린샷. CDP Page.captureScreenshot → base64."""
    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        fmt = params.get("format", "png")
        quality = params.get("quality", 80)
        cdp_params: Dict[str, Any] = {"format": fmt}
        if fmt == "jpeg":
            cdp_params["quality"] = quality
        result = await _send_cdp(ws_url, "Page.captureScreenshot", cdp_params)
        img_data = result.get("data", "")
        logger.info("브라우저 스크린샷 캡처 (%s)", fmt)
        return {
            "status": "success",
            "data": {"screenshot_base64": img_data, "format": fmt},
        }
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_get_text(params: Dict[str, Any]) -> Dict[str, Any]:
    """페이지 또는 셀렉터 텍스트 추출. params: selector(선택)"""
    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        selector = params.get("selector", "")
        if selector:
            js = f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return JSON.stringify({{"error": "요소를 찾을 수 없습니다"}});
                return JSON.stringify({{"text": el.innerText || el.textContent}});
            }})()
            """
        else:
            js = "JSON.stringify({text: document.body.innerText})"

        result = await _send_cdp(ws_url, "Runtime.evaluate", {"expression": js, "returnByValue": True})
        value = result.get("result", {}).get("value", "{}")
        data = json.loads(value) if isinstance(value, str) else value
        if "error" in data:
            return {"status": "error", "data": data}
        return {"status": "success", "data": data}
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_eval(params: Dict[str, Any]) -> Dict[str, Any]:
    """JavaScript 실행. params: expression(필수). 로컬 PC 전용, 로그 필수."""
    expression = params.get("expression", "")
    if not expression:
        return {"status": "error", "data": {"error": "expression 파라미터가 필요합니다"}}

    logger.info("브라우저 JS 실행: %s", expression[:200])

    port = _effective_port(params)
    try:
        ws_url = await _get_ws_url(port=port)
        result = await _send_cdp(ws_url, "Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        res_data = result.get("result", {})
        if res_data.get("subtype") == "error":
            return {"status": "error", "data": {"error": res_data.get("description", "JS 실행 오류")}}
        return {
            "status": "success",
            "data": {"value": res_data.get("value"), "type": res_data.get("type", "")},
        }
    except ConnectionError:
        return _chrome_not_running_error(port)
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_tabs(params: Dict[str, Any]) -> Dict[str, Any]:
    """열린 탭 목록."""
    port = _effective_port(params)
    try:
        targets = await _list_cdp_targets(port)
        tabs = [
            {"id": t.get("id", ""), "title": t.get("title", ""), "url": t.get("url", ""), "type": t.get("type", "")}
            for t in targets if t.get("type") == "page"
        ]
        return {"status": "success", "data": {"tabs": tabs, "count": len(tabs)}}
    except Exception:
        return _chrome_not_running_error(port)


async def browser_launch(params: Dict[str, Any]) -> Dict[str, Any]:
    """Chrome CDP 전용 세션 시작 (전용 프로필 + 동적 포트 충돌 회피)."""
    global _ACTIVE_CDP_PORT
    url = params.get("url", "about:blank")

    # OS별 Chrome 경로
    if sys.platform == "win32":
        chrome_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        chrome_paths = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        chrome_paths = ["google-chrome", "chromium-browser", "chromium"]

    chrome_exe = params.get("chrome_path", "")
    if not chrome_exe:
        for p in chrome_paths:
            if os.path.isfile(p):
                chrome_exe = p
                break
        if not chrome_exe:
            chrome_exe = chrome_paths[0]  # 기본값 시도

    profile_dir = _resolve_profile_dir(params)
    ports = _candidate_ports(params)
    new_window = _as_bool(params.get("new_window", True), default=True)
    ready_timeout = float(params.get("ready_timeout_seconds", 15.0) or 15.0)

    try:
        os.makedirs(profile_dir, exist_ok=True)

        for port in ports:
            existing = await _probe_cdp_version(port)
            if existing is not None:
                _ACTIVE_CDP_PORT = port
                return {
                    "status": "success",
                    "data": {
                        "message": f"기존 CDP 세션 사용 (port {port})",
                        "port": port,
                        "user_data_dir": profile_dir,
                        "cdp_ready": True,
                        "websocket_debugger_url": existing.get("webSocketDebuggerUrl", ""),
                    },
                }

            if await _is_port_open(port):
                # 열려 있지만 /json/version 미응답이면 다른 포트로 우회
                continue

            cmd = [
                chrome_exe,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if new_window:
                cmd.append("--new-window")
            cmd.append(url)

            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready = await _wait_cdp_ready(port, ready_timeout)
            if ready is None:
                continue

            _ACTIVE_CDP_PORT = port
            logger.info("Chrome CDP 시작 완료 (port=%d profile=%s)", port, profile_dir)
            return {
                "status": "success",
                "data": {
                    "message": f"Chrome CDP 준비 완료 (port {port})",
                    "port": port,
                    "user_data_dir": profile_dir,
                    "cdp_ready": True,
                    "websocket_debugger_url": ready.get("webSocketDebuggerUrl", ""),
                },
            }

        return {
            "status": "error",
            "data": {
                "error": "CDP endpoint 준비 실패 (/json/version 응답 없음)",
                "error_code": "CDP_NOT_READY",
                "port_candidates": ports,
                "hint": "포트를 변경하거나 이미 실행 중인 Chrome 충돌 여부를 확인하세요",
            },
        }
    except FileNotFoundError:
        return {
            "status": "error",
            "data": {
                "error": f"Chrome을 찾을 수 없습니다: {chrome_exe}",
                "hint": "chrome_path 파라미터로 Chrome 경로를 지정하거나 Chrome을 설치하세요",
            },
        }
    except Exception as e:
        return {"status": "error", "data": {"error": str(e), "error_code": "CDP_NOT_READY"}}
