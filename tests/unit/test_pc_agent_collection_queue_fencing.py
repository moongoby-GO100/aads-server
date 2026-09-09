"""Regression coverage for PostgreSQL queue lease fencing."""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _reload_queue(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    import app.services.pc_agent_collection_queue as queue

    return importlib.reload(queue)


def test_claim_separates_immediate_expiry_from_legacy_stale_threshold(monkeypatch):
    queue = _reload_queue(monkeypatch)
    now_value = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    captured: dict[str, object] = {}

    class Connection:
        async def execute(self, query, *args):
            captured["reclaim_query"] = query
            captured["reclaim_args"] = args

        async def fetchrow(self, query, *args):
            captured["claim_query"] = query
            captured["claim_args"] = args
            return None

    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, exc_type, exc, tb): return False

    class Pool:
        def acquire(self): return Acquire()

    async def ready(): return {"db_enabled": True}
    async def pool(): return Pool()

    monkeypatch.setattr(queue, "ensure_queue_storage_ready", ready)
    monkeypatch.setattr(queue, "_ensure_pool", pool)
    monkeypatch.setattr(queue, "_stale_running_seconds", lambda: 600)
    asyncio.run(queue._claim_next_db(agent_id="agent-1", now_value=now_value))

    reclaim_query = str(captured["reclaim_query"])
    assert "lease_expires_at IS NOT NULL AND lease_expires_at <= $1" in reclaim_query
    assert "lease_expires_at IS NULL" in reclaim_query
    assert "updated_at <= $2" in reclaim_query
    assert captured["reclaim_args"] == (now_value, now_value - timedelta(seconds=600))
    assert "owner_epoch = q.owner_epoch + 1" in str(captured["claim_query"])
    assert captured["claim_args"][:2] == (now_value, "agent-1")


@pytest.mark.parametrize("status", ["succeeded", "failed", "queued", "action_required"])
def test_stale_owner_cannot_overwrite_reassigned_lease(monkeypatch, status):
    queue = _reload_queue(monkeypatch)
    state = {"owner_instance": "green", "owner_epoch": 8, "status": "running"}

    class Connection:
        async def fetchrow(self, query, *args):
            assert "status <> 'running'" in query
            assert "lease_agent_id = ''" in query
            assert "owner_instance = ''" in query
            assert "lease_expires_at = NULL" in query
            owner_instance, owner_epoch = args[-2:]
            if (owner_instance, owner_epoch) != (state["owner_instance"], state["owner_epoch"]):
                return None
            state["status"] = args[1]
            return {"id": args[0], "tenant_id": None, "status": state["status"],
                    "payload": {}, "result": {}, "owner_instance": owner_instance,
                    "owner_epoch": owner_epoch}

    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, exc_type, exc, tb): return False

    class Pool:
        def acquire(self): return Acquire()

    async def ready(): return {"db_enabled": True}
    async def pool(): return Pool()

    monkeypatch.setattr(queue, "ensure_queue_storage_ready", ready)
    monkeypatch.setattr(queue, "_ensure_pool", pool)
    result = asyncio.run(queue._complete_db(
        item_id="00000000-0000-0000-0000-000000000001", status=status, result={},
        error_code="stale", message="stale completion", next_run_at="",
        owner_instance="blue", owner_epoch=7,
    ))
    assert result is None
    assert state["status"] == "running"


def test_unfenced_completion_cannot_update_a_running_claim(monkeypatch):
    queue = _reload_queue(monkeypatch)
    captured: dict[str, object] = {}

    class Connection:
        async def fetchrow(self, query, *args):
            captured["query"] = query
            captured["args"] = args
            return None

    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, exc_type, exc, tb): return False

    class Pool:
        def acquire(self): return Acquire()

    async def ready(): return {"db_enabled": True}
    async def pool(): return Pool()

    monkeypatch.setattr(queue, "ensure_queue_storage_ready", ready)
    monkeypatch.setattr(queue, "_ensure_pool", pool)
    result = asyncio.run(queue._complete_db(
        item_id="00000000-0000-0000-0000-000000000001", status="succeeded", result={},
        error_code="", message="legacy completion", next_run_at="",
    ))

    assert result is None
    assert "status <> 'running'" in str(captured["query"])
    assert captured["args"][-2:] == ("", None)


def test_fencing_migration_is_unique_and_additive():
    migrations = list(Path("migrations").glob("*.sql"))
    numbered: dict[str, list[str]] = {}
    for migration in migrations:
        prefix = migration.name.split("_", 1)[0]
        if prefix.isdigit():
            numbered.setdefault(prefix, []).append(migration.name)
    assert numbered["169"] == ["169_pc_agent_collection_queue_lease_fencing.sql"]
    migration = Path("migrations/169_pc_agent_collection_queue_lease_fencing.sql").read_text(encoding="utf-8")
    assert migration.count("ADD COLUMN IF NOT EXISTS") == 3
    assert "DROP " not in migration.upper()


def test_nineteen_legacy_rows_import_twice_without_duplicates(tmp_path, monkeypatch):
    queue = _reload_queue(monkeypatch)
    queue_path = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", queue_path)
    rows = [queue.normalize_queue_item({
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "service": f"legacy-{index}", "work_key": f"work-{index}",
    }) for index in range(1, 20)]
    queue_path.write_text(__import__("json").dumps(rows), encoding="utf-8")
    stored: set[str] = set()

    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc, tb): return False

    class Connection:
        def transaction(self): return Transaction()
        async def execute(self, query): return None
        async def fetchrow(self, query, *args):
            job_key = args[2]
            if job_key in stored: return None
            stored.add(job_key)
            return {"id": args[0]}

    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, exc_type, exc, tb): return False

    class Pool:
        def acquire(self): return Acquire()

    async def pool(): return Pool()
    monkeypatch.setattr(queue, "_ensure_pool", pool)

    first = asyncio.run(queue.reconcile_json_queue_to_db())
    second = asyncio.run(queue.reconcile_json_queue_to_db())
    assert first["imported"] == 19
    assert second["imported"] == 0
    assert len(stored) == 19
