"""AADS-195: PC Agent 명령 모듈 패키지."""
from __future__ import annotations

from . import shell, screenshot, file_ops, process, system_info, kakao, updater
from . import input_control, window_control, screen_utils, system_extra

# command_type → handler 함수 매핑
COMMAND_HANDLERS = {
    # 기본
    "shell": shell.execute,
    "screenshot": screenshot.execute,
    "file_list": file_ops.file_list,
    "file_read": file_ops.file_read,
    "file_write": file_ops.file_write,
    "process_list": process.execute,
    "process_kill": process.process_kill,
    "system_info": system_info.execute,
    "kakao_send": kakao.kakao_send,
    "kakao_read": kakao.kakao_read,
    "self_update": updater.execute,
    # P0: 마우스/키보드
    "mouse_click": input_control.mouse_click,
    "mouse_move": input_control.mouse_move,
    "mouse_scroll": input_control.mouse_scroll,
    "mouse_drag": input_control.mouse_drag,
    "keyboard_type": input_control.keyboard_type,
    "keyboard_hotkey": input_control.keyboard_hotkey,
    "keyboard_press": input_control.keyboard_press,
    # P0: 윈도우/클립보드/앱실행
    "window_list": window_control.window_list,
    "window_focus": window_control.window_focus,
    "clipboard_get": window_control.clipboard_get,
    "clipboard_set": window_control.clipboard_set,
    "app_launch": window_control.app_launch,
    # P1: 화면탐색/OCR/URL/대기/매크로
    "find_on_screen": screen_utils.find_on_screen,
    "screen_text": screen_utils.screen_text,
    "open_url": screen_utils.open_url,
    "wait": screen_utils.wait,
    "batch_command": screen_utils.batch_command,
    # P2: 볼륨/모니터/전원/앱목록/알림/파일검색
    "volume_control": system_extra.volume_control,
    "monitor_info": system_extra.monitor_info,
    "power_control": system_extra.power_control,
    "installed_apps": system_extra.installed_apps,
    "notification": system_extra.notification,
    "file_search": system_extra.file_search,
}

__all__ = [
    "shell", "screenshot", "file_ops", "process", "system_info",
    "kakao", "updater", "input_control", "window_control",
    "screen_utils", "system_extra", "COMMAND_HANDLERS",
]
