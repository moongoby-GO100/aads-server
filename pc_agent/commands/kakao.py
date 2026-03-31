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


def _get_window_rect(hwnd: int) -> Optional[Dict[str, int]]:
    """창의 위치/크기 반환."""
    try:
        import ctypes
        import ctypes.wintypes
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {
            "left": rect.left,
            "top": rect.top,
            "right": rect.right,
            "bottom": rect.bottom,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
        }
    except Exception as e:
        logger.error("get_window_rect_error: %s", e)
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
    """내 프로필 → 나와의 채팅 열기 (키보드 전용, 좌표 불필요)."""
    try:
        from commands.win_input import hotkey, press_key

        # 1) 기존 팝업/검색 닫기
        press_key("escape")
        time.sleep(0.3)

        # 2) 친구 탭으로 이동 (Ctrl+1)
        hotkey("ctrl", "1")
        time.sleep(0.5)

        # 3) 최상단 이동 — 내 프로필이 항상 최상단
        press_key("home")
        time.sleep(0.3)

        # 4) 내 프로필 선택 (Enter → 프로필 팝업)
        press_key("enter")
        time.sleep(0.8)

        # 5) 프로필 팝업에서 "1:1 채팅" 실행 (Enter = 기본 동작)
        press_key("enter")
        time.sleep(0.8)

        return True
    except Exception as e:
        logger.error("open_my_chat_error: %s", e)
        return False


# ── "나에게 보내기" 전용 함수들 ──────────────────────────────────────


def _detect_my_nickname() -> Optional[str]:
    """
    카카오톡 '내 프로필' 닉네임 자동 탐지.

    방법: 친구 탭 → Ctrl+A → Ctrl+C → 클립보드 첫 줄 = 내 닉네임.
    카카오톡 친구 목록에서 항상 맨 위에 '내 프로필'이 표시되므로
    전체 선택 → 복사하면 첫 줄이 본인 닉네임.
    """
    try:
        from commands.win_input import (
            clipboard_get,
            clipboard_set,
            hotkey,
            press_key,
        )

        # 1. ESC로 기존 팝업/검색 닫기
        press_key("escape")
        time.sleep(0.3)
        press_key("escape")
        time.sleep(0.3)

        # 2. 친구 탭으로 이동 (첫 번째 탭)
        #    카카오톡 PC에서 Ctrl+1 = 친구탭, Ctrl+2 = 채팅탭, Ctrl+3 = 더보기
        hotkey("ctrl", "1")
        time.sleep(0.5)

        # 3. 클립보드 초기화
        clipboard_set("")
        time.sleep(0.1)

        # 4. 전체 선택 → 복사
        hotkey("ctrl", "a")
        time.sleep(0.3)
        hotkey("ctrl", "c")
        time.sleep(0.3)

        # 5. 선택 해제
        press_key("escape")
        time.sleep(0.2)

        # 6. 클립보드에서 첫 줄 추출
        text = clipboard_get() or ""
        if not text.strip():
            logger.warning("detect_my_nickname: 클립보드 비어있음")
            return None

        lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
        if not lines:
            return None

        nickname = lines[0]
        logger.info("detect_my_nickname: '%s'", nickname)
        return nickname

    except Exception as e:
        logger.error("detect_my_nickname_error: %s", e)
        return None


def _open_my_chat_via_profile(hwnd: int) -> bool:
    """
    방안 2: 내 프로필 경유 "나와의 채팅" 열기.

    1. 친구 탭 이동
    2. 내 프로필 (맨 위) 더블클릭 → 프로필 팝업
    3. "나와의 채팅" 버튼 클릭

    좌표 계산: 카카오톡 창 위치 기준 상대 좌표 사용.
    """
    try:
        import ctypes
        from commands.win_input import hotkey, mouse_click, press_key

        user32 = ctypes.windll.user32

        # 1. 기존 팝업 닫기
        press_key("escape")
        time.sleep(0.3)

        # 2. 친구 탭 (Ctrl+1)
        hotkey("ctrl", "1")
        time.sleep(0.5)

        # 3. 카카오톡 창 위치 가져오기
        rect = _get_window_rect(hwnd)
        if not rect:
            logger.error("open_my_chat: 창 위치 가져오기 실패")
            return False

        left = rect["left"]
        top = rect["top"]
        width = rect["width"]

        # 4. "내 프로필" 위치 계산
        #    카카오톡 PC 레이아웃:
        #    - 좌측 사이드바: ~52px
        #    - 상단 탭/검색바: ~75px
        #    - 내 프로필: 검색바 바로 아래, 약 y=105 (창 상단 기준)
        #    - 내 프로필 영역 중앙: x = 사이드바 + (내용영역 / 2)
        sidebar_width = 52
        content_center_x = sidebar_width + (width - sidebar_width) // 2
        profile_y = 105

        profile_abs_x = left + content_center_x
        profile_abs_y = top + profile_y

        # 5. 내 프로필 더블클릭
        mouse_click(profile_abs_x, profile_abs_y)
        time.sleep(0.2)
        mouse_click(profile_abs_x, profile_abs_y)
        time.sleep(0.8)

        # 6. 프로필 팝업에서 "나와의 채팅" 버튼 찾기
        #    프로필 팝업은 화면 중앙에 ~450x580px 크기로 뜸
        #    "나와의 채팅" 버튼: 팝업 하단, 좌측에서 첫 번째 아이콘
        #    대략 팝업 중앙 x, 하단에서 약 60px 위
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)

        # 프로필 팝업 중앙 ≈ 화면 중앙
        popup_center_x = screen_w // 2
        popup_bottom_y = screen_h // 2 + 240  # 팝업 높이/2 ≈ 240

        # "나와의 채팅" 버튼: 팝업 하단 버튼 영역, 첫 번째 버튼
        chat_btn_x = popup_center_x - 80  # 약간 왼쪽 (첫 번째 버튼)
        chat_btn_y = popup_bottom_y - 50

        mouse_click(chat_btn_x, chat_btn_y)
        time.sleep(1.0)

        return True

    except Exception as e:
        logger.error("open_my_chat_via_profile_error: %s", e)
        return False


# ── 공개 API (PC Agent command 인터페이스) ───────────────────────────


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


async def kakao_detect_my_name(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    카카오톡 '내 프로필' 닉네임 자동 탐지.
    카카오톡이 실행 중이어야 함.
    """
    hwnd = _find_kakao_window()
    if hwnd is None:
        return {"status": "error", "data": {"error": "카카오톡이 실행되어 있지 않습니다."}}

    if not _activate_window(hwnd):
        return {"status": "error", "data": {"error": "카카오톡 창 활성화 실패"}}

    nickname = _detect_my_nickname()
    if not nickname:
        return {
            "status": "error",
            "data": {"error": "닉네임 탐지 실패. 카카오톡 친구 탭이 열려있는지 확인해주세요."},
        }

    return {
        "status": "success",
        "data": {"nickname": nickname},
    }


async def kakao_send_to_me(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    카카오톡 "나에게 보내기" — 본인에게 메시지 전송.

    2단계 접근:
    1단계: 닉네임 자동 탐지 → Ctrl+F 검색으로 "나와의 채팅" 진입 시도
    2단계: 실패 시 → 방안2 (내 프로필 더블클릭 → "나와의 채팅" 버튼)
    """
    message = params.get("message", "")
    if not message:
        return {"status": "error", "data": {"error": "전송할 메시지(message)를 입력해주세요."}}

    hwnd = _find_kakao_window()
    if hwnd is None:
        return {"status": "error", "data": {"error": "카카오톡이 실행되어 있지 않습니다."}}

    if not _activate_window(hwnd):
        return {"status": "error", "data": {"error": "카카오톡 창 활성화 실패"}}

    method_used = ""

    # ── 1단계: 닉네임 탐지 → 검색으로 나와의 채팅 진입 ──
    nickname = _detect_my_nickname()
    if nickname:
        logger.info("kakao_send_to_me: 닉네임 '%s' 탐지 성공, 검색 시도", nickname)

        # 채팅 탭으로 이동
        try:
            from commands.win_input import hotkey
            hotkey("ctrl", "2")  # 채팅 탭
            time.sleep(0.5)
        except Exception:
            pass

        # 닉네임으로 검색
        if _search_chat_room(nickname):
            method_used = "search"
            # 채팅방 진입 성공 → 메시지 전송
            if _send_message_to_chat(message):
                try:
                    from commands.win_input import press_key
                    press_key("escape")
                except Exception:
                    pass

                logger.info("kakao_send_to_me_success method=search nickname=%s", nickname)
                return {
                    "status": "success",
                    "data": {
                        "recipient": f"나 ({nickname})",
                        "message": message,
                        "sent": True,
                        "method": "search",
                        "nickname": nickname,
                    },
                }

    # ── 2단계: 방안2 — 내 프로필 경유 ──
    logger.info("kakao_send_to_me: 방안2 시도 (내 프로필 경유)")

    # 카카오톡 다시 활성화
    _activate_window(hwnd)

    if _open_my_chat_via_profile(hwnd):
        method_used = "profile"
        if _send_message_to_chat(message):
            logger.info("kakao_send_to_me_success method=profile")
            return {
                "status": "success",
                "data": {
                    "recipient": f"나 ({nickname or '프로필'})",
                    "message": message,
                    "sent": True,
                    "method": "profile",
                    "nickname": nickname,
                },
            }

    return {
        "status": "error",
        "data": {
            "error": "나에게 보내기 실패. 두 가지 방법 모두 실패했습니다.",
            "nickname_detected": nickname,
            "methods_tried": ["search", "profile"],
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
        from commands.win_input import clipboard_get, hotkey, press_key

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
