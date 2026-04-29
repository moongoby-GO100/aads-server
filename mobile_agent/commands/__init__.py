"""모바일 커맨드 모듈 — 안전 임포트 패턴."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_modules: Dict[str, Any] = {}


def _safe_import(name: str):
    try:
        mod = __import__(f"mobile_agent.commands.{name}", fromlist=[name])
        _modules[name] = mod
        return mod
    except Exception as e:
        logger.warning("mobile_agent.commands.%s 임포트 실패: %s", name, e)
        return None


def _handler(mod, attr: str):
    if mod is None:
        return None
    return getattr(mod, attr, None)


sms = _safe_import("sms")
location = _safe_import("location")
camera = _safe_import("camera")
notification = _safe_import("notification")
call = _safe_import("call")
battery = _safe_import("battery")
clipboard = _safe_import("clipboard")
vibrate = _safe_import("vibrate")
tts = _safe_import("tts")
wifi = _safe_import("wifi")
volume = _safe_import("volume")
shell = _safe_import("shell")
adb_input = _safe_import("adb_input")
adb_screenshot = _safe_import("adb_screenshot")

_RAW_HANDLERS: Dict[str, Callable | None] = {
    "sms_send": _handler(sms, "sms_send"),
    "sms_list": _handler(sms, "sms_list"),
    "location": _handler(location, "get_location"),
    "camera_photo": _handler(camera, "take_photo"),
    "notification_send": _handler(notification, "send_notification"),
    "notification_list": _handler(notification, "list_notifications"),
    "call": _handler(call, "make_call"),
    "call_log": _handler(call, "get_call_log"),
    "battery": _handler(battery, "get_battery_status"),
    "clipboard_get": _handler(clipboard, "clipboard_get"),
    "clipboard_set": _handler(clipboard, "clipboard_set"),
    "vibrate": _handler(vibrate, "do_vibrate"),
    "tts_speak": _handler(tts, "speak"),
    "wifi_info": _handler(wifi, "wifi_info"),
    "wifi_scan": _handler(wifi, "wifi_scan"),
    "volume_set": _handler(volume, "set_volume"),
    "shell": _handler(shell, "execute"),
    "adb_tap": _handler(adb_input, "tap"),
    "adb_swipe": _handler(adb_input, "swipe"),
    "adb_text": _handler(adb_input, "input_text"),
    "adb_screenshot": _handler(adb_screenshot, "take_screenshot"),
}

AVAILABLE_COMMANDS: Dict[str, Callable] = {
    k: v for k, v in _RAW_HANDLERS.items() if v is not None
}

_failed = len(_RAW_HANDLERS) - len(AVAILABLE_COMMANDS)
if _failed:
    logger.warning("모바일 커맨드 %d/%d 로드 실패", _failed, len(_RAW_HANDLERS))
logger.info("모바일 커맨드 %d개 활성화: %s", len(AVAILABLE_COMMANDS), list(AVAILABLE_COMMANDS.keys()))
