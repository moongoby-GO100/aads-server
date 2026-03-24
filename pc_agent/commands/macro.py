"""AADS: 매크로 녹화/재생 — pynput 기반 이벤트 캡처 + pyautogui 재생."""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MACRO_DIR = Path.home() / ".aads_macros"


class MacroRecorder:
    """싱글톤 매크로 레코더 — 마우스/키보드 이벤트 녹화 및 재생."""

    _instance: Optional[MacroRecorder] = None
    _lock = threading.Lock()

    def __new__(cls) -> MacroRecorder:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._recording = False
        self._running = False
        self._events: List[Dict[str, Any]] = []
        self._start_time: float = 0.0
        self._mouse_listener: Any = None
        self._key_listener: Any = None
        MACRO_DIR.mkdir(parents=True, exist_ok=True)

    def start_recording(self) -> Dict[str, Any]:
        """pynput으로 마우스/키보드 이벤트 녹화 시작."""
        if self._recording:
            return {"status": "error", "data": {"error": "이미 녹화 중입니다"}}

        self._events = []
        self._start_time = time.monotonic()
        self._recording = True

        try:
            from pynput import mouse, keyboard

            def on_click(x: int, y: int, button: Any, pressed: bool) -> None:
                if not self._recording:
                    return
                if pressed:
                    self._events.append({
                        "type": "mouse_click",
                        "x": int(x), "y": int(y),
                        "button": str(button),
                        "time": round(time.monotonic() - self._start_time, 3),
                    })

            def on_move(x: int, y: int) -> None:
                if not self._recording:
                    return
                # 이동 이벤트는 100ms 간격으로 샘플링 (과도한 이벤트 방지)
                elapsed = round(time.monotonic() - self._start_time, 3)
                if self._events and self._events[-1]["type"] == "mouse_move":
                    if elapsed - self._events[-1]["time"] < 0.1:
                        return
                self._events.append({
                    "type": "mouse_move",
                    "x": int(x), "y": int(y),
                    "time": elapsed,
                })

            def on_press(key: Any) -> None:
                if not self._recording:
                    return
                try:
                    key_str = key.char if hasattr(key, "char") and key.char else str(key)
                except AttributeError:
                    key_str = str(key)
                self._events.append({
                    "type": "key_press",
                    "key": key_str,
                    "time": round(time.monotonic() - self._start_time, 3),
                })

            def on_release(key: Any) -> None:
                if not self._recording:
                    return
                try:
                    key_str = key.char if hasattr(key, "char") and key.char else str(key)
                except AttributeError:
                    key_str = str(key)
                self._events.append({
                    "type": "key_release",
                    "key": key_str,
                    "time": round(time.monotonic() - self._start_time, 3),
                })

            self._mouse_listener = mouse.Listener(on_click=on_click, on_move=on_move)
            self._key_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._mouse_listener.start()
            self._key_listener.start()

            return {"status": "success", "data": {"message": "매크로 녹화 시작 (pynput)"}}

        except ImportError:
            logger.warning("pynput 미설치 — 수동 좌표 기록 모드로 전환")
            return {
                "status": "success",
                "data": {
                    "message": "매크로 녹화 시작 (수동 모드 — pynput 미설치)",
                    "note": "pynput 미설치로 자동 이벤트 캡처 불가. "
                            "macro_save로 수동 이벤트 배열을 전달하세요. "
                            "설치: pip install pynput",
                },
            }

    def stop_recording(self) -> Dict[str, Any]:
        """녹화 중지, 이벤트 리스트 반환."""
        if not self._recording:
            return {"status": "error", "data": {"error": "녹화 중이 아닙니다"}}

        self._recording = False

        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None
        if self._key_listener:
            self._key_listener.stop()
            self._key_listener = None

        event_count = len(self._events)
        duration = round(self._events[-1]["time"], 1) if self._events else 0
        return {
            "status": "success",
            "data": {
                "message": f"녹화 완료 — {event_count}개 이벤트, {duration}초",
                "event_count": event_count,
                "duration": duration,
            },
        }

    def save_macro(self, name: str, events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """매크로를 JSON 파일로 저장."""
        if not name:
            return {"status": "error", "data": {"error": "매크로 이름이 필요합니다"}}

        # 외부 이벤트가 전달되면 사용, 아니면 녹화된 이벤트 사용
        save_events = events if events is not None else self._events
        if not save_events:
            return {"status": "error", "data": {"error": "저장할 이벤트가 없습니다"}}

        # 안전한 파일명
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_")
        if not safe_name:
            return {"status": "error", "data": {"error": "유효하지 않은 매크로 이름입니다"}}

        file_path = MACRO_DIR / f"{safe_name}.json"
        macro_data = {
            "name": safe_name,
            "event_count": len(save_events),
            "events": save_events,
        }
        file_path.write_text(json.dumps(macro_data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("매크로 저장: %s (%d 이벤트)", safe_name, len(save_events))
        return {
            "status": "success",
            "data": {"message": f"매크로 '{safe_name}' 저장 완료 ({len(save_events)}개 이벤트)"},
        }

    def load_macro(self, name: str) -> Optional[Dict[str, Any]]:
        """저장된 매크로 로드."""
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_")
        file_path = MACRO_DIR / f"{safe_name}.json"
        if not file_path.exists():
            return None
        return json.loads(file_path.read_text(encoding="utf-8"))

    def play_macro(self, name: str, repeat: int = 1, speed: float = 1.0) -> Dict[str, Any]:
        """매크로 재생 — pyautogui로 이벤트 재현."""
        macro = self.load_macro(name)
        if not macro:
            return {"status": "error", "data": {"error": f"매크로 '{name}'을 찾을 수 없습니다"}}

        try:
            import pyautogui
        except ImportError:
            return {
                "status": "error",
                "data": {"error": "pyautogui 미설치. 설치: pip install pyautogui"},
            }

        events = macro.get("events", [])
        if not events:
            return {"status": "error", "data": {"error": "재생할 이벤트가 없습니다"}}

        self._running = True
        total_played = 0

        try:
            pyautogui.FAILSAFE = True
            for _round in range(repeat):
                if not self._running:
                    break
                prev_time = 0.0
                for event in events:
                    if not self._running:
                        break
                    # 이벤트 간 대기 (속도 조절)
                    delay = (event["time"] - prev_time) / speed if speed > 0 else 0
                    if delay > 0:
                        time.sleep(delay)
                    prev_time = event["time"]

                    etype = event["type"]
                    if etype == "mouse_click":
                        pyautogui.click(event["x"], event["y"])
                    elif etype == "mouse_move":
                        pyautogui.moveTo(event["x"], event["y"], duration=0)
                    elif etype == "key_press":
                        key = event["key"].replace("Key.", "")
                        try:
                            pyautogui.keyDown(key)
                        except Exception:
                            pass
                    elif etype == "key_release":
                        key = event["key"].replace("Key.", "")
                        try:
                            pyautogui.keyUp(key)
                        except Exception:
                            pass
                    total_played += 1
        finally:
            self._running = False

        return {
            "status": "success",
            "data": {
                "message": f"매크로 '{name}' 재생 완료 — {total_played}개 이벤트, {repeat}회 반복",
            },
        }

    def cancel_playback(self) -> None:
        """매크로 재생 중단."""
        self._running = False

    def list_macros(self) -> List[Dict[str, Any]]:
        """저장된 매크로 목록."""
        macros = []
        for f in sorted(MACRO_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                macros.append({
                    "name": data.get("name", f.stem),
                    "event_count": data.get("event_count", 0),
                })
            except Exception:
                macros.append({"name": f.stem, "event_count": -1})
        return macros

    def delete_macro(self, name: str) -> Dict[str, Any]:
        """매크로 삭제."""
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_")
        file_path = MACRO_DIR / f"{safe_name}.json"
        if not file_path.exists():
            return {"status": "error", "data": {"error": f"매크로 '{name}'을 찾을 수 없습니다"}}
        file_path.unlink()
        logger.info("매크로 삭제: %s", safe_name)
        return {"status": "success", "data": {"message": f"매크로 '{safe_name}' 삭제 완료"}}


# ── 싱글톤 인스턴스 ─────────────────────────────────────────────────────────
_recorder = MacroRecorder()


# ── 커맨드 핸들러 (async) ──────────────────────────────────────────────────
async def record_start(params: Dict[str, Any]) -> Dict[str, Any]:
    """매크로 녹화 시작."""
    return _recorder.start_recording()


async def record_stop(params: Dict[str, Any]) -> Dict[str, Any]:
    """매크로 녹화 중지."""
    return _recorder.stop_recording()


async def save_macro_cmd(params: Dict[str, Any]) -> Dict[str, Any]:
    """매크로 저장. params: name(필수), events(선택)"""
    name = params.get("name", "")
    events = params.get("events")
    return _recorder.save_macro(name, events)


async def play_macro_cmd(params: Dict[str, Any]) -> Dict[str, Any]:
    """매크로 재생. params: name(필수), repeat(기본1), speed(기본1.0)"""
    name = params.get("name", "")
    if not name:
        return {"status": "error", "data": {"error": "매크로 이름이 필요합니다"}}
    repeat = int(params.get("repeat", 1))
    speed = float(params.get("speed", 1.0))
    return _recorder.play_macro(name, repeat=repeat, speed=speed)


async def list_macros_cmd(params: Dict[str, Any]) -> Dict[str, Any]:
    """저장된 매크로 목록."""
    macros = _recorder.list_macros()
    return {"status": "success", "data": {"macros": macros, "count": len(macros)}}


async def delete_macro_cmd(params: Dict[str, Any]) -> Dict[str, Any]:
    """매크로 삭제. params: name(필수)"""
    name = params.get("name", "")
    if not name:
        return {"status": "error", "data": {"error": "매크로 이름이 필요합니다"}}
    return _recorder.delete_macro(name)
