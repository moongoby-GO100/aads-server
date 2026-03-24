"""AADS-195: 마우스/키보드 입력 제어."""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def mouse_click(params: Dict[str, Any]) -> Dict[str, Any]:
    """마우스 클릭. params: x, y, button(left/right/middle), clicks(1/2)"""
    try:
        import pyautogui
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return {"status": "error", "data": {"error": "x, y 좌표 필수"}}
        button = params.get("button", "left")
        clicks = params.get("clicks", 1)
        pyautogui.click(x=int(x), y=int(y), button=button, clicks=int(clicks))
        return {"status": "success", "data": {"x": int(x), "y": int(y), "button": button, "clicks": int(clicks)}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyautogui 미설치. pip install pyautogui"}}
    except Exception as e:
        logger.error("mouse_click error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def mouse_move(params: Dict[str, Any]) -> Dict[str, Any]:
    """마우스 이동. params: x, y, duration(초, 선택)"""
    try:
        import pyautogui
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return {"status": "error", "data": {"error": "x, y 좌표 필수"}}
        duration = float(params.get("duration", 0.3))
        pyautogui.moveTo(x=int(x), y=int(y), duration=duration)
        return {"status": "success", "data": {"x": int(x), "y": int(y), "duration": duration}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyautogui 미설치. pip install pyautogui"}}
    except Exception as e:
        logger.error("mouse_move error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def mouse_scroll(params: Dict[str, Any]) -> Dict[str, Any]:
    """마우스 스크롤. params: clicks(양수=위, 음수=아래), x, y(선택)"""
    try:
        import pyautogui
        scroll_clicks = params.get("clicks", 0)
        if not scroll_clicks:
            return {"status": "error", "data": {"error": "clicks 파라미터 필수 (양수=위, 음수=아래)"}}
        x = params.get("x")
        y = params.get("y")
        if x is not None and y is not None:
            pyautogui.scroll(int(scroll_clicks), x=int(x), y=int(y))
        else:
            pyautogui.scroll(int(scroll_clicks))
        return {"status": "success", "data": {"clicks": int(scroll_clicks)}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyautogui 미설치. pip install pyautogui"}}
    except Exception as e:
        logger.error("mouse_scroll error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def mouse_drag(params: Dict[str, Any]) -> Dict[str, Any]:
    """마우스 드래그. params: start_x, start_y, end_x, end_y, button(left/right), duration"""
    try:
        import pyautogui
        sx = params.get("start_x")
        sy = params.get("start_y")
        ex = params.get("end_x")
        ey = params.get("end_y")
        if None in (sx, sy, ex, ey):
            return {"status": "error", "data": {"error": "start_x, start_y, end_x, end_y 필수"}}
        button = params.get("button", "left")
        duration = float(params.get("duration", 0.5))
        pyautogui.moveTo(int(sx), int(sy))
        pyautogui.drag(int(ex) - int(sx), int(ey) - int(sy), duration=duration, button=button)
        return {"status": "success", "data": {"start": [int(sx), int(sy)], "end": [int(ex), int(ey)]}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyautogui 미설치. pip install pyautogui"}}
    except Exception as e:
        logger.error("mouse_drag error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def keyboard_type(params: Dict[str, Any]) -> Dict[str, Any]:
    """텍스트 입력 (한글 포함). params: text, interval(초, 선택)"""
    try:
        import pyautogui
        import pyperclip
        text = params.get("text", "")
        if not text:
            return {"status": "error", "data": {"error": "text 파라미터 필수"}}
        has_non_ascii = any(ord(c) > 127 for c in text)
        if has_non_ascii:
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
        else:
            interval = float(params.get("interval", 0.02))
            pyautogui.write(text, interval=interval)
        return {"status": "success", "data": {"text": text, "length": len(text)}}
    except ImportError as ie:
        return {"status": "error", "data": {"error": f"필요 라이브러리 미설치: {ie}"}}
    except Exception as e:
        logger.error("keyboard_type error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def keyboard_hotkey(params: Dict[str, Any]) -> Dict[str, Any]:
    """단축키 입력. params: keys (리스트, 예: ["ctrl", "c"])"""
    try:
        import pyautogui
        keys = params.get("keys", [])
        if not keys:
            return {"status": "error", "data": {"error": "keys 파라미터 필수 (예: ['ctrl', 'c'])"}}
        pyautogui.hotkey(*keys)
        return {"status": "success", "data": {"keys": keys}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyautogui 미설치. pip install pyautogui"}}
    except Exception as e:
        logger.error("keyboard_hotkey error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def keyboard_press(params: Dict[str, Any]) -> Dict[str, Any]:
    """단일 키 입력. params: key (예: enter, esc, tab, space, f5 등), presses(횟수)"""
    try:
        import pyautogui
        key = params.get("key", "")
        if not key:
            return {"status": "error", "data": {"error": "key 파라미터 필수"}}
        presses = int(params.get("presses", 1))
        pyautogui.press(key, presses=presses)
        return {"status": "success", "data": {"key": key, "presses": presses}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyautogui 미설치. pip install pyautogui"}}
    except Exception as e:
        logger.error("keyboard_press error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
