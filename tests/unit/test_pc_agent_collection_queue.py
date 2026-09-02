import importlib
import json
from datetime import datetime, timedelta, timezone


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
