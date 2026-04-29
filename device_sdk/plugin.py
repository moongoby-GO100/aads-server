"""AADS Device SDK — plugin registration system."""
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Callable

_REGISTRY: dict[str, Callable] = {}


def register(command_type: str) -> Callable:
    """Decorator that registers an async function or CommandPlugin subclass into _REGISTRY."""
    def decorator(target: Callable) -> Callable:
        if inspect.isclass(target) and issubclass(target, CommandPlugin):
            instance = target()
            _REGISTRY[command_type] = instance.execute
        else:
            _REGISTRY[command_type] = target
        return target
    return decorator


class CommandPlugin(ABC):
    """Base class for plugin-style command handlers."""

    @abstractmethod
    async def execute(self, params: dict) -> dict:
        ...


def get_registry() -> dict[str, Callable]:
    return dict(_REGISTRY)
