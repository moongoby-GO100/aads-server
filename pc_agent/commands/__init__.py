"""AADS-195: PC Agent 명령 모듈 패키지."""
from __future__ import annotations

from . import shell, screenshot, file_ops, process, system_info, kakao, updater
from . import input_control, window_control, screen_utils, system_extra, screen_stream
from . import macro, browser_auto
from . import file_transfer, scheduler
from . import security, process_monitor
from . import kakao_auto

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
    # 스트리밍 (stream_start는 agent.py에서 직접 처리, 여기는 폴백)
    "stream_start": screen_stream.stream_start,
    "stream_stop": screen_stream.stream_stop,
    # P3: 매크로 녹화/재생
    "macro_record_start": macro.record_start,
    "macro_record_stop": macro.record_stop,
    "macro_save": macro.save_macro_cmd,
    "macro_play": macro.play_macro_cmd,
    "macro_list": macro.list_macros_cmd,
    "macro_delete": macro.delete_macro_cmd,
    # P3: CDP 브라우저 자동화
    "browser_navigate": browser_auto.browser_navigate,
    "browser_click": browser_auto.browser_click,
    "browser_fill": browser_auto.browser_fill,
    "browser_screenshot": browser_auto.browser_screenshot,
    "browser_get_text": browser_auto.browser_get_text,
    "browser_eval": browser_auto.browser_eval,
    "browser_tabs": browser_auto.browser_tabs,
    "browser_launch": browser_auto.browser_launch,
    # P4: 파일 전송 (서버↔PC)
    "file_upload": file_transfer.file_upload,
    "file_download": file_transfer.file_download,
    "file_sync_status": file_transfer.file_sync_status,
    # P4: 작업 스케줄러
    "schedule_add": scheduler.schedule_add,
    "schedule_remove": scheduler.schedule_remove,
    "schedule_list": scheduler.schedule_list,
    # P5: 보안 잠금 + 감사 로그
    "security_lock": security.security_lock,
    "security_unlock": security.security_unlock,
    "security_locked_list": security.security_locked_list,
    "security_audit": security.security_audit,
    # P5: 프로세스 감시
    "monitor_add": process_monitor.monitor_add,
    "monitor_remove": process_monitor.monitor_remove,
    "monitor_list": process_monitor.monitor_list,
    # P6: 카카오톡 자동 응답
    "kakao_auto_start": kakao_auto.kakao_auto_start,
    "kakao_auto_stop": kakao_auto.kakao_auto_stop,
    "kakao_auto_status": kakao_auto.kakao_auto_status,
    "kakao_auto_config": kakao_auto.kakao_auto_config,
    "kakao_auto_rooms": kakao_auto.kakao_auto_rooms,
    "kakao_auto_history": kakao_auto.kakao_auto_history,
}

__all__ = [
    "shell", "screenshot", "file_ops", "process", "system_info",
    "kakao", "updater", "input_control", "window_control",
    "screen_utils", "system_extra", "screen_stream",
    "macro", "browser_auto", "file_transfer", "scheduler",
    "security", "process_monitor", "kakao_auto", "COMMAND_HANDLERS",
]
