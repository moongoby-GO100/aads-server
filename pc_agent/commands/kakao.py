"""
AADS-195 Phase 3: 카카오톡 GUI 자동 조작.
PyAutoGUI + Win32 API 기반 카카오톡 메시지 전송/읽기.
Windows 전용 — 서버가 아닌 PC Agent 클라이언트에서 실행.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 카카오톡 창 제목 패턴
_KAKAO_WINDOW_TITLE = "카카오톡"
_KAKAO_CLASS_NAME = "EVA_Window_Dblclk"


def _find_kakao_window() -> Optional[int]:
    """카카오톡 메인 창 핸들 찾기."""
    try:
        import win32gui
        hwnd = win32gui.FindWindow(None, _KAKAO_WINDOW_TITLE)
        if hwnd == 0:
            # 클래스명으로 재시도
            hwnd = win32gui.FindWindow(_KAKAO_CLASS_NAME, None)
        return hwnd if hwnd != 0 else None
    except ImportError:
        logger.error("win32gui 미설치 — pip install pywin32")
        return None
    except Exception as e:
        logger.error("find_kakao_window_error: %s", e)
        return None


def _activate_window(hwnd: int) -> bool:
    """창 활성화 (최소화 복원 포함)."""
    try:
        import win32gui
        import win32con

        # 최소화 상태면 복원
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)

        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        return True
    except Exception as e:
        logger.error("activate_window_error: %s", e)
        return False


def _search_chat_room(recipient: str) -> bool:
    """카카오톡 대화방 검색 및 진입."""
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1

        # Ctrl+F로 검색창 열기 (카카오톡 검색 단축키)
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.5)

        # 검색어 입력
        pyautogui.typewrite(recipient, interval=0.05) if recipient.isascii() else None
        if not recipient.isascii():
            # 한글 입력은 클립보드 방식
            import pyperclip
            pyperclip.copy(recipient)
            pyautogui.hotkey("ctrl", "v")
        time.sleep(0.5)

        # Enter로 대화방 진입
        pyautogui.press("enter")
        time.sleep(0.5)

        return True
    except ImportError as e:
        logger.error("pyautogui/pyperclip 미설치: %s", e)
        return False
    except Exception as e:
        logger.error("search_chat_room_error: %s", e)
        return False


def _send_message_to_chat(message: str) -> bool:
    """현재 활성 대화방에 메시지 전송."""
    try:
        import pyautogui
        import pyperclip

        # 메시지 입력 (클립보드 방식 — 한글 지원)
        pyperclip.copy(message)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.2)

        # Enter로 전송
        pyautogui.press("enter")
        time.sleep(0.3)

        return True
    except ImportError as e:
        logger.error("pyautogui/pyperclip 미설치: %s", e)
        return False
    except Exception as e:
        logger.error("send_message_error: %s", e)
        return False


async def kakao_send(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    카카오톡 메시지 전송.
    params: {"recipient": "대화방/수신자 이름", "message": "전송할 메시지"}
    """
    recipient = params.get("recipient", "")
    message = params.get("message", "")

    if not recipient:
        return {"status": "error", "data": {"error": "수신자(recipient)를 지정해주세요."}}
    if not message:
        return {"status": "error", "data": {"error": "전송할 메시지(message)를 입력해주세요."}}

    # 1. 카카오톡 창 찾기
    hwnd = _find_kakao_window()
    if hwnd is None:
        return {"status": "error", "data": {"error": "카카오톡이 실행되어 있지 않습니다."}}

    # 2. 창 활성화
    if not _activate_window(hwnd):
        return {"status": "error", "data": {"error": "카카오톡 창 활성화 실패"}}

    # 3. 대화방 검색
    if not _search_chat_room(recipient):
        return {"status": "error", "data": {"error": f"대화방 '{recipient}' 검색 실패"}}

    # 4. 메시지 전송
    if not _send_message_to_chat(message):
        return {"status": "error", "data": {"error": "메시지 전송 실패"}}

    logger.info("kakao_send_success recipient=%s", recipient)
    return {
        "status": "success",
        "data": {
            "recipient": recipient,
            "message": message,
            "sent": True,
        },
    }


async def kakao_read(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    카카오톡 현재 대화방 최근 메시지 읽기.
    Win32 API로 대화 목록 텍스트 추출.
    """
    hwnd = _find_kakao_window()
    if hwnd is None:
        return {"status": "error", "data": {"error": "카카오톡이 실행되어 있지 않습니다."}}

    if not _activate_window(hwnd):
        return {"status": "error", "data": {"error": "카카오톡 창 활성화 실패"}}

    try:
        import pyautogui

        # 대화 영역 스크린샷으로 읽기 시도 (OCR 필요 시 별도 처리)
        # 현재는 클립보드 복사 방식: Ctrl+A → Ctrl+C
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.2)

        import pyperclip
        text = pyperclip.paste()

        # 선택 해제
        pyautogui.press("escape")

        if not text:
            return {"status": "success", "data": {"messages": [], "note": "대화 내용을 읽지 못했습니다."}}

        # 텍스트를 메시지 단위로 파싱 (간단 형태)
        lines = text.strip().split("\n")
        messages: List[Dict[str, str]] = []
        for line in lines[-20:]:  # 최근 20줄
            line = line.strip()
            if line:
                messages.append({"text": line})

        return {"status": "success", "data": {"messages": messages}}
    except ImportError as e:
        return {"status": "error", "data": {"error": f"필수 라이브러리 미설치: {e}"}}
    except Exception as e:
        logger.error("kakao_read_error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
