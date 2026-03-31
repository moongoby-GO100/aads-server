"""
AADS-195 Phase 3: 카카오톡 GUI 자동 조작.
ctypes 전용 — pyautogui/pyperclip/pywin32 의존성 제거.
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

# "나에게 보내기" 수신자 키워드
_SELF_RECIPIENTS = {"나", "나에게", "나에게 보내기", "me", "자신"}


def _find_kakao_window() -> Optional[int]:
    """카카오톡 메인 창 핸들 찾기 (ctypes — pywin32 불필요)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, _KAKAO_WINDOW_TITLE)
        if hwnd == 0:
            hwnd = user32.FindWindowW(_KAKAO_CLASS_NAME, None)
        return hwnd if hwnd != 0 else None
    except Exception as e:
        logger.error("find_kakao_window_error: %s", e)
        return None


def _activate_window(hwnd: int) -> bool:
    """창 활성화 (최소화 복원 포함, ctypes — pywin32 불필요)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        SW_RESTORE = 9

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.3)

        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        return True
    except Exception as e:
        logger.error("activate_window_error: %s", e)
        return False


def _search_chat_room(recipient: str) -> bool:
    """카카오톡 대화방 검색 및 진입 (ctypes 전용)."""
    try:
        from commands.win_input import hotkey, press_key, type_text_via_clipboard

        # Ctrl+F로 검색창 열기 (카카오톡 검색 단축키)
        hotkey("ctrl", "f")
        time.sleep(0.5)

        # 검색어 입력 (클립보드 방식 — 한글 지원)
        type_text_via_clipboard(recipient)
        time.sleep(0.8)

        # Enter로 대화방 진입
        press_key("enter")
        time.sleep(0.5)

        return True
    except Exception as e:
        logger.error("search_chat_room_error: %s", e)
        return False


def _send_message_to_chat(message: str) -> bool:
    """현재 활성 대화방에 메시지 전송 (ctypes 전용)."""
    try:
        from commands.win_input import press_key, type_text_via_clipboard

        # 메시지 입력 (클립보드 방식 — 한글 지원)
        type_text_via_clipboard(message)
        time.sleep(0.2)

        # Enter로 전송
        press_key("enter")
        time.sleep(0.3)

        return True
    except Exception as e:
        logger.error("send_message_error: %s", e)
        return False


def _open_my_chat(hwnd: int) -> bool:
    """내 프로필 나와의 채팅 열기 (키보드 전용, 좌표 불필요)."""
    try:
        from commands.win_input import hotkey, press_key

        # 1) 기존 팝업/검색 닫기
        press_key("escape")
        time.sleep(0.3)

        # 2) 친구 탭으로 이동 (Ctrl+1)
        hotkey("ctrl", "1")
        time.sleep(0.5)

        # 3) 최상단 이동 - 내 프로필이 항상 최상단
        press_key("home")
        time.sleep(0.3)

        # 4) 내 프로필 선택 (Enter 프로필 팝업)
        press_key("enter")
        time.sleep(0.8)

        # 5) 프로필 팝업에서 1:1 채팅 실행 (Enter 기본 동작)
        press_key("enter")
        time.sleep(0.8)

        return True
    except Exception as e:
        logger.error("open_my_chat_error: %s", e)
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

    # 3. 대화방 검색 또는 나와의 채팅
    if recipient.strip().lower() in {s.lower() for s in _SELF_RECIPIENTS}:
        if not _open_my_chat(hwnd):
            return {"status": "error", "data": {"error": "나와의 채팅 열기 실패"}}
    else:
        if not _search_chat_room(recipient):
            return {"status": "error", "data": {"error": f"대화방 '{recipient}' 검색 실패"}}

    # 4. 메시지 전송
    if not _send_message_to_chat(message):
        return {"status": "error", "data": {"error": "메시지 전송 실패"}}

    # 5. ESC로 검색 닫기 (원래 상태 복원)
    try:
        from commands.win_input import press_key
        press_key("escape")
    except Exception:
        pass

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
    클립보드 방식: Ctrl+A → Ctrl+C → 텍스트 파싱.
    """
    hwnd = _find_kakao_window()
    if hwnd is None:
        return {"status": "error", "data": {"error": "카카오톡이 실행되어 있지 않습니다."}}

    if not _activate_window(hwnd):
        return {"status": "error", "data": {"error": "카카오톡 창 활성화 실패"}}

    try:
        from commands.win_input import hotkey, press_key, clipboard_get

        # 대화 영역 전체 선택 → 복사
        hotkey("ctrl", "a")
        time.sleep(0.2)
        hotkey("ctrl", "c")
        time.sleep(0.2)

        text = clipboard_get() or ""

        # 선택 해제
        press_key("escape")

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
    except Exception as e:
        logger.error("kakao_read_error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
