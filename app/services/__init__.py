"""AADS-130: Services package shared business logic."""
from __future__ import annotations

from importlib import import_module
from typing import Any

from .cost_tracker import CostLimitExceeded, check_and_increment
from .db_recorder import record_artifact
from .model_router import estimate_cost, get_llm_for_agent

__all__ = [
    "CostLimitExceeded",
    "check_and_increment",
    "estimate_cost",
    "get_llm_for_agent",
    "memory_manager",
    "record_artifact",
]


def __getattr__(name: str) -> Any:
    if name == "memory_manager":
        return import_module(".memory_manager", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
