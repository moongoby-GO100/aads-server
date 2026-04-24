from unittest.mock import AsyncMock, Mock

import pytest

from app.api import llm_keys
from app.core import llm_key_provider
from app.services import model_registry


class _AsyncTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AsyncAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AsyncAcquire(self.conn)


class FakeConn:
    def __init__(self, fetchrows=None):
        self.fetchrows = list(fetchrows or [])
        self.fetchrow_calls = []
        self.execute_calls = []

    def transaction(self):
        return _AsyncTransaction()

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if not self.fetchrows:
            return None
        return self.fetchrows.pop(0)

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"


@pytest.mark.asyncio
async def test_create_llm_key_triggers_registry_sync(monkeypatch):
    conn = FakeConn(
        fetchrows=[
            {
                "id": 1,
                "provider": "openai",
                "key_name": "OPENAI_API_KEY",
                "label": "Primary",
                "priority": 1,
                "is_active": True,
                "created_at": None,
            }
        ]
    )
    sync_mock = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr(llm_keys, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(llm_keys, "_validate_priority", AsyncMock())
    monkeypatch.setattr(llm_keys, "append_key_audit_log", AsyncMock())
    monkeypatch.setattr(llm_keys, "encrypt_value", lambda value: f"enc::{value}")
    monkeypatch.setattr(llm_keys, "_run_registry_sync", sync_mock)

    result = await llm_keys.create_llm_key(
        llm_keys.LlmKeyCreate(
            provider="openai",
            key_name="OPENAI_API_KEY",
            value="secret",
            label="Primary",
            priority=1,
        )
    )

    sync_mock.assert_awaited_once_with("create:OPENAI_API_KEY")
    assert result["key_name"] == "OPENAI_API_KEY"


@pytest.mark.asyncio
async def test_update_llm_key_triggers_registry_sync(monkeypatch):
    conn = FakeConn(
        fetchrows=[
            {
                "id": 9,
                "provider": "openai",
                "key_name": "OPENAI_API_KEY",
                "label": "Primary",
                "priority": 1,
                "is_active": True,
                "notes": "",
            },
            {
                "id": 9,
                "provider": "openai",
                "key_name": "OPENAI_API_KEY",
                "label": "Updated",
                "priority": 1,
                "is_active": True,
                "updated_at": None,
            },
        ]
    )
    sync_mock = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr(llm_keys, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(llm_keys, "_validate_priority", AsyncMock())
    monkeypatch.setattr(llm_keys, "append_key_audit_log", AsyncMock())
    monkeypatch.setattr(llm_keys, "encrypt_value", lambda value: f"enc::{value}")
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", Mock())
    monkeypatch.setattr(llm_keys, "_run_registry_sync", sync_mock)

    result = await llm_keys.update_llm_key(
        9,
        llm_keys.LlmKeyUpdate(label="Updated"),
    )

    sync_mock.assert_awaited_once_with("update:OPENAI_API_KEY")
    assert result["key_name"] == "OPENAI_API_KEY"


@pytest.mark.asyncio
async def test_activate_llm_key_triggers_registry_sync(monkeypatch):
    conn = FakeConn(
        fetchrows=[
            {"id": 7, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN", "priority": 1},
            {"id": 7, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN", "is_active": True, "updated_at": None},
        ]
    )
    sync_mock = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr(llm_keys, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(llm_keys, "_validate_priority", AsyncMock())
    monkeypatch.setattr(llm_keys, "append_key_audit_log", AsyncMock())
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", Mock())
    monkeypatch.setattr(llm_keys, "_run_registry_sync", sync_mock)

    result = await llm_keys.activate_llm_key(7)

    sync_mock.assert_awaited_once_with("activate:ANTHROPIC_AUTH_TOKEN")
    assert result["is_active"] is True


@pytest.mark.asyncio
async def test_deactivate_llm_key_triggers_registry_sync(monkeypatch):
    conn = FakeConn(
        fetchrows=[
            {"id": 11, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN"},
            {"id": 11, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN", "is_active": False, "updated_at": None},
        ]
    )
    sync_mock = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr(llm_keys, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(llm_keys, "append_key_audit_log", AsyncMock())
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", Mock())
    monkeypatch.setattr(llm_keys, "_run_registry_sync", sync_mock)

    result = await llm_keys.deactivate_llm_key(11)

    sync_mock.assert_awaited_once_with("deactivate:ANTHROPIC_AUTH_TOKEN")
    assert result["is_active"] is False


@pytest.mark.asyncio
async def test_mark_key_rate_limited_triggers_registry_sync(monkeypatch):
    conn = FakeConn()
    sync_mock = AsyncMock(return_value={"ok": True})
    invalidate_registry_cache = Mock()

    monkeypatch.setattr(llm_key_provider, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(llm_key_provider, "invalidate_key_cache", Mock())
    monkeypatch.setattr(model_registry, "sync_model_registry", sync_mock)
    monkeypatch.setattr(model_registry, "invalidate_registry_cache", invalidate_registry_cache)

    await llm_key_provider.mark_key_rate_limited("OPENAI_API_KEY", seconds=60)

    sync_mock.assert_awaited_once_with(triggered_by="llm_key_provider", reason="rate_limited:OPENAI_API_KEY")
    assert any("rate_limited_until" in query for query, _ in conn.execute_calls)


@pytest.mark.asyncio
async def test_store_api_key_triggers_registry_sync(monkeypatch):
    conn = FakeConn()
    sync_mock = AsyncMock(return_value={"ok": True})
    invalidate_registry_cache = Mock()

    monkeypatch.setattr(llm_key_provider, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(llm_key_provider, "encrypt_value", lambda value: f"enc::{value}")
    monkeypatch.setattr(model_registry, "sync_model_registry", sync_mock)
    monkeypatch.setattr(model_registry, "invalidate_registry_cache", invalidate_registry_cache)

    await llm_key_provider.store_api_key(
        "OPENAI_API_KEY",
        "secret",
        "openai",
        label="Primary",
        priority=1,
    )

    sync_mock.assert_awaited_once_with(triggered_by="llm_key_provider", reason="store:OPENAI_API_KEY")
    assert any("INSERT INTO llm_api_keys" in query for query, _ in conn.execute_calls)
