"""
카카오톡 PC앱 자동 응답 모듈 (C안 Phase 1).

기존 PC Agent 명령(window_list, screen_text, keyboard_type 등)을 조합하여
카카오톡 채팅방을 감시하고 AI 응답을 자동 전송.
Windows 전용 — Linux에서 import 시 graceful 폴백.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Windows 전용 모듈 — Linux에서는 None
try:
    import ctypes
    from ctypes import wintypes
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False

# pyautogui 안전 장치
try:
    import pyautogui
    pyautogui.FAILSAFE = True
except ImportError:
    pyautogui = None  # type: ignore[assignment]

# ── 히스토리 파일 경로 ────────────────────────────────────────────────
_HISTORY_DIR = Path.home() / ".aads_kakao_auto"
_HISTORY_FILE = _HISTORY_DIR / "history.jsonl"

# ── 카카오톡 창 클래스명 ──────────────────────────────────────────────
_KAKAO_CHAT_CLASSES = ("EVA_Window_Dblclk", "EVA_Window")


class KakaoAutoResponder:
    """카카오톡 자동 응답 싱글톤."""

    _instance: Optional[KakaoAutoResponder] = None

    def __new__(cls) -> KakaoAutoResponder:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._running: bool = False
        self._target_rooms: List[str] = []
        self._config: Dict[str, Any] = {
            "delay_min": 1.0,
            "delay_max": 5.0,
            "tone": "friendly",
            "max_length": 200,
            "rate_limit_per_min": 10,
        }
        self._last_messages: Dict[str, str] = {}
        self._server_url: str = ""
        self._auth_token: str = ""
        self._monitor_task: Optional[asyncio.Task] = None
        self._history: List[Dict[str, Any]] = []
        self._sent_count_minute: int = 0
        self._minute_reset_time: float = 0.0

    # ── 창 탐지 ───────────────────────────────────────────────────────

    def _find_kakao_chat_windows(self) -> List[Dict[str, Any]]:
        """열린 카카오톡 채팅창 목록 반환."""
        if not _HAS_WIN32:
            logger.warning("kakao_auto: Windows 전용 — 현재 플랫폼에서 사용 불가")
            return []

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        windows: List[Dict[str, Any]] = []

        def enum_callback(hwnd: int, _: Any) -> bool:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value

                    class_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, class_buf, 256)
                    class_name = class_buf.value

                    if class_name in _KAKAO_CHAT_CLASSES:
                        windows.append({
                            "hwnd": hwnd,
                            "title": title,
                            "class": class_name,
                        })
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        return windows

    # ── 메시지 읽기 ───────────────────────────────────────────────────

    async def _read_last_message(self, hwnd: int) -> Optional[Dict[str, str]]:
        """채팅창에서 마지막 메시지 읽기 (클립보드 복사 방식)."""
        if not _HAS_WIN32 or pyautogui is None:
            return None

        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]

            # 창 활성화
            user32.SetForegroundWindow(hwnd)
            await asyncio.sleep(0.3)

            # 채팅 영역 전체 선택 → 복사
            pyautogui.hotkey("ctrl", "a")
            await asyncio.sleep(0.1)
            pyautogui.hotkey("ctrl", "c")
            await asyncio.sleep(0.2)

            # 클립보드 읽기
            import win32clipboard  # type: ignore[import-untyped]
            win32clipboard.OpenClipboard()
            try:
                text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except Exception:
                text = ""
            finally:
                win32clipboard.CloseClipboard()

            if not text.strip():
                return None

            # 마지막 줄 파싱 — "[발신자] [시간] 메시지" 패턴
            lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
            if not lines:
                return None

            last_line = lines[-1]
            # 카카오톡 메시지 형식: "[이름] [오후 3:42] 메시지내용"
            if last_line.startswith("["):
                parts = last_line.split("] ", 2)
                if len(parts) >= 3:
                    sender = parts[0].lstrip("[")
                    message = parts[2]
                    return {"sender": sender, "message": message, "raw": last_line}

            return {"sender": "", "message": last_line, "raw": last_line}

        except Exception as e:
            logger.error("kakao_auto read_message 오류: %s", e)
            return None

    # ── AI 응답 요청 ──────────────────────────────────────────────────

    async def _get_ai_response(self, room: str, message: str, sender: str) -> Optional[str]:
        """서버 API로 AI 응답 요청."""
        if not self._server_url:
            logger.error("kakao_auto: server_url 미설정")
            return None

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {}
                if self._auth_token:
                    headers["Authorization"] = f"Bearer {self._auth_token}"

                resp = await client.post(
                    f"{self._server_url}/api/v1/kakao-bot/respond",
                    json={
                        "room": room,
                        "message": message,
                        "sender": sender,
                        "tone": self._config.get("tone", "friendly"),
                        "max_length": self._config.get("max_length", 200),
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("reply")
        except Exception as e:
            logger.error("kakao_auto AI 응답 요청 실패: %s", e)
            return None

    # ── 메시지 전송 ───────────────────────────────────────────────────

    async def _send_message(self, hwnd: int, text: str) -> bool:
        """채팅 입력창에 응답 입력 + Enter."""
        if not _HAS_WIN32 or pyautogui is None:
            return False

        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.SetForegroundWindow(hwnd)
            await asyncio.sleep(0.3)

            # 입력창 포커스 (ESC로 선택 해제 후 입력)
            pyautogui.press("escape")
            await asyncio.sleep(0.1)

            # 텍스트 입력 — 500자 제한
            safe_text = text[:500]
            pyautogui.typewrite(safe_text, interval=0.02) if safe_text.isascii() else None
            # 한글 입력은 클립보드 복사 방식
            if not safe_text.isascii():
                import win32clipboard  # type: ignore[import-untyped]
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, safe_text)
                win32clipboard.CloseClipboard()
                pyautogui.hotkey("ctrl", "v")
                await asyncio.sleep(0.2)

            pyautogui.hotkey("enter")
            return True

        except Exception as e:
            logger.error("kakao_auto 메시지 전송 오류: %s", e)
            return False

    # ── 레이트 리밋 ───────────────────────────────────────────────────

    def _check_rate_limit(self) -> bool:
        """분당 발송 수 제한 확인."""
        now = time.time()
        if now - self._minute_reset_time >= 60:
            self._sent_count_minute = 0
            self._minute_reset_time = now

        limit = self._config.get("rate_limit_per_min", 10)
        return self._sent_count_minute < limit

    # ── 히스토리 기록 ─────────────────────────────────────────────────

    def _log_history(self, room: str, sender: str, message: str, reply: str) -> None:
        """응답 이력 기록."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "room": room,
            "sender": sender,
            "message": message,
            "reply": reply,
        }
        self._history.append(entry)
        # 메모리 내 최근 100건만 유지
        if len(self._history) > 100:
            self._history = self._history[-100:]

        # 파일 기록
        try:
            _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("kakao_auto 히스토리 기록 실패: %s", e)

    # ── 감시 루프 ─────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """2초 간격으로 카카오톡 새 메시지 감시."""
        logger.info("kakao_auto 감시 루프 시작 — 대상 채팅방: %s", self._target_rooms)

        while self._running:
            try:
                windows = self._find_kakao_chat_windows()

                for win in windows:
                    title = win["title"]
                    hwnd = win["hwnd"]

                    # 화이트리스트 필터
                    if self._target_rooms and title not in self._target_rooms:
                        continue

                    # 마지막 메시지 읽기
                    msg_data = await self._read_last_message(hwnd)
                    if not msg_data:
                        continue

                    raw = msg_data.get("raw", "")
                    sender = msg_data.get("sender", "")
                    message = msg_data.get("message", "")

                    # 중복 방지
                    if self._last_messages.get(title) == raw:
                        continue

                    self._last_messages[title] = raw

                    # 레이트 리밋
                    if not self._check_rate_limit():
                        logger.warning("kakao_auto 분당 발송 제한 초과")
                        continue

                    # AI 응답 요청
                    reply = await self._get_ai_response(title, message, sender)
                    if not reply:
                        continue

                    # 랜덤 딜레이 (사람처럼)
                    delay = random.uniform(
                        self._config.get("delay_min", 1.0),
                        self._config.get("delay_max", 5.0),
                    )
                    await asyncio.sleep(delay)

                    # 전송
                    success = await self._send_message(hwnd, reply)
                    if success:
                        self._sent_count_minute += 1
                        self._log_history(title, sender, message, reply)
                        logger.info("kakao_auto 응답 전송: room=%s sender=%s", title, sender)

            except Exception as e:
                logger.error("kakao_auto 감시 오류: %s", e)

            await asyncio.sleep(2)

    # ── 공개 메서드 ───────────────────────────────────────────────────

    async def start(
        self,
        rooms: List[str],
        server_url: str,
        auth_token: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """자동 응답 시작."""
        if self._running:
            raise RuntimeError("kakao_auto 이미 실행 중")

        self._target_rooms = rooms
        self._server_url = server_url.rstrip("/")
        self._auth_token = auth_token
        if config:
            self._config.update(config)

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("kakao_auto 시작: rooms=%s server=%s", rooms, self._server_url)

    async def stop(self) -> None:
        """자동 응답 중지."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._monitor_task = None
        logger.info("kakao_auto 중지됨")

    def get_status(self) -> Dict[str, Any]:
        """현재 상태 반환."""
        return {
            "running": self._running,
            "target_rooms": self._target_rooms,
            "config": self._config,
            "processed_count": len(self._history),
            "last_response_time": self._history[-1]["timestamp"] if self._history else None,
        }

    def update_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """설정 변경."""
        allowed_keys = {"delay_min", "delay_max", "tone", "max_length", "rate_limit_per_min"}
        for k, v in config.items():
            if k in allowed_keys:
                self._config[k] = v
        return self._config

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """최근 응답 이력 반환."""
        return self._history[-limit:]


# ── 싱글톤 접근 ───────────────────────────────────────────────────────

def _get_responder() -> KakaoAutoResponder:
    return KakaoAutoResponder()


# ── 6개 핸들러 (PC Agent command 인터페이스) ──────────────────────────

async def kakao_auto_start(params: Dict[str, Any]) -> Dict[str, Any]:
    """자동 응답 시작."""
    responder = _get_responder()
    try:
        rooms = params.get("rooms", [])
        server_url = params.get("server_url", "")
        auth_token = params.get("auth_token", "")
        config = params.get("config")

        if not server_url:
            return {"status": "error", "data": {"error": "server_url 필수"}}
        if not rooms:
            return {"status": "error", "data": {"error": "rooms (대상 채팅방 목록) 필수"}}

        await responder.start(rooms, server_url, auth_token, config)
        return {"status": "success", "data": {"message": "카카오톡 자동 응답 시작", "rooms": rooms}}
    except RuntimeError as e:
        return {"status": "error", "data": {"error": str(e)}}
    except Exception as e:
        logger.error("kakao_auto_start 오류: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def kakao_auto_stop(params: Dict[str, Any]) -> Dict[str, Any]:
    """자동 응답 중지."""
    responder = _get_responder()
    try:
        await responder.stop()
        return {"status": "success", "data": {"message": "카카오톡 자동 응답 중지됨"}}
    except Exception as e:
        logger.error("kakao_auto_stop 오류: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def kakao_auto_status(params: Dict[str, Any]) -> Dict[str, Any]:
    """현재 상태 조회."""
    responder = _get_responder()
    return {"status": "success", "data": responder.get_status()}


async def kakao_auto_config(params: Dict[str, Any]) -> Dict[str, Any]:
    """설정 변경."""
    responder = _get_responder()
    try:
        config = params.get("config", {})
        if not config:
            return {"status": "error", "data": {"error": "config 필수"}}
        updated = responder.update_config(config)
        return {"status": "success", "data": {"config": updated}}
    except Exception as e:
        logger.error("kakao_auto_config 오류: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def kakao_auto_rooms(params: Dict[str, Any]) -> Dict[str, Any]:
    """현재 열린 카카오톡 채팅방 목록 반환."""
    responder = _get_responder()
    try:
        windows = responder._find_kakao_chat_windows()
        rooms = [{"title": w["title"], "hwnd": w["hwnd"]} for w in windows]
        return {"status": "success", "data": {"rooms": rooms, "count": len(rooms)}}
    except Exception as e:
        logger.error("kakao_auto_rooms 오류: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def kakao_auto_history(params: Dict[str, Any]) -> Dict[str, Any]:
    """최근 자동 응답 이력 조회."""
    responder = _get_responder()
    limit = params.get("limit", 20)
    history = responder.get_history(limit)
    return {"status": "success", "data": {"history": history, "count": len(history)}}
