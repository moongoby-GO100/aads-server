"""모바일 에이전트 설정."""
from __future__ import annotations

import os
import shutil

SERVER_URL = os.environ.get(
    "DEVICE_SERVER_URL", "wss://aads.newtalk.kr/api/v1/devices/ws"
)
AGENT_TOKEN = os.environ.get("DEVICE_AGENT_TOKEN", "")

BLOCKED_COMMANDS = frozenset([
    "rm -rf /", "rm -rf /*", "dd if=", "mkfs.", "fdisk",
    "format", "shutdown", "reboot", "halt", "poweroff",
    "su ", "su\n", "chmod 777 /",
])

TERMUX_PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
IS_TERMUX = os.path.isdir(TERMUX_PREFIX)


def check_termux_binary(name: str) -> bool:
    return shutil.which(name) is not None


def is_command_safe(cmd: str) -> bool:
    cmd_lower = cmd.strip().lower()
    for blocked in BLOCKED_COMMANDS:
        if cmd_lower.startswith(blocked):
            return False
    return True
