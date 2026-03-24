"""AADS-195: 윈도우 관리 + 클립보드 + 앱 실행 (P0)."""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def window_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """열린 창 목록 반환."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        windows = []

        def enum_callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    windows.append({
                        "title": buf.value,
                        "hwnd": hwnd,
                        "x": rect.left,
                        "y": rect.top,
                        "width": rect.right - rect.left,
                        "height": rect.bottom - rect.top,
                        "is_active": hwnd == user32.GetForegroundWindow(),
                    })
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        return {"status": "success", "data": {"windows": windows, "count": len(windows)}}
    except Exception as e:
        logger.error("window_list error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def window_focus(params: Dict[str, Any]) -> Dict[str, Any]:
    """창 활성화/최소화/최대화/복원/닫기."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        title = params.get("title")
        hwnd = params.get("hwnd")
        action = params.get("action", "focus")

        if hwnd:
            target_hwnd = int(hwnd)
        elif title:
            target_hwnd = None

            def enum_callback(h, _):
                nonlocal target_hwnd
                length = user32.GetWindowTextLengthW(h)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(h, buf, length + 1)
                    if title.lower() in buf.value.lower():
                        target_hwnd = h
                        return False
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDENUMPROC(enum_callback), 0)

            if target_hwnd is None:
                return {"status": "error", "data": {"error": f"'{title}' 창을 찾을 수 없음"}}
        else:
            return {"status": "error", "data": {"error": "title 또는 hwnd 필수"}}

        SW_MINIMIZE = 6
        SW_MAXIMIZE = 3
        SW_RESTORE = 9

        if action == "focus":
            user32.ShowWindow(target_hwnd, SW_RESTORE)
            user32.SetForegroundWindow(target_hwnd)
        elif action == "minimize":
            user32.ShowWindow(target_hwnd, SW_MINIMIZE)
        elif action == "maximize":
            user32.ShowWindow(target_hwnd, SW_MAXIMIZE)
        elif action == "restore":
            user32.ShowWindow(target_hwnd, SW_RESTORE)
        elif action == "close":
            WM_CLOSE = 0x0010
            user32.PostMessageW(target_hwnd, WM_CLOSE, 0, 0)
        else:
            return {"status": "error", "data": {"error": f"지원하지 않는 action: {action}"}}

        return {"status": "success", "data": {"hwnd": target_hwnd, "action": action}}
    except Exception as e:
        logger.error("window_focus error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def clipboard_get(params: Dict[str, Any]) -> Dict[str, Any]:
    """클립보드 텍스트 읽기."""
    try:
        import pyperclip
        text = pyperclip.paste()
        return {"status": "success", "data": {"text": text, "length": len(text)}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyperclip 미설치. pip install pyperclip"}}
    except Exception as e:
        logger.error("clipboard_get error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def clipboard_set(params: Dict[str, Any]) -> Dict[str, Any]:
    """클립보드에 텍스트 설정."""
    try:
        import pyperclip
        text = params.get("text", "")
        if not text:
            return {"status": "error", "data": {"error": "text 파라미터 필수"}}
        pyperclip.copy(text)
        return {"status": "success", "data": {"text": text, "length": len(text)}}
    except ImportError:
        return {"status": "error", "data": {"error": "pyperclip 미설치. pip install pyperclip"}}
    except Exception as e:
        logger.error("clipboard_set error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def app_launch(params: Dict[str, Any]) -> Dict[str, Any]:
    """프로그램 실행."""
    try:
        path = params.get("path")
        name = params.get("name")

        if path:
            if not os.path.exists(path):
                return {"status": "error", "data": {"error": f"경로 없음: {path}"}}
            os.startfile(path)
            return {"status": "success", "data": {"launched": path}}

        if name:
            # 시작 메뉴에서 .lnk 파일 검색
            search_dirs = [
                os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"),
                             "Microsoft", "Windows", "Start Menu", "Programs"),
                os.path.join(os.environ.get("APPDATA", ""),
                             "Microsoft", "Windows", "Start Menu", "Programs"),
            ]
            name_lower = name.lower()
            for search_dir in search_dirs:
                if not os.path.exists(search_dir):
                    continue
                for root, dirs, files in os.walk(search_dir):
                    for f in files:
                        if name_lower in f.lower() and f.endswith(".lnk"):
                            full = os.path.join(root, f)
                            os.startfile(full)
                            return {"status": "success", "data": {"launched": full, "matched": f}}

            # shell로 시도
            try:
                subprocess.Popen(name, shell=True)
                return {"status": "success", "data": {"launched": name, "method": "shell"}}
            except Exception:
                return {"status": "error", "data": {"error": f"'{name}' 프로그램을 찾을 수 없음"}}

        return {"status": "error", "data": {"error": "path 또는 name 파라미터 필수"}}
    except Exception as e:
        logger.error("app_launch error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
