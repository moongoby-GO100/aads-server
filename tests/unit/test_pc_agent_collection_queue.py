import asyncio
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest


def test_async_queue_api_uses_db_inside_running_event_loop(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    calls: list[str] = []

    async def fake_enqueue(item):
        calls.append("enqueue")
        return item

    async def fake_claim_next_db(*, agent_id, now_value):
        calls.append(f"claim:{agent_id}")
        return {"id": "claimed", "status": "running"}

    async def fake_complete_db(**kwargs):
        calls.append(f"complete:{kwargs['status']}")
        return {"id": kwargs["item_id"], "status": kwargs["status"]}

    async def fake_snapshot_db(*, limit):
        calls.append(f"snapshot:{limit}")
        return [{"id": "db-row"}]

    monkeypatch.setattr(queue_module, "_enqueue_db", fake_enqueue)
    monkeypatch.setattr(queue_module, "_claim_next_db", fake_claim_next_db)
    monkeypatch.setattr(queue_module, "_complete_db", fake_complete_db)
    monkeypatch.setattr(queue_module, "_snapshot_db", fake_snapshot_db)

    async def scenario():
        queued = await queue_module.enqueue_collection_item_async({"service": "baemin"})
        claimed = await queue_module.claim_next_collection_item_async(agent_id="agent-1")
        completed = await queue_module.complete_collection_item_async(
            claimed["id"], status="succeeded"
        )
        snapshot = await queue_module.queue_snapshot_async(25)
        return queued, claimed, completed, snapshot

    queued, claimed, completed, snapshot = asyncio.run(scenario())

    assert queued["service"] == "baemin"
    assert claimed["status"] == "running"
    assert completed["status"] == "succeeded"
    assert snapshot == [{"id": "db-row"}]
    assert calls == ["enqueue", "claim:agent-1", "complete:succeeded", "snapshot:25"]
    assert not queue_path.exists()


def test_sync_db_api_fails_closed_inside_running_event_loop(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)

    async def scenario():
        with pytest.raises(RuntimeError, match=r"use the \*_async queue API"):
            queue_module.queue_snapshot()

    asyncio.run(scenario())


def test_async_db_error_does_not_fall_back_to_json(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)

    async def fail_snapshot(*, limit):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(queue_module, "_snapshot_db", fail_snapshot)

    with pytest.raises(ConnectionError, match="database unavailable"):
        asyncio.run(queue_module.queue_snapshot_async())
    assert not queue_path.exists()


@pytest.mark.parametrize("contents", ["{not-json", '{"unexpected": "object"}'])
def test_db_reconciliation_fails_closed_for_invalid_json_queue(
    tmp_path, monkeypatch, contents
):
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)

    with pytest.raises(RuntimeError, match="Cannot reconcile"):
        asyncio.run(queue_module.reconcile_json_queue_to_db())


def test_json_to_db_reconciliation_is_idempotent_and_removes_secrets(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    source = queue_module.normalize_queue_item(
        {
            "id": "7dc5272e-95dd-4a5c-9681-21b4fbbe2181",
            "queue_type": "bank",
            "service": "ibk_business",
            "business_id": "biz-junghwa",
            "work_key": "ibk-junghwa",
            "status": "action_required",
            "created_at": "2026-09-09T09:00:00+09:00",
            "updated_at": "2026-09-09T10:00:00+09:00",
            "payload": {
                "bank_account_id": "account-1",
                "login_password": "must-not-migrate",
                "nested": {"access_token": "must-not-migrate", "safe": "kept"},
            },
            "result": {"approved_input": "must-not-migrate", "count": 3},
        }
    )
    queue_path.write_text(json.dumps([source], ensure_ascii=False), encoding="utf-8")

    stored: dict[str, tuple] = {}

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def transaction(self):
            return FakeTransaction()

        async def execute(self, query):
            assert "pg_advisory_xact_lock" in query

        async def fetchrow(self, query, *args):
            assert "$25::timestamptz, $26::timestamptz" in query
            job_key = args[2]
            if job_key in stored:
                return None
            stored[job_key] = args
            return {"id": args[0]}

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    async def fake_ensure_pool():
        return FakePool()

    monkeypatch.setattr(queue_module, "_ensure_pool", fake_ensure_pool)

    first = asyncio.run(queue_module.reconcile_json_queue_to_db())
    second = asyncio.run(queue_module.reconcile_json_queue_to_db())

    assert first == {
        "db_enabled": True,
        "json_rows": 1,
        "imported": 1,
        "skipped": 0,
        "secrets_removed": 3,
    }
    assert second["imported"] == 0
    assert second["skipped"] == 1
    stored_args = next(iter(stored.values()))
    assert str(stored_args[0]) == source["id"]
    assert stored_args[15].isoformat() == source["next_run_at"]
    assert stored_args[24] is None
    assert stored_args[25] is None
    assert stored_args[26].isoformat() == source["created_at"]
    assert stored_args[27].isoformat() == source["updated_at"]
    migrated_payload = json.loads(stored_args[19])
    migrated_result = json.loads(stored_args[20])
    assert migrated_payload == {"bank_account_id": "account-1", "nested": {"safe": "kept"}}
    assert migrated_result == {"count": 3}


def test_global_queue_latest_only_supersedes_same_resource(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    first = queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:coupangeats",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "work-coupangeats-mia",
            "priority": 20,
            "latest_only": True,
            "payload": {"services": ["coupangeats"], "business_id": "biz-mia"},
        }
    )
    second = queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:coupangeats",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "work-coupangeats-mia",
            "priority": 20,
            "latest_only": True,
            "payload": {"services": ["coupangeats"], "business_id": "biz-mia", "date_to": "2026-08-28"},
        }
    )

    snapshot = queue_module.queue_snapshot()
    assert len(snapshot) == 1
    assert first["job_key"] == second["job_key"]
    assert snapshot[0]["payload"]["date_to"] == "2026-08-28"


def test_bank_latest_only_is_scoped_by_service_and_account(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    ibk = queue_module.enqueue_collection_item(
        {
            "queue_type": "bank",
            "site_key": "bank:ibk_business",
            "service": "ibk_business",
            "business_id": "biz-junghwa",
            "branch": "branch-junghwa",
            "work_key": "yeoljeong-bank-ibk-junghwa",
            "priority": 10,
            "latest_only": True,
            "payload": {
                "project_key": "BANKING",
                "bank_account_id": "acct-ibk-junghwa",
                "browser_agent_id": "agent-bank",
            },
        }
    )
    shinhan = queue_module.enqueue_collection_item(
        {
            "queue_type": "bank",
            "site_key": "bank:shinhan_business",
            "service": "shinhan_business",
            "business_id": "biz-junghwa",
            "branch": "branch-junghwa",
            "work_key": "yeoljeong-bank-shinhan-junghwa",
            "priority": 10,
            "latest_only": True,
            "payload": {
                "project_key": "BANKING",
                "bank_account_id": "acct-shinhan-junghwa",
                "browser_agent_id": "agent-bank",
            },
        }
    )

    snapshot = queue_module.queue_snapshot()

    assert len(snapshot) == 2
    assert ibk["resource_key"] != shinhan["resource_key"]
    assert ibk["resource_key"].startswith("financial_exclusive|")
    assert shinhan["resource_key"].startswith("financial_exclusive|")
    assert {row["status"] for row in snapshot} == {"queued"}


def test_global_queue_preserves_action_required_until_next_run(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    first = queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:coupangeats",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "work-coupangeats-mia",
            "priority": 20,
            "latest_only": True,
            "payload": {"services": ["coupangeats"], "business_id": "biz-mia"},
        }
    )
    queue_module.complete_collection_item(
        first["id"],
        status="action_required",
        error_code="PC_AGENT_LOGIN_REQUIRED",
        message="쿠팡이츠 로그인이 필요합니다.",
        next_run_at="2099-01-01T00:00:00+09:00",
    )

    second = queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:coupangeats",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "work-coupangeats-mia",
            "priority": 20,
            "latest_only": True,
            "payload": {"services": ["coupangeats"], "business_id": "biz-mia", "date_to": "2026-08-28"},
        }
    )

    assert second["status"] == "action_required"
    assert second["error_code"] == "PC_AGENT_LOGIN_REQUIRED"
    assert second["next_run_at"] == "2099-01-01T00:00:00+09:00"
    assert queue_module.claim_next_collection_item(agent_id="agent-1") is None


def test_global_queue_claims_one_running_resource_at_a_time(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:ddangyo",
            "service": "ddangyo",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "same-work",
            "priority": 30,
            "latest_only": False,
            "payload": {"services": ["ddangyo"]},
        }
    )
    queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:ddangyo",
            "service": "ddangyo",
            "business_id": "biz-sungshin",
            "branch": "성신여대점",
            "work_key": "same-work",
            "priority": 30,
            "latest_only": False,
            "payload": {"services": ["ddangyo"]},
        }
    )

    first = queue_module.claim_next_collection_item(agent_id="agent-1")
    second = queue_module.claim_next_collection_item(agent_id="agent-1")

    assert first is not None
    assert first["status"] == "running"
    assert second is None


def test_stale_zombie_running_item_recovers_to_queued(tmp_path, monkeypatch):
    """status=running + lease_agent_id='' + updated_at 임계값 초과 항목은 queued로 복구된다.

    AADS-FOOD-QUEUE-DRAIN-AGENT-ONLINE-MISMATCH-P0: wait_for_agent_online()이 어긋난 판정
    소스로 계속 오프라인을 리턴하는 동안 claim_next_collection_item()이 전혀 호출되지 않아
    running 상태로 좌초된 항목(예: 신한 미아점)이 영원히 재시도되지 않는 문제를 재현한다.
    """
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("YEOLJEONG_QUEUE_STALE_RUNNING_SECONDS", "1800")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    queued_item = queue_module.enqueue_collection_item(
        {
            "queue_type": "bank",
            "site_key": "bank:shinhan_business",
            "service": "shinhan_business",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "bank-shinhan-mia",
            "priority": 10,
            "latest_only": True,
            "payload": {"bank_account_id": "shinhan-mia"},
        }
    )

    stale_updated_at = (datetime.now(timezone.utc) - timedelta(hours=7)).astimezone(
        timezone(timedelta(hours=9))
    ).isoformat(timespec="seconds")
    rows = json.loads(queue_path.read_text(encoding="utf-8"))
    for row in rows:
        if row["id"] == queued_item["id"]:
            row["status"] = "running"
            row["lease_agent_id"] = ""
            row["started_at"] = ""
            row["updated_at"] = stale_updated_at
    queue_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    claimed = queue_module.claim_next_collection_item(agent_id="agent-1")

    assert claimed is not None
    assert claimed["id"] == queued_item["id"]
    assert claimed["status"] == "running"
    assert claimed["lease_agent_id"] == "agent-1"


def test_running_item_with_lease_agent_id_is_not_recovered(tmp_path, monkeypatch):
    """lease_agent_id가 채워진 정상 running 항목은 오래되어도 건드리지 않는다."""
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("YEOLJEONG_QUEUE_STALE_RUNNING_SECONDS", "1800")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    queued_item = queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:baemin",
            "service": "baemin",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "delivery-baemin-mia",
            "priority": 10,
            "latest_only": True,
            "payload": {"services": ["baemin"]},
        }
    )

    stale_updated_at = (datetime.now(timezone.utc) - timedelta(hours=7)).astimezone(
        timezone(timedelta(hours=9))
    ).isoformat(timespec="seconds")
    rows = json.loads(queue_path.read_text(encoding="utf-8"))
    for row in rows:
        if row["id"] == queued_item["id"]:
            row["status"] = "running"
            row["lease_agent_id"] = "agent-still-working"
            row["started_at"] = stale_updated_at
            row["updated_at"] = stale_updated_at
    queue_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    claimed = queue_module.claim_next_collection_item(agent_id="agent-1")
    snapshot = queue_module.queue_snapshot()

    assert claimed is None
    assert snapshot[0]["status"] == "running"
    assert snapshot[0]["lease_agent_id"] == "agent-still-working"


def test_financial_running_item_blocks_delivery_claim_on_same_agent(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    financial = queue_module.enqueue_collection_item(
        {
            "queue_type": "bank",
            "site_key": "bank:shinhan_business",
            "service": "shinhan_business",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "yeoljeong-bank-shinhan-mia",
            "priority": 10,
            "latest_only": False,
            "payload": {
                "project_key": "BANKING",
                "bank_account_id": "acct-shinhan-mia",
                "browser_agent_id": "agent-bank",
            },
        }
    )
    queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:coupangeats",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "yeoljeong-delivery-coupangeats-mia",
            "priority": 20,
            "latest_only": False,
            "payload": {"services": ["coupangeats"], "browser_agent_id": "agent-bank"},
        }
    )
    queue_module.complete_collection_item(financial["id"], status="queued")
    first = queue_module.claim_next_collection_item(agent_id="agent-bank")
    second = queue_module.claim_next_collection_item(agent_id="agent-bank")

    assert first is not None
    assert first["queue_type"] == "bank"
    assert first["resource_key"].startswith("financial_exclusive|")
    assert second is None


def test_financial_running_item_blocks_delivery_claim_on_other_agent(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    queue_module.enqueue_collection_item(
        {
            "queue_type": "bank",
            "site_key": "bank:shinhan_business",
            "service": "shinhan_business",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "yeoljeong-bank-shinhan-mia",
            "priority": 10,
            "latest_only": False,
            "payload": {
                "project_key": "BANKING",
                "bank_account_id": "acct-shinhan-mia",
                "browser_agent_id": "agent-bank",
            },
        }
    )
    queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:coupangeats",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "yeoljeong-delivery-coupangeats-mia",
            "priority": 20,
            "latest_only": False,
            "payload": {"services": ["coupangeats"], "browser_agent_id": "agent-other"},
        }
    )

    first = queue_module.claim_next_collection_item(agent_id="agent-bank")
    second = queue_module.claim_next_collection_item(agent_id="agent-other")

    assert first is not None
    assert first["queue_type"] == "bank"
    assert second is None


def test_due_financial_queued_item_blocks_delivery_claim_on_other_agent(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(queue_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module

    queue_module = importlib.reload(queue_module)
    queue_module.enqueue_collection_item(
        {
            "queue_type": "bank",
            "site_key": "bank:shinhan_business",
            "service": "shinhan_business",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "yeoljeong-bank-shinhan-mia",
            "priority": 10,
            "latest_only": False,
            "payload": {
                "project_key": "BANKING",
                "bank_account_id": "acct-shinhan-mia",
                "browser_agent_id": "agent-bank",
            },
        }
    )
    queue_module.enqueue_collection_item(
        {
            "queue_type": "delivery",
            "site_key": "delivery:coupangeats",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "work_key": "yeoljeong-delivery-coupangeats-mia",
            "priority": 20,
            "latest_only": False,
            "payload": {"services": ["coupangeats"], "browser_agent_id": "agent-other"},
        }
    )

    assert queue_module.claim_next_collection_item(agent_id="agent-other") is None
    claimed = queue_module.claim_next_collection_item(agent_id="agent-bank")

    assert claimed is not None
    assert claimed["queue_type"] == "bank"


def test_sync_db_api_reuses_private_loop_and_keeps_shared_pool(monkeypatch):
    """Regression: the drain CLI calls several sync queue APIs in a row.
    Each call used asyncio.run(), which closed the loop owning the shared asyncpg
    pool, so the next call failed with 'Event loop is closed'. The sync API now
    runs on one private loop and must never close the API server's shared pool."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")

    import app.core.db_pool as db_pool_module
    import app.services.pc_agent_collection_queue as queue_module

    monkeypatch.setattr(queue_module, "_SYNC_LOOP", None)
    events: list[str] = []

    async def fake_close_pool():
        events.append("close_pool")

    monkeypatch.setattr(db_pool_module, "close_pool", fake_close_pool)
    loops: list[object] = []

    async def fake_operation(tag: str):
        loops.append(asyncio.get_running_loop())
        events.append(f"op:{tag}")
        return tag

    assert queue_module._run_db(fake_operation("first")) == "first"
    assert queue_module._run_db(fake_operation("second")) == "second"
    assert events == ["op:first", "op:second"]
    assert loops[0] is loops[1]
    assert not loops[0].is_closed()
