"""
AADS-195: Windows ctypes 기반 키보드/마우스/클립보드 헬퍼.
pyautogui 의존성 제거 — EXE 환경에서도 항상 동작.
Python 표준 라이브러리(ctypes)만 사용.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ── 상수 ──────────────────────────────────────────────────────────────
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_ALT = 0x12
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_BACK = 0x08
VK_DELETE = 0x2E
VK_HOME = 0x24
VK_END = 0x23
VK_UP = 0x26
VK_DOWN = 0x28
VK_LEFT = 0x25
VK_RIGHT = 0x27

# 가상키 매핑 (pyautogui 호환)
_VK_MAP = {
    "ctrl": VK_CONTROL, "control": VK_CONTROL,
    "shift": VK_SHIFT, "alt": VK_ALT,
    "enter": VK_RETURN, "return": VK_RETURN,
    "tab": VK_TAB, "escape": VK_ESCAPE, "esc": VK_ESCAPE,
    "backspace": VK_BACK, "delete": VK_DELETE,
    "home": VK_HOME, "end": VK_END,
    "up": VK_UP, "down": VK_DOWN, "left": VK_LEFT, "right": VK_RIGHT,
    "space": 0x20,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}
# a-z, 0-9
for c in range(ord('a'), ord('z') + 1):
    _VK_MAP[chr(c)] = ctypes.windll.user32.VkKeyScanW(c) & 0xFF
for c in range(ord('0'), ord('9') + 1):
    _VK_MAP[chr(c)] = c


# ── SendInput 구조체 ──────────────────────────────────────────────────
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", INPUT_UNION)]


def _send_input(*inputs: INPUT):
    """SendInput API 호출."""
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


# ── 키보드 ────────────────────────────────────────────────────────────
def _make_key_input(vk: int, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk
    inp.union.ki.dwFlags = flags
    return inp


def key_down(key: str):
    """키 누르기."""
    vk = _VK_MAP.get(key.lower())
    if vk is None:
        raise ValueError(f"알 수 없는 키: {key}")
    _send_input(_make_key_input(vk))


def key_up(key: str):
    """키 떼기."""
    vk = _VK_MAP.get(key.lower())
    if vk is None:
        raise ValueError(f"알 수 없는 키: {key}")
    _send_input(_make_key_input(vk, KEYEVENTF_KEYUP))


def press_key(key: str, interval: float = 0.05):
    """키 누르고 떼기."""
    key_down(key)
    time.sleep(interval)
    key_up(key)


def hotkey(*keys: str, interval: float = 0.05):
    """핫키 조합 (예: hotkey('ctrl', 'f'))."""
    for k in keys:
        key_down(k)
        time.sleep(interval)
    for k in reversed(keys):
        key_up(k)
        time.sleep(interval)


# ── 클립보드 ──────────────────────────────────────────────────────────
CF_UNICODETEXT = 13

def clipboard_set(text: str) -> bool:
    """클립보드에 텍스트 설정 (ctypes — pyperclip 불필요)."""
    try:
        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
            ptr = kernel32.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(CF_UNICODETEXT, h)
            return True
        finally:
            user32.CloseClipboard()
    except Exception as e:
        logger.error("clipboard_set_error: %s", e)
        return False


def clipboard_get() -> Optional[str]:
    """클립보드 텍스트 가져오기."""
    try:
        if not user32.OpenClipboard(0):
            return None
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception as e:
        logger.error("clipboard_get_error: %s", e)
        return None


# ── 마우스 ────────────────────────────────────────────────────────────
def mouse_click(x: int, y: int, button: str = "left"):
    """마우스 클릭 (절대좌표)."""
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    if button == "left":
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# ── 복합 동작 ─────────────────────────────────────────────────────────
def type_text_via_clipboard(text: str):
    """클립보드를 통해 텍스트 입력 (한글 지원)."""
    clipboard_set(text)
    time.sleep(0.1)
    hotkey("ctrl", "v")
    time.sleep(0.1)
