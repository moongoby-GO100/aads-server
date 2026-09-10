import asyncio
from datetime import datetime


def test_enqueue_db_passes_datetime_to_asyncpg(monkeypatch):
    """asyncpg requires a datetime object for timestamptz query arguments."""
    from app.services import pc_agent_collection_queue as queue_module

    captured: dict[str, object] = {}

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def transaction(self):
            return FakeTransaction()

        async def execute(self, query, *args):
            return "UPDATE 0"

        async def fetchrow(self, query, *args):
            captured["next_run_at"] = args[13]
            return {"id": "queue-item", "tenant_id": None, "payload": {}, "result": {}}

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    async def fake_ready():
        return {"db_enabled": True}

    async def fake_pool():
        return FakePool()

    monkeypatch.setattr(queue_module, "ensure_queue_storage_ready", fake_ready)
    monkeypatch.setattr(queue_module, "_ensure_pool", fake_pool)

    item = queue_module.normalize_queue_item(
        {
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "next_run_at": "2026-09-10T15:35:34+09:00",
        }
    )

    asyncio.run(queue_module._enqueue_db(item))

    assert isinstance(captured["next_run_at"], datetime)
    assert captured["next_run_at"].isoformat() == "2026-09-10T15:35:34+09:00"
