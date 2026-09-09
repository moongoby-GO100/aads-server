"""Regression: the synchronous queue API must reuse one private event loop.

``asyncio.run()`` per call closed the loop that owned the shared asyncpg pool,
so the second synchronous call in scripts/yeoljeong_auto_collect.py failed with
``Event loop is closed`` / ``another operation is in progress`` (queue drain exit=1).
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import pc_agent_collection_queue as queue_module


def test_run_db_reuses_single_private_loop(monkeypatch):
    monkeypatch.setattr(queue_module, "_db_enabled", lambda: True)
    monkeypatch.setattr(queue_module, "_SYNC_LOOP", None)
    seen: list[asyncio.AbstractEventLoop] = []

    async def probe() -> int:
        seen.append(asyncio.get_running_loop())
        return len(seen)

    assert queue_module._run_db(probe()) == 1
    assert queue_module._run_db(probe()) == 2
    assert seen[0] is seen[1]
    assert not seen[0].is_closed()


def test_ensure_pool_uses_private_pool_on_sync_loop(monkeypatch):
    monkeypatch.setattr(queue_module, "_db_enabled", lambda: True)
    monkeypatch.setattr(queue_module, "_SYNC_LOOP", None)
    sentinel = object()

    async def fake_sync_pool():
        return sentinel

    monkeypatch.setattr(queue_module, "_ensure_sync_pool", fake_sync_pool)
    assert queue_module._run_db(queue_module._ensure_pool()) is sentinel


def test_ensure_pool_keeps_shared_pool_outside_sync_loop(monkeypatch):
    shared = object()
    monkeypatch.setattr(queue_module, "_SYNC_LOOP", None)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: shared)
    assert asyncio.run(queue_module._ensure_pool()) is shared


def test_run_db_still_rejects_calls_inside_running_loop(monkeypatch):
    monkeypatch.setattr(queue_module, "_db_enabled", lambda: True)

    async def scenario() -> None:
        async def noop() -> None:
            return None

        with pytest.raises(RuntimeError, match=r"use the \*_async queue API"):
            queue_module._run_db(noop())

    asyncio.run(scenario())
