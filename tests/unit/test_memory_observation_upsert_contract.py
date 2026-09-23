"""Observation UPSERT must match the live project-aware unique index."""

import pytest

from app.services import memory_manager


@pytest.mark.asyncio
async def test_observe_targets_project_aware_unique_index(monkeypatch):
    calls = []

    class FakeConnection:
        async def execute(self, query, *args):
            calls.append((query, args))

        async def close(self):
            pass

    async def fake_get_conn():
        return FakeConnection()

    monkeypatch.setattr(memory_manager, "_get_conn", fake_get_conn)

    await memory_manager.MemoryManager().observe("learning", "key", "value")

    assert len(calls) == 1
    query, args = calls[0]
    assert "ON CONFLICT (category, key, COALESCE(project, ''))" in query
    assert args[:3] == ("learning", "key", "value")
