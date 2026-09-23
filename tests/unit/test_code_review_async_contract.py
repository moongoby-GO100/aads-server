from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_async_review_api_persists_before_scheduling_and_polls_db():
    source = (ROOT / "app" / "api" / "code_review.py").read_text(encoding="utf-8")

    insert = source.index("INSERT INTO code_review_requests")
    schedule = source.index("_schedule_review_request(request_id)", insert)
    poll_route = source.index('@router.get("/code-diff/requests/{request_id}")')

    assert insert < schedule < poll_route
    assert "status_code=status.HTTP_202_ACCEPTED" in source
    assert "WHERE request_id=$1 AND status='queued'" in source
    assert "SET status='completed'" in source
    assert "stale async review recovered" in source
    assert "AND attempts=$10" in source
    assert "AND attempts=$3" in source
    assert "asyncio.wait_for(" in source
    assert '@router.post(\n    "/code-diff/requests/{request_id}/resume"' in source


def test_recovery_uses_persisted_payload_and_never_mutates_before_hash_check():
    source = (ROOT / "app" / "api" / "code_review.py").read_text(encoding="utf-8")
    create = source[source.index("async def create_code_review_request("):]
    resume = source[source.index("async def _resume_stored_request("):source.index('@router.post("/code-diff"')]

    assert create.index('row["payload_sha256"] != payload_hash') < create.index("_schedule_review_request(request_id)")
    assert "diff=" not in resume
    assert "instruction=" not in resume
    assert "files_changed=" not in resume


def test_runner_defaults_to_active_local_nginx_not_blue_port():
    for name in ("pipeline-runner.sh", "pipeline-runner.sh.local", "review-hold-sweeper.sh"):
        script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'AADS_API_URL="${AADS_API_URL:-http://127.0.0.1}"' in script


def test_async_review_migration_is_durable_and_idempotent():
    migration = (ROOT / "migrations" / "177_code_review_async_requests.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS code_review_requests" in migration
    assert "request_id UUID PRIMARY KEY" in migration
    assert "ADD COLUMN IF NOT EXISTS review_request_id UUID" in migration
