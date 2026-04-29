"""AADS Device SDK — command dispatcher."""
from __future__ import annotations

import importlib
import logging
from typing import Callable

from device_sdk import plugin as _plugin_module

logger = logging.getLogger(__name__)


class CommandDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}

    def register_handler(self, command_type: str, handler: Callable) -> None:
        self._handlers[command_type] = handler

    def load_plugins(self, module) -> None:
        """Collect all @register'd handlers from plugin._REGISTRY after importing module."""
        importlib.import_module(module.__name__)
        for command_type, handler in _plugin_module.get_registry().items():
            if command_type not in self._handlers:
                self._handlers[command_type] = handler

    def load_safe(self, module_name: str) -> None:
        """Try importing a module; log a warning on failure (mirrors pc_agent's _safe_import)."""
        try:
            mod = importlib.import_module(module_name)
            self.load_plugins(mod)
        except Exception as e:
            logger.warning("모듈 임포트 실패 — %s 비활성화: %s", module_name, e)

    async def dispatch(self, command_type: str, params: dict) -> dict:
        handler = self._handlers.get(command_type)
        if handler is None:
            return {"status": "error", "data": {"error": f"지원하지 않는 명령: {command_type}"}}
        try:
            return await handler(params)
        except Exception as e:
            logger.error("핸들러 실행 오류 command_type=%s: %s", command_type, e)
            return {"status": "error", "data": {"error": str(e)}}

    def available_commands(self) -> list[str]:
        return list(self._handlers.keys())
