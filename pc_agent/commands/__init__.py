"""AADS-195: PC Agent 명령 모듈 패키지."""
from __future__ import annotations

from . import shell, screenshot, file_ops, process, system_info, kakao, updater

# command_type → handler 함수 매핑
COMMAND_HANDLERS = {
    "shell": shell.execute,
    "screenshot": screenshot.execute,
    "file_list": file_ops.file_list,
    "file_read": file_ops.file_read,
    "file_write": file_ops.file_write,
    "process_list": process.execute,
    "system_info": system_info.execute,
    "kakao_send": kakao.kakao_send,
    "kakao_read": kakao.kakao_read,
    "self_update": updater.execute,
}

__all__ = [
    "shell",
    "screenshot",
    "file_ops",
    "process",
    "system_info",
    "kakao",
    "updater",
    "COMMAND_HANDLERS",
]
