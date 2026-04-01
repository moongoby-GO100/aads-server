"""AADS-195: PC Agent 명령 모듈 패키지.

각 모듈 임포트 실패 시 해당 명령만 비활성화 (전체 크래시 방지).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 방어적 임포트 — 개별 모듈 실패 시 해당 명령만 비활성화
_modules = {}

def _safe_import(name: str):
    """commands 하위 모듈을 안전하게 임포트."""
    try:
        mod = __import__(f"commands.{name}", fromlist=[name])
        _modules[name] = mod
        return mod
    except Exception as e:
        logger.warning("commands.%s 임포트 실패 (해당 명령 비활성화): %s", name, e)
        return None

shell = _safe_import("shell")
screenshot = _safe_import("screenshot")
file_ops = _safe_import("file_ops")
process = _safe_import("process")
system_info = _safe_import("system_info")
kakao = _safe_import("kakao")
updater = _safe_import("updater")
input_control = _safe_import("input_control")
window_control = _safe_import("window_control")
screen_utils = _safe_import("screen_utils")
system_extra = _safe_import("system_extra")
screen_stream = _safe_import("screen_stream")
macro = _safe_import("macro")
browser_auto = _safe_import("browser_auto")
file_transfer = _safe_import("file_transfer")
scheduler = _safe_import("scheduler")
security = _safe_import("security")
process_monitor = _safe_import("process_monitor")
kakao_auto = _safe_import("kakao_auto")
network = _safe_import("network")


def _handler(mod, attr: str):
    """모듈이 None이면 None 반환, 아니면 속성 참조."""
    if mod is None:
        return None
    return getattr(mod, attr, None)


# command_type → handler 함수 매핑 (None 제거)
_RAW_HANDLERS = {
    # 기본
    "shell": _handler(shell, "execute"),
    "screenshot": _handler(screenshot, "execute"),
    "file_list": _handler(file_ops, "file_list"),
    "file_read": _handler(file_ops, "file_read"),
    "file_write": _handler(file_ops, "file_write"),
    "process_list": _handler(process, "execute"),
    "process_kill": _handler(process, "process_kill"),
    "system_info": _handler(system_info, "execute"),
    "kakao_send": _handler(kakao, "kakao_send"),
    "kakao_read": _handler(kakao, "kakao_read"),
    "kakao_send_to_me": _handler(kakao, "kakao_send_to_me"),
    "kakao_detect_my_name": _handler(kakao, "kakao_detect_my_name"),
    "self_update": _handler(updater, "execute"),
    # P0: 마우스/키보드
    "mouse_click": _handler(input_control, "mouse_click"),
    "mouse_move": _handler(input_control, "mouse_move"),
    "mouse_scroll": _handler(input_control, "mouse_scroll"),
    "mouse_drag": _handler(input_control, "mouse_drag"),
    "keyboard_type": _handler(input_control, "keyboard_type"),
    "keyboard_hotkey": _handler(input_control, "keyboard_hotkey"),
    "keyboard_press": _handler(input_control, "keyboard_press"),
    # P0: 윈도우/클립보드/앱실행
    "window_list": _handler(window_control, "window_list"),
    "window_focus": _handler(window_control, "window_focus"),
    "clipboard_get": _handler(window_control, "clipboard_get"),
    "clipboard_set": _handler(window_control, "clipboard_set"),
    "app_launch": _handler(window_control, "app_launch"),
    # P1: 화면탐색/OCR/URL/대기/매크로
    "find_on_screen": _handler(screen_utils, "find_on_screen"),
    "screen_text": _handler(screen_utils, "screen_text"),
    "open_url": _handler(screen_utils, "open_url"),
    "wait": _handler(screen_utils, "wait"),
    "batch_command": _handler(screen_utils, "batch_command"),
    # P2: 볼륨/모니터/전원/앱목록/알림/파일검색
    "volume_control": _handler(system_extra, "volume_control"),
    "monitor_info": _handler(system_extra, "monitor_info"),
    "power_control": _handler(system_extra, "power_control"),
    "installed_apps": _handler(system_extra, "installed_apps"),
    "notification": _handler(system_extra, "notification"),
    "file_search": _handler(system_extra, "file_search"),
    # 스트리밍
    "stream_start": _handler(screen_stream, "stream_start"),
    "stream_stop": _handler(screen_stream, "stream_stop"),
    # P3: 매크로 녹화/재생
    "macro_record_start": _handler(macro, "record_start"),
    "macro_record_stop": _handler(macro, "record_stop"),
    "macro_save": _handler(macro, "save_macro_cmd"),
    "macro_play": _handler(macro, "play_macro_cmd"),
    "macro_list": _handler(macro, "list_macros_cmd"),
    "macro_delete": _handler(macro, "delete_macro_cmd"),
    # P3: CDP 브라우저 자동화
    "browser_navigate": _handler(browser_auto, "browser_navigate"),
    "browser_click": _handler(browser_auto, "browser_click"),
    "browser_fill": _handler(browser_auto, "browser_fill"),
    "browser_screenshot": _handler(browser_auto, "browser_screenshot"),
    "browser_get_text": _handler(browser_auto, "browser_get_text"),
    "browser_eval": _handler(browser_auto, "browser_eval"),
    "browser_tabs": _handler(browser_auto, "browser_tabs"),
    "browser_launch": _handler(browser_auto, "browser_launch"),
    # P4: 파일 전송
    "file_upload": _handler(file_transfer, "file_upload"),
    "file_download": _handler(file_transfer, "file_download"),
    "file_sync_status": _handler(file_transfer, "file_sync_status"),
    # P4: 작업 스케줄러
    "schedule_add": _handler(scheduler, "schedule_add"),
    "schedule_remove": _handler(scheduler, "schedule_remove"),
    "schedule_list": _handler(scheduler, "schedule_list"),
    # P5: 보안 잠금 + 감사 로그
    "security_lock": _handler(security, "security_lock"),
    "security_unlock": _handler(security, "security_unlock"),
    "security_locked_list": _handler(security, "security_locked_list"),
    "security_audit": _handler(security, "security_audit"),
    # P5: 프로세스 감시
    "monitor_add": _handler(process_monitor, "monitor_add"),
    "monitor_remove": _handler(process_monitor, "monitor_remove"),
    "monitor_list": _handler(process_monitor, "monitor_list"),
    # P6: 카카오톡 자동 응답
    "kakao_auto_start": _handler(kakao_auto, "kakao_auto_start"),
    "kakao_auto_stop": _handler(kakao_auto, "kakao_auto_stop"),
    "kakao_auto_status": _handler(kakao_auto, "kakao_auto_status"),
    "kakao_auto_config": _handler(kakao_auto, "kakao_auto_config"),
    "kakao_auto_rooms": _handler(kakao_auto, "kakao_auto_rooms"),
    "kakao_auto_history": _handler(kakao_auto, "kakao_auto_history"),
    # P7: 네트워크 정보 (WoL용)
    "network_info": _handler(network, "network_info"),
    "wol_register": _handler(network, "wol_register"),
}

# None 핸들러 제거 — 사용 가능한 명령만 등록
COMMAND_HANDLERS = {k: v for k, v in _RAW_HANDLERS.items() if v is not None}

_failed = len(_RAW_HANDLERS) - len(COMMAND_HANDLERS)
if _failed:
    logger.warning("비활성화된 명령: %d개 (의존성 누락)", _failed)

__all__ = [
    "shell", "screenshot", "file_ops", "process", "system_info",
    "kakao", "updater", "input_control", "window_control",
    "screen_utils", "system_extra", "screen_stream",
    "macro", "browser_auto", "file_transfer", "scheduler",
    "security", "process_monitor", "kakao_auto", "network", "COMMAND_HANDLERS",
]
