"""AADS: CDP 브라우저 자동화 — Chrome DevTools Protocol via WebSocket."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict

logger = logging.getLogger(__name__)

CDP_HOST = "localhost"
CDP_PORT = 9222
_MSG_ID = 0


def _next_id() -> int:
    """CDP 메시지 ID 순차 생성."""
    global _MSG_ID
    _MSG_ID += 1
    return _MSG_ID


async def _get_ws_url(target_idx: int = 0) -> str:
    """Chrome 디버그 WS URL 획득 (/json/version 또는 /json)."""
    try:
        import aiohttp
    except ImportError:
        # aiohttp 없으면 websockets + http 직접 사용
        pass

    url = f"http://{CDP_HOST}:{CDP_PORT}/json"
    try:
        # asyncio로 HTTP GET
        reader, writer = await asyncio.open_connection(CDP_HOST, CDP_PORT)
        request = f"GET /json HTTP/1.1\r\nHost: {CDP_HOST}:{CDP_PORT}\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()

        response = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response:
                # 헤더와 바디 분리
                header, body = response.split(b"\r\n\r\n", 1)
                # chunked 또는 content-length 처리
                if b"Transfer-Encoding: chunked" in header:
                    # 간단한 chunked 파싱 — 첫 번째 청크만
                    if b"\r\n" in body:
                        size_str, rest = body.split(b"\r\n", 1)
                        try:
                            size = int(size_str, 16)
                            if len(rest) >= size:
                                body = rest[:size]
                                break
                        except ValueError:
                            break
                else:
                    for line in header.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            cl = int(line.split(b":")[1].strip())
                            if len(body) >= cl:
                                body = body[:cl]
                                break
                    else:
                        # 데이터 충분히 수신될 때까지 대기
                        continue
                    break

        writer.close()
        targets = json.loads(body)
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            pages = targets
        if target_idx >= len(pages):
            target_idx = 0
        return pages[target_idx]["webSocketDebuggerUrl"]

    except Exception as e:
        raise ConnectionError(
            f"Chrome CDP 연결 실패 (http://{CDP_HOST}:{CDP_PORT}/json). "
            f"Chrome이 --remote-debugging-port={CDP_PORT}로 실행 중인지 확인하세요. "
            f"시작: browser_launch 명령 사용. 오류: {e}"
        ) from e


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


def _chrome_not_running_error() -> Dict[str, Any]:
    return {
        "status": "error",
        "data": {
            "error": f"Chrome이 CDP 모드로 실행 중이 아닙니다 (port {CDP_PORT})",
            "hint": "browser_launch 명령으로 Chrome을 시작하세요",
        },
    }


# ── 커맨드 핸들러 ─────────────────────────────────────────────────────────

async def browser_navigate(params: Dict[str, Any]) -> Dict[str, Any]:
    """URL 이동. params: url(필수)"""
    url = params.get("url", "")
    if not url:
        return {"status": "error", "data": {"error": "url 파라미터가 필요합니다"}}

    try:
        ws_url = await _get_ws_url()
        result = await _send_cdp(ws_url, "Page.navigate", {"url": url})
        logger.info("브라우저 이동: %s", url)
        return {"status": "success", "data": {"url": url, "frameId": result.get("frameId", "")}}
    except ConnectionError:
        return _chrome_not_running_error()
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_click(params: Dict[str, Any]) -> Dict[str, Any]:
    """CSS 셀렉터 클릭. params: selector(필수)"""
    selector = params.get("selector", "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    try:
        ws_url = await _get_ws_url()
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
        return _chrome_not_running_error()
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_fill(params: Dict[str, Any]) -> Dict[str, Any]:
    """입력 필드에 텍스트 입력. params: selector(필수), value(필수)"""
    selector = params.get("selector", "")
    value = params.get("value", "")
    if not selector:
        return {"status": "error", "data": {"error": "selector 파라미터가 필요합니다"}}

    try:
        ws_url = await _get_ws_url()
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
        return _chrome_not_running_error()
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_screenshot(params: Dict[str, Any]) -> Dict[str, Any]:
    """브라우저 스크린샷. CDP Page.captureScreenshot → base64."""
    try:
        ws_url = await _get_ws_url()
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
        return _chrome_not_running_error()
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_get_text(params: Dict[str, Any]) -> Dict[str, Any]:
    """페이지 또는 셀렉터 텍스트 추출. params: selector(선택)"""
    try:
        ws_url = await _get_ws_url()
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
        return _chrome_not_running_error()
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_eval(params: Dict[str, Any]) -> Dict[str, Any]:
    """JavaScript 실행. params: expression(필수). 로컬 PC 전용, 로그 필수."""
    expression = params.get("expression", "")
    if not expression:
        return {"status": "error", "data": {"error": "expression 파라미터가 필요합니다"}}

    logger.info("브라우저 JS 실행: %s", expression[:200])

    try:
        ws_url = await _get_ws_url()
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
        return _chrome_not_running_error()
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def browser_tabs(params: Dict[str, Any]) -> Dict[str, Any]:
    """열린 탭 목록."""
    try:
        # /json 엔드포인트에서 전체 탭 목록 조회
        reader, writer = await asyncio.open_connection(CDP_HOST, CDP_PORT)
        request = f"GET /json HTTP/1.1\r\nHost: {CDP_HOST}:{CDP_PORT}\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()

        response = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response and len(response) > 100:
                break
        writer.close()

        body = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else response
        # chunked 디코딩
        if b"\r\n" in body:
            try:
                size = int(body.split(b"\r\n", 1)[0], 16)
                body = body.split(b"\r\n", 1)[1][:size]
            except ValueError:
                pass
        targets = json.loads(body)
        tabs = [
            {"id": t.get("id", ""), "title": t.get("title", ""), "url": t.get("url", ""), "type": t.get("type", "")}
            for t in targets if t.get("type") == "page"
        ]
        return {"status": "success", "data": {"tabs": tabs, "count": len(tabs)}}
    except Exception:
        return _chrome_not_running_error()


async def browser_launch(params: Dict[str, Any]) -> Dict[str, Any]:
    """Chrome을 --remote-debugging-port=9222로 시작."""
    port = params.get("port", CDP_PORT)
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

    try:
        # 이미 실행 중인지 확인
        try:
            reader, writer = await asyncio.open_connection(CDP_HOST, port)
            writer.close()
            return {
                "status": "success",
                "data": {"message": f"Chrome이 이미 CDP 포트 {port}에서 실행 중입니다"},
            }
        except (ConnectionRefusedError, OSError):
            pass

        cmd = [
            chrome_exe,
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 잠시 대기 후 연결 확인
        await asyncio.sleep(2)

        try:
            reader, writer = await asyncio.open_connection(CDP_HOST, port)
            writer.close()
            logger.info("Chrome CDP 시작 완료 (포트 %d)", port)
            return {
                "status": "success",
                "data": {"message": f"Chrome 시작 완료 (CDP 포트 {port})", "port": port},
            }
        except (ConnectionRefusedError, OSError):
            return {
                "status": "success",
                "data": {
                    "message": "Chrome 프로세스 시작됨, CDP 연결 대기 중 — 잠시 후 재시도하세요",
                    "port": port,
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
        return {"status": "error", "data": {"error": str(e)}}
