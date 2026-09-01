import importlib


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
